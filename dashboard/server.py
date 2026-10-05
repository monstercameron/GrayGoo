#!/usr/bin/env python3
"""GrayGoo agent control + visualization dashboard backend.

Stdlib only (http.server + subprocess + json + friends). No pip dependencies.
Binds 127.0.0.1 (loopback) only -- no auth, single-user local use.

Endpoints (JSON):
  GET  /api/status          todos census, git log, spend, presence checks
  GET  /api/pipeline        static pipeline stages + last demo summary
  POST /api/run             {job: smoke|tests|baseline-stub} -> {job_id} (409 if busy)
  GET  /api/jobs/{id}       {state, exit_code, tail, output}
  POST /api/jobs/{id}/stop  kill a running job
  GET  /api/events          SSE job snapshots (optional; polling /api/jobs works too)
  GET  /, /index.html, /app.js, /styles.css   static frontend

Secrets policy: values matching CEREBRAS_API_KEY / CEREBRAS patterns are
redacted in every response and in every byte written to job output files.
Presence is reported as present/absent only, never the value.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DASH_DIR = Path(__file__).resolve().parent
OUT_DIR = ROOT / "artifacts" / "dashboard"
BUDGET_USD = 50.0
HOST = "127.0.0.1"  # loopback only; not configurable by design
DEFAULT_PORT = 8137
TAIL_LINES = 40

# ---------------------------------------------------------------------------
# Secrets redaction
# ---------------------------------------------------------------------------

_REDACT_PATTERNS = [
    # CEREBRAS_API_KEY=xxxx / "cerebras_api_key": "xxxx" / cerebras key: xxxx
    re.compile(
        r"(?i)(cerebras[_\- ]?(?:api[_\- ]?key)?[\"']?\s*[:=]\s*[\"']?)"
        r"([^\s\"',}]+)"
    ),
    # sk-... style tokens (defensive; never a dashboard value)
    re.compile(r"(sk-[A-Za-z0-9\-_]{8,})"),
]


def redact_text(text):
    """Replace secret values with [REDACTED]. Never raises on bad input."""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return "[unprintable]"
    for pat in _REDACT_PATTERNS:
        text = pat.sub(r"\1[REDACTED]", text)
    return text


# ---------------------------------------------------------------------------
# Status collectors (pure-ish: all take root, no globals mutated)
# ---------------------------------------------------------------------------

_TODO_OPEN = re.compile(r"^\s*-\s*\[\s\]", re.MULTILINE)
_TODO_DONE = re.compile(r"^\s*-\s*\[[xX]\]", re.MULTILINE)


def parse_todos(root):
    """Count checked/open checkbox items in todos.md."""
    path = Path(root) / "todos.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"open": 0, "checked": 0, "total": 0, "found": False}
    checked = len(_TODO_DONE.findall(text))
    open_ = len(_TODO_OPEN.findall(text))
    return {"open": open_, "checked": checked, "total": open_ + checked,
            "found": True}


def get_git_log(root, n=8):
    """Last n oneline commits; [] with note when git is unavailable."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "log", "--oneline", f"-{n}"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return {"commits": [], "available": False}
    if proc.returncode != 0:
        return {"commits": [], "available": False}
    return {"commits": [redact_text(line)
                        for line in proc.stdout.splitlines() if line.strip()],
            "available": True}


def _sum_cost_usd(node):
    total = 0.0
    found = False
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "cost_usd" and isinstance(value, (int, float)):
                total += float(value)
                found = True
            else:
                sub, sub_found = _sum_cost_usd(value)
                total += sub
                found = found or sub_found
    elif isinstance(node, list):
        for value in node:
            sub, sub_found = _sum_cost_usd(value)
            total += sub
            found = found or sub_found
    return total, found


def spend_tracker(root):
    """Sum cost_usd across artifacts/**/usage*.json; unknown when absent."""
    files = sorted(Path(root).glob("artifacts/**/usage*.json"))
    spent = 0.0
    counted = 0
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sub, found = _sum_cost_usd(payload)
        if found:
            spent += sub
            counted += 1
    if not files or counted == 0:
        return {"known": False, "spent_usd": None, "budget_usd": BUDGET_USD,
                "files": len(files)}
    return {"known": True, "spent_usd": round(spent, 6),
            "budget_usd": BUDGET_USD, "files": len(files)}


def presence_checks(root):
    """Tool/key/venv presence. Key reported as present/absent ONLY."""
    root = Path(root)
    sbcl = shutil.which("sbcl") is not None
    if not sbcl:
        # Mirror workers.py resolution: $GRAYGOO_SBCL, else the repo-standard
        # user-local SBCL (off-PATH by design).
        override = os.environ.get("GRAYGOO_SBCL", "")
        default = os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "sbcl-local", "sbcl-2.6.9",
            "PFiles", "Steel Bank Common Lisp", "sbcl.exe")
        sbcl = bool(override and os.path.exists(override)) or os.path.exists(
            default)
    venv_dir = root / ".venv"
    venv = venv_dir.is_dir() and (
        (venv_dir / "pyvenv.cfg").exists()
        or (venv_dir / "Scripts" / "python.exe").exists()
        or (venv_dir / "bin" / "python").exists()
    )
    # Mirror cerebras_client auth: CEREBRAS_API_KEY (preferred) or CEREBRAS.
    key_present = bool(os.environ.get("CEREBRAS_API_KEY")
                       or os.environ.get("CEREBRAS"))
    if not key_present:
        try:
            for line in (root / ".env").read_text(encoding="utf-8",
                                                  errors="replace").splitlines():
                line = line.strip()
                if re.match(r"(?i)(export\s+)?CEREBRAS(_API_KEY)?\s*=",
                            line) and line.split("=", 1)[1].strip():
                    key_present = True
                    break
        except OSError:
            pass
    return {"sbcl": sbcl, "cerebras_key": "present" if key_present else "absent",
            "venv": venv}


def build_status(root):
    """Full /api/status payload."""
    todos = parse_todos(root)
    git = get_git_log(root)
    spend = spend_tracker(root)
    presence = presence_checks(root)
    return {"todos": todos, "git": git, "spend": spend, "presence": presence}


# ---------------------------------------------------------------------------
# Pipeline description
# ---------------------------------------------------------------------------

STAGES = [
    {"id": "parse", "label": "Parse",
     "desc": "Parse candidate Lisp into s-expressions; reject malformed output."},
    {"id": "risk", "label": "Risk",
     "desc": "Classify mutation risk R0-R6 before any execution."},
    {"id": "worker", "label": "Worker",
     "desc": "Compile + run in an isolated rehearsal worker (timeout, limits)."},
    {"id": "repair", "label": "Repair",
     "desc": "Attempt automated repair of failing candidates."},
    {"id": "patch", "label": "Patch",
     "desc": "Assemble promoted patch with lineage and contracts."},
    {"id": "transfer", "label": "Transfer",
     "desc": "Transfer lessons/patches to persistent memory."},
]


def _trim_demo_summary(payload):
    """Extract a small, stable subset of artifacts/demo-e2e/summary.json."""
    if not isinstance(payload, dict):
        return None
    trimmed = {}
    for key, value in payload.items():
        if isinstance(value, (int, float, str, bool)) or value is None:
            trimmed[key] = value
        elif isinstance(value, list):
            trimmed[key + "_count"] = len(value)
            sub, found = _sum_cost_usd(value)
            if found:
                trimmed[key + "_cost_usd"] = round(sub, 6)
        elif isinstance(value, dict):
            trimmed[key + "_keys"] = sorted(value)[:12]
    return trimmed


def build_pipeline(root):
    """Static run_candidate stage list + last demo summary if present."""
    summary = None
    summary_path = Path(root) / "artifacts" / "demo-e2e" / "summary.json"
    try:
        summary = _trim_demo_summary(
            json.loads(summary_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        summary = None
    return {"stages": STAGES,
            "last_demo": {"found": summary is not None, "summary": summary}}


# ---------------------------------------------------------------------------
# Job runner (single concurrent job)
# ---------------------------------------------------------------------------

def _runner_prefix():
    """Prefer `uv run python ...`; fall back to the current interpreter."""
    if shutil.which("uv") is not None:
        return ["uv", "run", "python"]
    return [sys.executable]


def default_commands():
    prefix = _runner_prefix()
    return {
        "smoke": prefix + ["demo.py", "--smoke"],
        # Full unittest suite for the repo.
        "tests": prefix + ["-m", "unittest", "discover", "-s", "tests"],
        # Offline baseline listing (stub adapter; --list exits before any
        # recorded-output requirement, no live calls, no secrets needed).
        "baseline-stub": prefix + ["benchmarks/runner.py", "--adapter",
                                   "stub", "--list"],
    }


class JobManager:
    """Tracks background jobs; at most one running at a time."""

    def __init__(self, root, out_dir=None, commands=None):
        self.root = Path(root)
        self.out_dir = Path(out_dir) if out_dir else OUT_DIR
        self.commands = dict(commands) if commands is not None \
            else default_commands()
        self._lock = threading.Lock()
        self._jobs = {}
        self._running_id = None

    def launch(self, kind):
        """Start a job. Returns (job_id, None) or (None, 'busy'|'unknown')."""
        if kind not in self.commands:
            return None, "unknown"
        with self._lock:
            if self._running_id is not None:
                return None, "busy"
            job_id = uuid.uuid4().hex[:12]
            self.out_dir.mkdir(parents=True, exist_ok=True)
            output_path = self.out_dir / f"{job_id}-{kind}.log"
            record = {
                "id": job_id, "kind": kind,
                "argv": list(self.commands[kind]),
                "state": "running", "exit_code": None,
                "started_at": time.time(), "ended_at": None,
                "output": str(output_path),
                "tail": [],
                "proc": None,
            }
            self._jobs[job_id] = record
            self._running_id = job_id
        thread = threading.Thread(target=self._run, args=(job_id,),
                                  daemon=True)
        thread.start()
        return job_id, None

    def _run(self, job_id):
        record = self._jobs[job_id]
        try:
            with open(record["output"], "w", encoding="utf-8",
                      errors="replace") as handle:
                handle.write(f"$ {' '.join(record['argv'])}\n")
                handle.flush()
                proc = subprocess.Popen(
                    record["argv"], cwd=str(self.root),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="replace", bufsize=1,
                )
                record["proc"] = proc
                try:
                    for line in proc.stdout:
                        clean = redact_text(line.rstrip("\n"))
                        handle.write(clean + "\n")
                        handle.flush()
                        record["tail"].append(clean)
                        del record["tail"][:-TAIL_LINES]
                finally:
                    proc.stdout.close()
                proc.wait()
                record["exit_code"] = proc.returncode
                record["state"] = "done" if proc.returncode == 0 else "error"
        except Exception as exc:  # never leave a job stuck in running
            record["tail"].append(redact_text(f"[runner failed: {exc}]"))
            record["exit_code"] = -1
            record["state"] = "error"
        finally:
            record["ended_at"] = time.time()
            record["proc"] = None
            with self._lock:
                if self._running_id == job_id:
                    self._running_id = None

    def get(self, job_id):
        """Redacted public snapshot, or None for unknown ids."""
        record = self._jobs.get(job_id)
        if record is None:
            return None
        return {
            "job_id": record["id"], "kind": record["kind"],
            "state": record["state"], "exit_code": record["exit_code"],
            "started_at": record["started_at"],
            "ended_at": record["ended_at"],
            "tail": [redact_text(line) for line in record["tail"]],
            "output": record["output"],
        }

    def stop(self, job_id):
        """Kill a running job. True stopped, False not-running, None unknown."""
        record = self._jobs.get(job_id)
        if record is None:
            return None
        if record["state"] != "running":
            return False
        # The runner thread may not have spawned the process yet; wait briefly.
        proc = None
        for _ in range(100):
            proc = record.get("proc")
            if proc is not None or record["state"] != "running":
                break
            time.sleep(0.05)
        if proc is None:
            return False
        try:
            proc.kill()
        except OSError:
            return False
        return True

    def busy(self):
        with self._lock:
            return self._running_id


# ---------------------------------------------------------------------------
# Request dispatch (pure layer; unit-tested without sockets)
# ---------------------------------------------------------------------------

def dispatch(method, path, body, ctx):
    """Route one request. Returns (http_status, jsonable_payload)."""
    root = ctx.root
    manager = ctx.manager
    clean_path = urlparse(path).path.rstrip("/") or "/"

    if method == "GET" and clean_path == "/api/status":
        return 200, build_status(root)
    if method == "GET" and clean_path == "/api/pipeline":
        return 200, build_pipeline(root)

    if method == "POST" and clean_path == "/api/run":
        try:
            payload = json.loads(body.decode("utf-8") if body else "{}")
        except (ValueError, UnicodeDecodeError):
            return 400, {"error": "invalid JSON body"}
        kind = payload.get("job")
        job_id, err = manager.launch(kind)
        if err == "unknown":
            return 400, {"error": f"unknown job: {kind!r}"}
        if err == "busy":
            return 409, {"error": "a job is already running",
                         "running_id": manager.busy()}
        return 200, {"job_id": job_id}

    if clean_path.startswith("/api/jobs/"):
        rest = clean_path[len("/api/jobs/"):]
        job_id, _, action = rest.partition("/")
        if not job_id or "/" in action and action != "stop":
            return 404, {"error": "unknown job"}
        if method == "GET" and not action:
            snapshot = manager.get(job_id)
            if snapshot is None:
                return 404, {"error": "unknown job"}
            return 200, snapshot
        if method == "POST" and action == "stop":
            result = manager.stop(job_id)
            if result is None:
                return 404, {"error": "unknown job"}
            snapshot = manager.get(job_id)
            return 200, {"stopped": result, "job": snapshot}
        return 404, {"error": "unknown endpoint"}

    return 404, {"error": "unknown endpoint"}


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}
STATIC_FILES = {"index.html", "app.js", "styles.css"}


class Handler(BaseHTTPRequestHandler):
    ctx = None  # set by serve()
    server_version = "GrayGooDashboard/1.0"

    def log_message(self, fmt, *args):  # keep logs; paths only, no bodies
        sys.stderr.write("dashboard: " + fmt % args + "\n")

    def _send_json(self, status, payload):
        body = redact_text(json.dumps(payload)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name):
        path = DASH_DIR / name
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type",
                         STATIC_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        clean_path = urlparse(self.path).path
        if clean_path in ("/", "/index.html"):
            self._send_static("index.html")
            return
        name = clean_path.lstrip("/")
        if name in STATIC_FILES:
            self._send_static(name)
            return
        if clean_path == "/api/events":
            self._send_events()
            return
        status, payload = dispatch("GET", self.path, b"", self.ctx)
        self._send_json(status, payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        status, payload = dispatch("POST", self.path, body, self.ctx)
        self._send_json(status, payload)

    def _send_events(self):
        """Minimal SSE: job snapshots every 2s for up to ~60s."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for _ in range(30):
                running = self.ctx.manager.busy()
                payload = {"running_id": running}
                if running:
                    payload["job"] = self.ctx.manager.get(running)
                line = "data: " + redact_text(json.dumps(payload)) + "\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve(port, manager=None):
    manager = manager or JobManager(ROOT)
    Handler.ctx = SimpleNamespace(root=ROOT, manager=manager)
    server = ThreadingHTTPServer((HOST, port), Handler)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="GrayGoo dashboard backend")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    server = serve(args.port)
    print(f"GrayGoo dashboard: http://{HOST}:{args.port}/ (loopback only, "
          f"no auth -- single-user local use)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
