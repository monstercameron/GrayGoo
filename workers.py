"""SBCL rehearsal-worker spike: drive isolated Lisp child processes from Python.

Plan reference: plan.md sections 19 (rehearsal workers), 20 (prewarmed
worker pool), 21 (rehearsal phases), 60 (resource limits). Covers the
``Create the rehearsal system`` items in todos.md.

Each :func:`run_lisp` call spawns a fresh ``sbcl --non-interactive``
child on a generated, BOM-free temp script. The script loads the tiny
``run-test-thunk`` convention from ``src/worker/`` (plus the rehearsal
alias in ``src/rehearsal/``), evaluates the caller-supplied code, and
prints a single-line JSON result envelope between marker lines. The
driver owns all orchestration: timeouts, pooling, recycling.

Stdlib only (``subprocess``, ``tempfile``, ``json``, ``time``, plus
``os``/``hashlib``/``threading`` for paths, fingerprints, and pooling).

Resource limits (plan.md section 60)
------------------------------------
* Wall-clock timeout: enforced robustly via ``Popen.communicate`` plus
  ``kill``; a runaway child cannot outlive ``timeout_s`` plus reaping
  slack, and the handle is never reused afterwards.
* Memory (``memory_mb``): passed to SBCL as ``--dynamic-space-size``,
  which caps the Lisp heap only, not total process RSS, and cannot
  contain foreign (non-heap) allocation. No OS-level job-object cap is
  applied in this spike: raising one from stdlib-only Python on Windows
  needs raw ``ctypes`` job-object plumbing, which is not trivially
  available, so it is deliberately left out rather than half-built.
* CPU: no quota or affinity is applied; a busy loop burns one core
  until the wall-clock timeout kills it. Same rationale as above.

Worker sandbox (adversarial hardening; see sandbox.py)
------------------------------------------------------
:func:`run_lisp` installs Lisp-level containment by default
(``sandbox=True``): every run first evaluates
``sandbox.build_prelude(jail)``, which invokes
``evo.worker::install-worker-sandbox`` from ``src/worker/worker.lisp``
(denies file/process/foreign-module operations, gates REQUIRE, re-locks
implementation packages). Pass ``sandbox=False`` only for trusted local
runs. ``jail`` (a ``sandbox.WorkerJail`` or a path) pins the worker's cwd
to the jail root and pins ``*default-pathname-defaults*`` there; it
audits writes but does NOT OS-confine the child (same user, full
rights). Lisp-level containment is bypassable in-image (SB-UNIX /
SB-IMPL / SB-ALIEN); OS enforcement is still future work.
"""

from __future__ import annotations

import fingerprint
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time

try:
    import sandbox as _sandbox
except ImportError:  # sandbox.py absent: sandboxed runs must fail, not degrade
    _sandbox = None

__all__ = [
    "SBCL_EXE",
    "WorkerPool",
    "generation_fingerprint",
    "resolve_sbcl",
    "run_lisp",
]

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

#: Default SBCL location (verified SBCL 2.6.9). Overridable per-call via
#: ``sbcl_exe=...`` or globally via the ``GRAYGOO_SBCL`` env var.
SBCL_EXE = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "sbcl-local",
    "sbcl-2.6.9",
    "PFiles",
    "Steel Bank Common Lisp",
    "sbcl.exe",
)

SBCL_ENV_VAR = "GRAYGOO_SBCL"

BEGIN_MARKER = "GRAYGOO-RESULT-BEGIN"
END_MARKER = "GRAYGOO-RESULT-END"

MAX_STDOUT_CHARS = 100_000
MAX_BACKTRACE_CHARS = 8_000
MAX_STDERR_TAIL_CHARS = 2_000
MAX_STDERR_HEAD_CHARS = 500

_sbcl_version_cache = {}
_sbcl_version_lock = threading.Lock()


def resolve_sbcl(sbcl_exe=None):
    """Resolve the SBCL executable: explicit arg, env var, then default."""
    if sbcl_exe:
        return sbcl_exe
    return os.environ.get(SBCL_ENV_VAR) or SBCL_EXE


def _sbcl_version(sbcl_exe):
    """Best-effort ``sbcl --version`` first line; ``"unknown"`` on failure."""
    with _sbcl_version_lock:
        if sbcl_exe in _sbcl_version_cache:
            return _sbcl_version_cache[sbcl_exe]
    try:
        proc = subprocess.run(
            [sbcl_exe, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        first = proc.stdout.decode("utf-8", errors="replace").splitlines()
        version = first[0].strip() if first else "unknown"
    except Exception:
        version = "unknown"
    with _sbcl_version_lock:
        _sbcl_version_cache[sbcl_exe] = version
    return version


def _asd_hash():
    """Short sha256 of graygoo.asd; ``"missing"`` if unreadable."""
    try:
        with open(os.path.join(REPO_ROOT, "graygoo.asd"), "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:16]
    except OSError:
        return "missing"


def generation_fingerprint(epoch_id=""):
    """Strong generation fingerprint stamped into every worker run.

    Delegates to :mod:`fingerprint` (source tree + asd + dependencies +
    capability manifest + schema generation + protocol version; issues.md
    #4). Same call signature as the original weak digest; the epoch id
    is caller-supplied (``"-"`` when empty).
    """
    return fingerprint.compute_string(epoch_id=epoch_id)


def _lisp_escape(text):
    """Escape TEXT for embedding in a Common Lisp string literal.

    CL strings only understand ``\\"`` and ``\\\\`` escapes; newlines
    stay literal.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[... truncated to %d chars ...]" % limit


_SCRIPT_TEMPLATE = """;;;; GRAYGOO rehearsal worker run (generated by workers.py; do not edit).
;;;; fingerprint: @@FINGERPRINT_COMMENT@@
(defparameter *graygoo-fingerprint* "@@FINGERPRINT@@")
(defparameter *graygoo-code* "@@CODE@@")
(defparameter *graygoo-prelude* "@@PRELUDE@@")
(let ((*load-verbose* nil)
      (*load-print* nil)
      (*compile-verbose* nil)
      (*compile-print* nil))
  (load "@@ROOT@@/src/worker/packages.lisp")
  (load "@@ROOT@@/src/worker/worker.lisp")
  (load "@@ROOT@@/src/rehearsal/packages.lisp")
  (load "@@ROOT@@/src/rehearsal/rehearsal.lisp"))
(defun graygoo-json-escape (string)
  "Escape STRING for JSON, using ASCII-only output."
  (with-output-to-string (out)
    (loop for ch across string
          for code = (char-code ch)
          do (cond ((char= ch #\\") (write-string "\\\\\\"" out))
                   ((char= ch #\\\\) (write-string "\\\\\\\\" out))
                   ((char= ch #\\Newline) (write-string "\\\\n" out))
                   ((char= ch #\\Return) (write-string "\\\\r" out))
                   ((char= ch #\\Tab) (write-string "\\\\t" out))
                   ((< code 32) (format out "\\\\u~4,'0X" code))
                   ((> code 127)
                    (if (< code 65536)
                        (format out "\\\\u~4,'0X" code)
                        (let ((tail (- code 65536)))
                          (format out "\\\\u~4,'0X\\\\u~4,'0X"
                                  (+ #xD800 (ash tail -10))
                                  (+ #xDC00 (logand tail #x3FF))))))
                   (t (write-char ch out))))))
(defun graygoo-eval-all (text)
  "Read, COMPILE, and run every form in TEXT; return the last value.
READ-EVAL is NIL while reading (issues.md #1): every form is read BEFORE
any form is evaluated, so no evaluated form can re-enable sharp-dot (#.)
reader evaluation for a later read. Reader evaluation never fires on
this driver path; candidate-initiated reads are the candidate's own.
Each form is COMPILEd before it runs (issues.md #60): compile-time
errors fail rehearsal instead of slipping through an interpreter."
  (let ((forms nil)
        (*read-eval* nil))
    (with-input-from-string (in text)
      (loop for form = (read in nil in)
            until (eq form in)
            do (push form forms)))
    (let ((result nil))
      (dolist (form (nreverse forms) result)
        (setf result (funcall (compile nil `(lambda () ,form))))))))
(defun graygoo-backtrace ()
  "Best-effort backtrace string; never signals."
  (let ((s (make-string-output-stream)))
    (ignore-errors (sb-debug:print-backtrace :stream s :count 24))
    (get-output-stream-string s)))
(let ((real-out *standard-output*)
      (captured (make-string-output-stream))
      (ok nil)
      (payload "")
      (bt "")
      (user-out "")
      (cand-ms 0)
      (err-type "")
      (abbr nil))
  (let ((*standard-output* captured)
        (*error-output* captured)
        (*trace-output* captured)
        (*terminal-io* captured)
        (*debug-io* captured))
    (handler-case
        (multiple-value-bind (pre-ok pre-payload pre-bt pre-type pre-abbr)
            (evo.worker:run-test-thunk
             (lambda ()
               (unless (string= *graygoo-prelude* "")
                 (graygoo-eval-all *graygoo-prelude*))
               nil))
          (declare (ignore pre-abbr))
          (if pre-ok
              (let ((t0 (get-internal-real-time)))
                (multiple-value-bind (ok2 payload2 bt2 type2 abbr2)
                    (evo.worker:run-test-thunk
                     (lambda () (graygoo-eval-all *graygoo-code*)))
                  (setf ok ok2 payload payload2 bt bt2
                        err-type type2 abbr abbr2
                        cand-ms (float (* 1000 (/ (- (get-internal-real-time) t0)
                                                  internal-time-units-per-second))))))
              (setf ok nil
                    payload (concatenate 'string "prelude error: " pre-payload)
                    bt pre-bt
                    err-type pre-type)))
      (serious-condition (c)
        (setf ok nil
              payload (princ-to-string c)
              bt (graygoo-backtrace))))
    (setf user-out (get-output-stream-string captured)))
  (format real-out "~&@@BEGIN@@~%")
  (format real-out "{\\"ok\\": ~A, \\"stdout\\": \\"~A\\", \\"return_value\\": \\"~A\\", \\"error\\": \\"~A\\", \\"backtrace\\": \\"~A\\", \\"fingerprint\\": \\"~A\\", \\"candidate_ms\\": ~A, \\"error_type\\": \\"~A\\", \\"return_truncated\\": ~A}~%"
          (if ok "true" "false")
          (graygoo-json-escape user-out)
          (graygoo-json-escape (if ok payload ""))
          (graygoo-json-escape (if ok "" payload))
          (graygoo-json-escape bt)
          (graygoo-json-escape *graygoo-fingerprint*)
          cand-ms
          (graygoo-json-escape err-type)
          (if abbr "true" "false"))
  (format real-out "@@END@@~%")
  (finish-output real-out))
(sb-ext:exit :code 0)
"""


def _build_script(code, prelude, fingerprint):
    root = REPO_ROOT.replace("\\", "/")
    comment = fingerprint.replace("\r", " ").replace("\n", " ")
    script = _SCRIPT_TEMPLATE
    script = script.replace("@@FINGERPRINT_COMMENT@@", comment)
    script = script.replace("@@FINGERPRINT@@", _lisp_escape(fingerprint))
    script = script.replace("@@CODE@@", _lisp_escape(code))
    script = script.replace("@@PRELUDE@@", _lisp_escape(prelude))
    script = script.replace("@@ROOT@@", root)
    script = script.replace("@@BEGIN@@", BEGIN_MARKER)
    script = script.replace("@@END@@", END_MARKER)
    return script


def _parse_envelope(stdout_text):
    """Return the decoded envelope dict, or None when markers are absent.

    Last complete pair wins (issues.md #56): the driver prints the
    real envelope after all candidate code has run, so a pre-printed
    forged envelope loses. A malformed last pair fails closed (no
    fallback to earlier pairs).
    """
    lines = stdout_text.splitlines()
    begins = [i for i, line in enumerate(lines) if line == BEGIN_MARKER]
    if not begins:
        return None
    begin = begins[-1]
    try:
        end = lines.index(END_MARKER, begin + 1)
    except ValueError:
        return None
    payload = "".join(lines[begin + 1 : end])
    try:
        decoded = json.loads(payload)
    except ValueError:
        return {"__malformed__": payload[:500]}
    return decoded if isinstance(decoded, dict) else {"__malformed__": payload[:500]}


def _jail_root(jail):
    """Resolve ``jail`` (WorkerJail or path) to a cwd string, else None."""
    if jail is None:
        return None
    if hasattr(jail, "root"):
        root = jail.root
    else:
        root = os.fspath(jail)
    if not isinstance(root, str):
        raise TypeError("jail root must be str, got %s"
                        % type(root).__name__)
    os.makedirs(root, exist_ok=True)
    return root


#: Minimal environment allowlist for worker processes (issues.md #46):
#: the worker must not inherit coordinator credentials or config. Only
#: the variables SBCL needs on Windows to start (system root, path
#: lookup, temp files) are passed through.
_WORKER_ENV_ALLOWLIST = ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT",
                         "TEMP", "TMP")


def _sanitized_env():
    """Build the worker environment: allowlist only, never inherited whole."""
    return {key: os.environ[key] for key in _WORKER_ENV_ALLOWLIST
            if key in os.environ}


def _sandbox_prelude(sandbox, jail):
    """Sandbox prelude text ("" only when explicitly disabled).

    Raises RuntimeError when sandboxing is requested but sandbox.py
    is unavailable: fail closed, never silently unsandboxed.
    """
    if not sandbox:
        return ""
    if _sandbox is None:
        raise RuntimeError(
            "sandbox requested but sandbox.py is unavailable; "
            "refusing to run unsandboxed")
    return _sandbox.build_prelude(jail)


def run_lisp(code, *, timeout_s=10.0, memory_mb=512, prelude="", epoch_id="",
             sbcl_exe=None, sandbox=True, jail=None):
    """Evaluate Lisp CODE in a fresh isolated SBCL child process.

    ``code`` / ``prelude`` are strings holding one or more Lisp forms;
    every form is evaluated in order and the last ``code`` value is
    reported (PRIN1 representation). ``prelude`` runs first for setup;
    its values are discarded.

    TRUST BOUNDARY (issues.md #76): ``prelude`` executes AFTER the
    sandbox containment guard, so it runs with enough authority to
    dismantle the sandbox. Only trusted-kernel preludes (repo-owned
    setup like the JSON helpers) may be passed here — never
    generated, task-controlled, or candidate-derived content.

    ``sandbox`` (default True) first evaluates the containment prelude
    from :mod:`sandbox` (worker install + optional jail-root defaults);
    pass False only for trusted local runs. ``jail`` (a
    ``sandbox.WorkerJail`` or a path) pins the worker's cwd to the jail
    root. Neither OS-confines the child (see :mod:`sandbox`).

    Returns exactly ``{"ok", "stdout", "return_value", "error",
    "timed_out", "elapsed_ms", "candidate_ms", "error_type",
    "return_truncated"}``: ``ok`` is True only when the code ran
    cleanly; ``stdout`` is the code's captured output; ``return_value``
    is the PRIN1 string ("" unless ok); ``error`` holds the condition
    plus backtrace ("" when ok); ``timed_out`` flags wall-clock expiry;
    ``elapsed_ms`` is driver wall time (spawn + SBCL startup +
    candidate); ``candidate_ms`` is the in-worker candidate-eval time
    (issues.md #77; None when the worker never reached the candidate);
    ``error_type`` names the signalled condition type ("" when ok;
    issues.md #59); ``return_truncated`` flags values abbreviated past
    the transport cap (issues.md #57/#58 — compare as unverifiable).
    Worker-side failures never raise; only API misuse (bad types,
    non-positive timeout/memory) raises.
    """
    if not isinstance(code, str):
        raise TypeError("code must be str, got %s" % type(code).__name__)
    if not isinstance(prelude, str):
        raise TypeError("prelude must be str, got %s" % type(prelude).__name__)
    if timeout_s is None or timeout_s <= 0:
        raise ValueError("timeout_s must be positive, got %r" % (timeout_s,))
    memory_mb = int(memory_mb)
    if memory_mb <= 0:
        raise ValueError("memory_mb must be positive, got %r" % (memory_mb,))
    jail_root = _jail_root(jail)
    guard = _sandbox_prelude(sandbox, jail)
    effective_prelude = (guard + " " + prelude).strip() if prelude else guard

    started = time.perf_counter()

    def _result(ok, stdout, return_value, error, timed_out,
                candidate_ms=None, error_type="", return_truncated=False):
        return {
            "ok": ok,
            "stdout": stdout,
            "return_value": return_value,
            "error": error,
            "timed_out": timed_out,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "candidate_ms": candidate_ms,
            "error_type": error_type,
            "return_truncated": return_truncated,
        }

    exe = resolve_sbcl(sbcl_exe)
    if not os.path.exists(exe):
        return _result(False, "", "",
                       "sbcl executable not found: %s" % exe, False)

    fingerprint = generation_fingerprint(epoch_id)
    script = _build_script(code, effective_prelude, fingerprint)

    fd, script_path = tempfile.mkstemp(suffix=".lisp", prefix="graygoo-worker-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        # NB: --dynamic-space-size is a C runtime option and must precede
        # all Lisp options, else SBCL refuses to start.
        argv = [exe, "--dynamic-space-size", str(memory_mb),
               "--non-interactive", "--no-userinit", "--no-sysinit",
               "--disable-debugger", "--load", script_path]
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                cwd=jail_root or None,
                env=_sanitized_env(),
            )
        except OSError as exc:
            return _result(False, "", "",
                           "failed to spawn sbcl: %s" % exc, False)
        try:
            out_bytes, err_bytes = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            out_bytes, err_bytes = proc.communicate()
            return _result(
                False, "", "",
                "wall-clock timeout after %gs; worker killed and recycled"
                % timeout_s, True)
        stdout_text = out_bytes.decode("utf-8", errors="replace")
        stderr_text = err_bytes.decode("utf-8", errors="replace")
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass

    envelope = _parse_envelope(stdout_text)
    if envelope is None:
        error = "worker produced no result envelope (exit code %s)" % proc.returncode
        tail = stderr_text.strip()
        if tail:
            # Head AND tail: fatal runtime conditions (e.g. SBCL's
            # "Heap exhausted during garbage collection") print at
            # stderr's head, while backtraces trail at the end.
            head = tail[:MAX_STDERR_HEAD_CHARS]
            tail = tail[-MAX_STDERR_TAIL_CHARS:]
            error += "; stderr head: " + head
            if tail != head:
                error += " ... stderr tail: " + tail
        return _result(False, "", "", error, False)
    if "__malformed__" in envelope:
        return _result(False, "", "",
                       "malformed result envelope: %r" % envelope["__malformed__"],
                       False)

    ok = bool(envelope.get("ok", False))
    stdout = _truncate(str(envelope.get("stdout", "")), MAX_STDOUT_CHARS)
    return_value = str(envelope.get("return_value", "")) if ok else ""
    error = "" if ok else str(envelope.get("error", ""))
    backtrace = _truncate(str(envelope.get("backtrace", "")), MAX_BACKTRACE_CHARS)
    if not ok and backtrace:
        error = (error + "\n--- backtrace ---\n" + backtrace) if error else backtrace
    if str(envelope.get("fingerprint", "")) != fingerprint:
        ok = False
        return_value = ""
        error = ("generation fingerprint mismatch: expected %r, worker ran %r"
                 % (fingerprint, envelope.get("fingerprint", "")))
    raw_candidate = envelope.get("candidate_ms")
    candidate_ms = None
    if isinstance(raw_candidate, bool):
        pass
    elif isinstance(raw_candidate, (int, float)) and raw_candidate >= 0:
        candidate_ms = float(raw_candidate)
    raw_truncated = envelope.get("return_truncated", False)
    return_truncated = (raw_truncated is True
                        or (isinstance(raw_truncated, str)
                            and raw_truncated.strip().lower() == "true"))
    return _result(ok, stdout, return_value, error, False,
                   candidate_ms=candidate_ms,
                   error_type=str(envelope.get("error_type", "") or ""),
                   return_truncated=return_truncated)


class WorkerPool:
    """Prewarmed pool of SBCL rehearsal workers (plan.md section 20).

    Each job runs in a fresh child process via :func:`run_lisp`;
    ``size`` bounds how many run concurrently. Broken workers
    (timeout/crash) are killed by :func:`run_lisp` and counted as
    recycled in :attr:`stats`, since no process handle is ever reused.

    :attr:`prewarm_evidence` records cold-vs-warm start evidence:
    ``{"cold_ms", "warm_ms", "slots", "all_ok"}``. A failed prewarm
    does not raise; ``all_ok`` is False and jobs will report their own
    errors.
    """

    def __init__(self, size, *, epoch_id="", sbcl_exe=None, memory_mb=512,
                 timeout_s=10.0, sandbox=True, jail=None):
        if size is None or int(size) < 1:
            raise ValueError("size must be a positive int, got %r" % (size,))
        self.size = int(size)
        self.epoch_id = epoch_id
        self.sbcl_exe = sbcl_exe
        self.memory_mb = memory_mb
        self.timeout_s = timeout_s
        self.sandbox = sandbox
        self.jail = jail
        self._semaphore = threading.Semaphore(self.size)
        self._lock = threading.Lock()
        self._active = 0
        self._shutdown = False
        #: Job counters; mutated under a lock as jobs finish.
        self.stats = {"submitted": 0, "completed": 0, "timed_out": 0,
                      "failed": 0, "recycled": 0}
        cold = self._warmup_run()
        warms = [self._warmup_run() for _ in range(self.size - 1)]
        #: Cold-vs-warm start evidence from construction-time warmups.
        self.prewarm_evidence = {
            "cold_ms": cold["elapsed_ms"],
            "warm_ms": [w["elapsed_ms"] for w in warms],
            "slots": self.size,
            "all_ok": bool(cold["ok"] and all(w["ok"] for w in warms)),
        }

    def _warmup_run(self):
        return run_lisp("(+ 1 2)", timeout_s=self.timeout_s,
                        memory_mb=self.memory_mb, epoch_id=self.epoch_id,
                        sbcl_exe=self.sbcl_exe, sandbox=self.sandbox,
                        jail=self.jail)

    def run(self, code, *, timeout_s=None, memory_mb=None, prelude="",
            epoch_id=None, sandbox=None, jail=None):
        """Run CODE on the pool; same result dict as :func:`run_lisp`.

        ``None`` options fall back to the pool defaults. Raises
        RuntimeError after :meth:`shutdown`.
        """
        with self._lock:
            if self._shutdown:
                raise RuntimeError("WorkerPool is shut down")
            self.stats["submitted"] += 1
            self._active += 1
        self._semaphore.acquire()
        try:
            result = run_lisp(
                code,
                timeout_s=self.timeout_s if timeout_s is None else timeout_s,
                memory_mb=self.memory_mb if memory_mb is None else memory_mb,
                prelude=prelude,
                epoch_id=self.epoch_id if epoch_id is None else epoch_id,
                sbcl_exe=self.sbcl_exe,
                sandbox=self.sandbox if sandbox is None else sandbox,
                jail=self.jail if jail is None else jail,
            )
        finally:
            self._semaphore.release()
            with self._lock:
                self._active -= 1
                self.stats["completed"] += 1
                if result["timed_out"]:
                    self.stats["timed_out"] += 1
                    self.stats["recycled"] += 1
                elif not result["ok"]:
                    self.stats["failed"] += 1
                    if result["error"].startswith(
                            ("worker produced no result envelope",
                             "generation fingerprint mismatch")):
                        self.stats["recycled"] += 1
        return result

    def shutdown(self):
        """Reject new jobs and wait for in-flight ones (bounded by their
        own timeouts)."""
        with self._lock:
            self._shutdown = True
        while True:
            with self._lock:
                if self._active == 0:
                    return
            time.sleep(0.01)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.shutdown()
        return False
