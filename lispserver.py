"""A long-lived SBCL process for calling already-defined pure functions fast.

``workers.run_lisp`` starts a fresh SBCL for every evaluation (about 100-200
ms). A mounted app calls the same saved functions on every request, so this
module keeps ONE child with those definitions loaded and evaluates one form
per request in a few milliseconds.

Wire protocol (one line each way, so arbitrary output cannot break framing):

* request:  the form's UTF-8 text, hex-encoded, then a newline
* reply:    ``GGR <ok|err> <hex of value or error text> <hex of captured stdout>``

The child reads with ``*read-eval*`` NIL, captures ``*standard-output*`` per
request, and catches every ERROR, so a failing form never kills it.

Containment: the same sandbox prelude ``workers.run_lisp`` applies is loaded
before the definitions, and the child gets the same sanitised environment.
What differs from a fresh worker: state lives across requests (a request could
redefine a function for later ones). Intended callers evaluate calls of pure
saved functions only; ``eval(..., fresh=True)`` restarts the child first.
"""
import atexit
import binascii
import os
import queue
import subprocess
import tempfile
import threading
import time

import workers

_LOOP = r"""
(defvar *gg-load-error* nil)
(defun gg-hex (string)
  (let ((octets (sb-ext:string-to-octets string :external-format :utf-8)))
    (with-output-to-string (out)
      (loop for o across octets do (format out "~(~2,'0x~)" o)))))
(defun gg-unhex (hex)
  (let ((octets (make-array (floor (length hex) 2) :element-type '(unsigned-byte 8))))
    (dotimes (i (length octets))
      (setf (aref octets i) (parse-integer hex :start (* 2 i) :end (+ 2 (* 2 i)) :radix 16)))
    (sb-ext:octets-to-string octets :external-format :utf-8)))
(defun gg-reply (tag text printed)
  (format *terminal-io* "GGR ~a ~a ~a~%" tag (gg-hex text) (gg-hex printed))
  (finish-output *terminal-io*))
(defun gg-serve ()
  (gg-reply (if *gg-load-error* "err" "ok") (or *gg-load-error* "ready") "")
  (loop
    (let ((line (read-line *standard-input* nil :eof)))
      (when (eq line :eof) (return))
      (let ((out (make-string-output-stream)))
        (handler-case
            (let* ((form (let ((*read-eval* nil)) (read-from-string (gg-unhex line))))
                   (value (let ((*standard-output* out)) (eval form))))
              (gg-reply "ok"
                        (let ((*print-pretty* nil) (*print-length* nil) (*print-level* nil))
                          (prin1-to-string value))
                        (get-output-stream-string out)))
          (error (c)
            (gg-reply "err" (princ-to-string c) (get-output-stream-string out))))))))
"""


def _hex(text):
    return binascii.hexlify(text.encode("utf-8")).decode("ascii")


def _unhex(hexed):
    return binascii.unhexlify(hexed).decode("utf-8", errors="replace")


class LispServer:
    """One SBCL child with PRELUDE loaded; ``eval`` runs one form in it."""

    def __init__(self, prelude="", timeout_s=10.0, sandbox=True, sbcl_exe=None):
        self.prelude = prelude
        self.timeout_s = timeout_s
        self.sandbox = sandbox
        self.sbcl_exe = sbcl_exe
        self.proc = None
        self._lines = None
        self._files = []
        self._lock = threading.Lock()
        self._load_error = ""
        atexit.register(self.close)

    # -- child lifecycle ---------------------------------------------------
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".lisp", prefix="graygoo-server-")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        self._files.append(path)
        return path.replace("\\", "/")

    def _start(self):
        """Spawn the child and wait for its ready line. Returns an error text or ''."""
        self._stop()
        exe = workers.resolve_sbcl(self.sbcl_exe)
        if not os.path.exists(exe):
            return "sbcl executable not found: %s" % exe
        guard = workers._sandbox_prelude(self.sandbox, None)
        # the loop is defined first (trusted), then containment, then the definitions
        root = workers.REPO_ROOT.replace("\\", "/")
        # the sandbox guard lives in the worker kernel's package, so load that first
        kernel = ("(let ((*load-verbose* nil) (*load-print* nil))\n"
                  "  (load \"%s/src/worker/packages.lisp\")\n"
                  "  (load \"%s/src/worker/worker.lisp\"))" % (root, root)) if guard else ""
        # The guard forbids LOAD once installed, so the definitions are read from
        # text and evaluated form by form, the way the worker treats candidate code.
        script = self._write(
            "%s\n%s\n%s\n(handler-case\n"
            "    (with-input-from-string (in \"%s\")\n"
            "      (let ((*read-eval* nil))\n"
            "        (loop for form = (read in nil :gg-eof)\n"
            "              until (eq form :gg-eof) do (eval form))))\n"
            "  (error (c) (setf *gg-load-error* (princ-to-string c))))\n(gg-serve)\n"
            % (_LOOP, kernel, guard, workers._lisp_escape(self.prelude or "")))
        try:
            self.proc = subprocess.Popen(
                [exe, "--dynamic-space-size", "512", "--no-userinit", "--no-sysinit",
                 "--disable-debugger", "--load", script],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=workers._sanitized_env())
        except OSError as exc:
            return "failed to spawn sbcl: %s" % exc
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc, self._lines), daemon=True).start()
        reply = self._read(max(self.timeout_s, 15.0))
        if reply is None:
            self._stop()
            return "the Lisp server did not start"
        self._load_error = "" if reply[0] == "ok" else reply[1]
        return ""

    @staticmethod
    def _pump(proc, lines):
        for raw in iter(proc.stdout.readline, b""):
            lines.put(raw.decode("ascii", errors="replace").strip())
        lines.put(None)                                  # the child is gone

    def _read(self, timeout):
        """The next framed reply as ``(tag, text, printed)``, or None on timeout/death."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                line = self._lines.get(timeout=left)
            except queue.Empty:
                return None
            if line is None:
                return None
            if line.startswith("GGR "):
                parts = line.split(" ")
                if len(parts) == 4 or len(parts) == 3:
                    tag, text = parts[1], _unhex(parts[2])
                    return tag, text, _unhex(parts[3]) if len(parts) == 4 else ""

    def _stop(self):
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if proc is not None:
            for pipe in (proc.stdin, proc.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass
        for path in self._files:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._files = []

    # -- public ------------------------------------------------------------
    def eval(self, code, fresh=False):
        """Evaluate one form; same envelope keys as ``workers.run_lisp``."""
        started = time.perf_counter()

        def result(ok, value="", error="", stdout="", timed_out=False):
            return {"ok": ok, "stdout": stdout, "return_value": value, "error": error,
                    "timed_out": timed_out, "return_truncated": False,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1)}
        with self._lock:
            if fresh or self.proc is None or self.proc.poll() is not None:
                problem = self._start()
                if problem:
                    return result(False, error=problem)
            if self._load_error:
                return result(False, error="the definitions failed to load: " + self._load_error)
            try:
                self.proc.stdin.write((_hex(code) + "\n").encode("ascii"))
                self.proc.stdin.flush()
            except OSError as exc:
                self._stop()
                return result(False, error="the Lisp server stopped: %s" % exc)
            reply = self._read(self.timeout_s)
            if reply is None:
                died = self.proc is not None and self.proc.poll() is not None
                self._stop()                             # a fresh child serves the next call
                if died:
                    return result(False, error="the Lisp server stopped during the call")
                return result(False, error="wall-clock timeout after %gs; server restarted"
                              % self.timeout_s, timed_out=True)
            tag, text, printed = reply
            if tag == "ok":
                return result(True, value=text, stdout=printed)
            return result(False, error=text, stdout=printed)

    def extend(self, more):
        """Add definitions to the loaded ones WITHOUT a restart. True when the child took them.

        The prelude grows either way, so a later restart (timeout, crash) loads
        everything. On any trouble the child is stopped and the next call starts
        a clean one from the full prelude; nothing half-loaded is ever served.
        """
        with self._lock:
            self.prelude = (self.prelude or "") + more
            if self.proc is None or self.proc.poll() is not None or self._load_error:
                self._stop()
                self._load_error = ""
                return False
            try:
                self.proc.stdin.write((_hex("(progn %s\n t)" % more) + "\n").encode("ascii"))
                self.proc.stdin.flush()
                reply = self._read(self.timeout_s)
            except OSError:
                reply = None
            if reply is None or reply[0] != "ok":
                self._stop()
                return False
            return True

    def reload(self, prelude):
        """Replace the loaded definitions (restarts the child on the next call)."""
        with self._lock:
            self.prelude = prelude
            self._load_error = ""
            self._stop()

    def close(self):
        with self._lock:
            self._stop()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


_CACHE = {}                 # key -> LispServer, newest last
_CACHE_LOCK = threading.Lock()
CACHE_MAX = 6               # two build sessions (with and without memory) plus mounted apps


def cached_server(key, prelude, timeout_s=10.0):
    """A server for KEY with PRELUDE loaded, at most CACHE_MAX alive.

    A prelude that only GREW since the last call (new definitions appended) is
    added to the running child; any other change restarts it.
    """
    with _CACHE_LOCK:
        srv = _CACHE.pop(key, None)
        if srv is None:
            srv = LispServer(prelude, timeout_s=timeout_s)
        elif srv.prelude != prelude:
            old = srv.prelude or ""
            if not (old and prelude.startswith(old) and srv.extend(prelude[len(old):])):
                srv.reload(prelude)
        _CACHE[key] = srv
        while len(_CACHE) > CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE))).close()
        return srv


def drop(key):
    """Stop and forget the server for KEY, if there is one."""
    with _CACHE_LOCK:
        srv = _CACHE.pop(key, None)
    if srv is not None:
        srv.close()


def close_all():
    """Stop every cached server."""
    with _CACHE_LOCK:
        for srv in _CACHE.values():
            srv.close()
        _CACHE.clear()
