"""Mount a project's Lisp program on a port.

The agent writes PURE Lisp, so a web app is one pure function:

    (handle-request request state)  ->  (:status 200 :headers (...) :body "..." :state new-state)

This module is the harness side of that contract and knows nothing about any
particular app. It listens on ``127.0.0.1``, turns each HTTP request into a
Lisp plist, calls the project's ``handle-request`` in the sandboxed SBCL
worker, sends back the response it returns, and stores the returned state in
SQLite so it survives restarts. Routing, pages, login - everything the app
does - is Lisp the agent built.

Everything impure is supplied in the request (``:now``, ``:nonce``) so the
handler stays a function of its two arguments.

    uv run python mount.py --project <id> --port 8160
    uv run python mount.py --project <id> --report
"""
import argparse
import contextlib
import json
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import agent_session as ag
import projects
import s_expr

HANDLER = "handle-request"
COMMAND = "handle-command"
INITIAL = "initial-state"
MAX_BODY = 64 * 1024              # request body bytes
MAX_STATE = 512 * 1024            # printed state characters
PORTS = range(8200, 8300)

CONTRACT = ag.WEB_APP_CONTRACT

_SYMBOL = re.compile(r"^:?[A-Za-z0-9*+\-/<>=!?._%&]+$")


def lisp_string(text):
    """TEXT as a Lisp string literal."""
    return '"%s"' % str(text).replace("\\", "\\\\").replace('"', '\\"')


def to_lisp(value):
    """Lisp source for a parsed value: lists, strings, numbers and plain symbols only.

    Raises ValueError for anything else, so state read back from the worker can
    never smuggle reader syntax into the next call.
    """
    if isinstance(value, s_expr.SString):
        return lisp_string(value)
    if isinstance(value, bool):
        raise ValueError("unsupported value")
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "(%s)" % " ".join(to_lisp(v) for v in value)
    if isinstance(value, str) and _SYMBOL.match(value):
        return value
    raise ValueError("state holds something that is not a list, string, number or "
                     "symbol: %r" % (value,))


def pairs(items):
    """``(("k" "v") ...)`` source for an iterable of (key, value) strings."""
    return "(%s)" % " ".join("(%s %s)" % (lisp_string(k), lisp_string(v)) for k, v in items)


def request_plist(method, target, headers, body, now=None, nonce=None):
    """The Lisp plist source describing one HTTP request."""
    url = urlsplit(target)
    form = parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True) \
        if method == "POST" else []
    cookies = [tuple(part.strip().split("=", 1)) for part in
               (headers.get("Cookie") or "").split(";") if "=" in part]
    return ("(:method %s :path %s :query %s :form %s :cookies %s :now %d :nonce %s)"
            % (lisp_string(method), lisp_string(url.path or "/"),
               pairs(parse_qsl(url.query, keep_blank_values=True)), pairs(form),
               pairs(cookies), int(now if now is not None else time.time()),
               lisp_string(nonce or secrets.token_hex(16))))


def handler_call(request_src, state_src):
    """Lisp that calls the handler and returns ``(status headers body changed state-text)``."""
    return ("(let* ((*print-pretty* nil) (*print-length* nil) (*print-level* nil)\n"
            "       (resp (%s '%s '%s)))\n"
            "  (list (getf resp :status 200) (getf resp :headers) (getf resp :body \"\")\n"
            "        (if (member :state resp) 1 0)\n"
            "        (prin1-to-string (getf resp :state))))"
            % (HANDLER, request_src, state_src))


def read_response(printed):
    """``(status, headers, body, state_src_or_None)`` from the worker's printed value.

    Raises ValueError when the handler did not follow the contract.
    """
    try:
        status, headers, body, changed, state_text = s_expr.parse(printed)
    except (s_expr.SExprError, ValueError, TypeError):
        raise ValueError("handle-request must return a plist with :status, :headers and :body")
    if not isinstance(status, int) or not 100 <= status <= 599:
        raise ValueError(":status must be an HTTP status number, got %r" % (status,))
    if not isinstance(body, s_expr.SString):
        raise ValueError(":body must be a string")
    out_headers = []
    if isinstance(headers, str):               # the symbol NIL: no headers
        headers = []
    for h in headers:
        if not (isinstance(h, list) and len(h) == 2
                and all(isinstance(x, s_expr.SString) for x in h)
                and re.fullmatch(r"[A-Za-z0-9-]+", h[0]) and "\n" not in h[1] and "\r" not in h[1]):
            raise ValueError(":headers must be a list of (\"Name\" \"value\") pairs")
        out_headers.append((str(h[0]), str(h[1])))
    state_src = None
    if changed == 1:
        if len(state_text) > MAX_STATE:
            raise ValueError("the returned state is too large (%d characters)" % len(state_text))
        state_src = to_lisp(s_expr.parse(state_text)) if state_text.strip().upper() != "NIL" \
            else "nil"
    return status, out_headers, str(body), state_src


class StateStore:
    """The app's state value, kept as Lisp text in one SQLite row."""

    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("create table if not exists app_state (id integer primary key "
                       "check (id = 1), value text not null, updated real not null)")

    @contextlib.contextmanager
    def _db(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def load(self):
        """The saved state source, or None when nothing was ever saved."""
        with self._db() as db:
            row = db.execute("select value from app_state where id = 1").fetchone()
        return row[0] if row else None

    def save(self, state_src):
        with self._db() as db:
            db.execute("insert into app_state (id, value, updated) values (1, ?, ?) "
                       "on conflict(id) do update set value = excluded.value, "
                       "updated = excluded.updated", (state_src, time.time()))


class MountedApp:
    """One project's ``handle-request`` behind HTTP. App-agnostic."""

    def __init__(self, registry, store, run_lisp=None, log_path=None):
        self.registry = registry
        self.store = store
        self.run_lisp = run_lisp or ag._worker_fn
        self.log_path = Path(log_path) if log_path else None
        self._lock = threading.Lock()
        self._cache = {}                 # (tools, state, request) -> response
        self.cache_max = 256

    def _names(self):
        return {t["name"] for t in self.registry.load()}

    def _time_free(self):
        """True when no saved tool mentions :now or :nonce, so answers depend
        only on the request and the state and may be reused."""
        return not any(re.search(r":(now|nonce)\b", t.get("definition") or "", re.I)
                       for t in self.registry.load())

    def run_command(self, args, now=None):
        """Run the project's ``handle-command`` for one command line.

        Returns ``{"ok", "output", "error"}``; the returned state is saved.
        """
        log = {"t": round(time.time(), 3), "method": "COMMAND", "path": " ".join(args)[:200]}
        started = time.perf_counter()
        out = {"ok": False, "output": "", "error": ""}
        try:
            if COMMAND not in self._names():
                raise LookupError("this project has no %s tool yet. Ask the agent to build "
                                  "the command-line app (it must define (%s args state now))."
                                  % (COMMAND, COMMAND))
            with self._lock:
                call = ("(let* ((*print-pretty* nil) (*print-length* nil) (*print-level* nil)\n"
                        "       (resp (%s '(%s) '%s %d)))\n"
                        "  (list (getf resp :output \"\") (if (member :state resp) 1 0)\n"
                        "        (prin1-to-string (getf resp :state))))"
                        % (COMMAND, " ".join(lisp_string(a) for a in args), self._state(),
                           int(now if now is not None else time.time())))
                log["lisp"] = call[:400]
                env = self._eval(call)
                log["lisp_ms"] = env.get("elapsed_ms")
                try:
                    text, changed, state_text = s_expr.parse(env["return_value"])
                except (s_expr.SExprError, ValueError, TypeError):
                    raise ValueError("handle-command must return a plist with :output")
                if not isinstance(text, s_expr.SString):
                    raise ValueError(":output must be a string")
                if changed == 1:
                    src = "nil" if state_text.strip().upper() == "NIL" \
                        else to_lisp(s_expr.parse(state_text))
                    if len(src) > MAX_STATE:
                        raise ValueError("the returned state is too large")
                    self.store.save(src)
                    log["state_saved"] = len(src)
                out = {"ok": True, "output": str(text), "error": ""}
        except Exception as exc:
            log["error"] = "%s: %s" % (type(exc).__name__, exc)
            out = {"ok": False, "output": "", "error": str(exc)}
        log["status"] = 200 if out["ok"] else 500
        log["ms"] = round((time.perf_counter() - started) * 1000, 1)
        self._write_log(log)
        return out

    def _eval(self, code):
        env = self.run_lisp("%s\n%s" % (self.registry.prelude(), code))
        if not env.get("ok"):
            raise RuntimeError(" ".join((env.get("error") or "the Lisp call failed")
                                        .split("--- backtrace ---")[0].split())[:400])
        if env.get("return_truncated"):
            raise RuntimeError("the response is too large for the worker to return")
        return env

    def _state(self):
        """Saved state, else ``(initial-state)`` when the project defines it, else NIL."""
        saved = self.store.load()
        if saved is not None:
            return saved
        if INITIAL in self._names():
            printed = self._eval("(let ((*print-pretty* nil)) (%s))" % INITIAL)["return_value"]
            return "nil" if printed.strip().upper() == "NIL" else to_lisp(s_expr.parse(printed))
        return "nil"

    def handle(self, method, target, headers, body=b""):
        """``(status, [(name, value)], body_text)``; never raises."""
        log = {"t": round(time.time(), 3), "method": method, "path": urlsplit(target).path}
        started = time.perf_counter()
        try:
            if HANDLER not in self._names():
                raise LookupError("this project has no %s tool yet. Ask the agent to build "
                                  "the web app (it must define (%s request state))."
                                  % (HANDLER, HANDLER))
            with self._lock:                 # one request at a time: read, call, save
                state = self._state()
                tools = self.registry.load()
                key = None
                if method == "GET" and self._time_free():
                    import hashlib
                    key = hashlib.sha256(repr((
                        [t["definition"] for t in tools], state, target,
                        headers.get("Cookie") or "")).encode("utf-8")).hexdigest()
                hit = self._cache.get(key) if key else None
                if hit:
                    status, out_headers, text = hit
                    log["cache"] = True
                else:
                    call = handler_call(request_plist(method, target, headers, body), state)
                    log["lisp"] = call[:400]
                    env = self._eval(call)
                    log["lisp_ms"] = env.get("elapsed_ms")
                    status, out_headers, text, state_src = read_response(env["return_value"])
                    if state_src is not None:
                        self.store.save(state_src)
                        log["state_saved"] = len(state_src)
                    elif key:
                        if len(self._cache) >= self.cache_max:
                            self._cache.clear()
                        self._cache[key] = (status, out_headers, text)
                log["response_chars"] = len(text)
        except LookupError as exc:
            log["error"] = str(exc)
            status, out_headers, text = 503, [], _page("Nothing is mounted yet", str(exc))
        except Exception as exc:             # the app failed: say so, keep the old state
            log["error"] = "%s: %s" % (type(exc).__name__, exc)
            status, out_headers, text = 500, [], _page(
                "The app failed on this request",
                "The saved state was not changed. The error is in the request log.\n\n%s" % exc)
        log["status"] = status
        log["ms"] = round((time.perf_counter() - started) * 1000, 1)
        self._write_log(log)
        return status, out_headers, text

    def _write_log(self, entry):
        if not self.log_path:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass


def _page(title, text):
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>%s</title></head>"
            "<body><h1>%s</h1><pre style=\"white-space:pre-wrap\">%s</pre></body></html>"
            % (esc(title), esc(title), esc(text)))


def report(log_path):
    """Summary of a request log: volume, speed and each distinct app failure."""
    rows = []
    if Path(log_path).exists():
        for line in Path(log_path).read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    errors = {}
    for r in rows:
        if r.get("error"):
            errors.setdefault(r["error"][:200], []).append(r)
    ms = sorted(r["ms"] for r in rows if "ms" in r)
    return {"requests": len(rows),
            "median_ms": ms[len(ms) // 2] if ms else None,
            "slowest_ms": ms[-1] if ms else None,
            "state_writes": sum(1 for r in rows if r.get("state_saved")),
            "served_from_cache": sum(1 for r in rows if r.get("cache")),
            "errors": [{"error": k, "count": len(v), "path": v[-1].get("path"),
                        "example_call": v[-1].get("lisp")}
                       for k, v in sorted(errors.items(), key=lambda kv: -len(kv[1]))]}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def _serve(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(length) if length else b""
            status, headers, text = app.handle(method, self.path, self.headers, body)
            data = text.encode("utf-8")
            self.send_response(status)
            names = {k.lower() for k, _ in headers}
            if "content-type" not in names:
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            if "content-security-policy" not in names:
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'self' 'unsafe-inline'")
            for k, v in headers:
                if k.lower() != "content-length":
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def log_message(self, fmt, *args):
            pass
    return Handler


def paths_for(agent_dir, project_id):
    """``(state_db, request_log)`` paths of a project's mounted app."""
    store = projects.ProjectStore(agent_dir)
    pid = store.resolve(project_id)
    folder = Path(agent_dir) / "apps" / pid
    return folder / "state.sqlite", folder / "requests.jsonl"


def free_port():
    for port in PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise OSError("no free port between %d and %d" % (PORTS[0], PORTS[-1]))


class MountManager:
    """Start and stop mounted apps, one per project."""

    def __init__(self, agent_dir, registry_for, mode="live"):
        self.agent_dir = Path(agent_dir)
        self.registry_for = registry_for
        self.mode = mode
        self._servers = {}
        self._lock = threading.Lock()

    def info(self, project_id):
        with self._lock:
            entry = self._servers.get(project_id)
        return {"port": entry[1], "url": "http://127.0.0.1:%d/" % entry[1]} if entry else None

    def start(self, project_id, port=None):
        """``(info, error)``. Starting an already mounted project returns its info."""
        if self.info(project_id):
            return self.info(project_id), None
        state_db, log = paths_for(self.agent_dir, project_id)
        app = MountedApp(self.registry_for(project_id).for_mode(self.mode),
                         StateStore(state_db), log_path=log)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port or free_port()),
                                         make_handler(app))
        except OSError as exc:
            return None, "could not open the port: %s" % exc
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with self._lock:
            self._servers[project_id] = (server, server.server_address[1])
        return self.info(project_id), None

    def stop(self, project_id):
        with self._lock:
            entry = self._servers.pop(project_id, None)
        if entry:
            entry[0].shutdown()
            entry[0].server_close()
        return bool(entry)

    def report(self, project_id):
        return report(paths_for(self.agent_dir, project_id)[1])

    def app(self, project_id):
        """A MountedApp for PROJECT_ID without a port (commands, tests)."""
        state_db, log = paths_for(self.agent_dir, project_id)
        return MountedApp(self.registry_for(project_id).for_mode(self.mode),
                          StateStore(state_db), log_path=log)

    def command(self, project_id, args):
        """Run one command line of the project's command-line app."""
        if not (isinstance(args, list) and all(isinstance(a, str) for a in args)
                and len(args) <= 32 and sum(map(len, args)) <= 4000):
            return {"ok": False, "output": "", "error": "args must be a short list of strings"}
        return self.app(project_id).run_command(args)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", default=None, help="project id (default: the built-in one)")
    ap.add_argument("--port", type=int, default=8160)
    ap.add_argument("--report", action="store_true", help="summarise the request log and exit")
    ap.add_argument("--command", nargs=argparse.REMAINDER,
                    help="run one command of the project's command-line app and exit")
    ap.add_argument("--shell", action="store_true",
                    help="interactive prompt for the project's command-line app")
    args = ap.parse_args(argv)
    store = projects.ProjectStore(ag.AGENT_DIR)
    if args.report:
        print(json.dumps(report(paths_for(ag.AGENT_DIR, args.project)[1]), indent=2))
        return 0
    manager = MountManager(ag.AGENT_DIR,
                           lambda pid: ag.ToolRegistry(store.tools_path(pid)))
    pid = store.resolve(args.project)
    if args.command is not None or args.shell:
        import shlex
        app = manager.app(pid)

        def run(words):
            out = app.run_command(words)
            print(out["output"] if out["ok"] else "error: " + out["error"], flush=True)
            return out["ok"]
        if args.command is not None:
            return 0 if run(args.command) else 1
        print("%s - type a command, or quit" % pid, flush=True)
        while True:
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                return 0
            if line.strip() in ("quit", "exit"):
                return 0
            if line.strip():
                run(shlex.split(line))
    info, err = manager.start(store.resolve(args.project), args.port)
    if err:
        print(err, file=sys.stderr)
        return 1
    print("mounted %s on %s" % (store.resolve(args.project), info["url"]), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        manager.stop(store.resolve(args.project))
    return 0


if __name__ == "__main__":
    sys.exit(main())
