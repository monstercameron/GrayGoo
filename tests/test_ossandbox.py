"""Tests for the OS-level boundary around SBCL children (ossandbox.py).

Windows-only checks skip with a reason when ossandbox.available() is False or
SBCL is missing. Children run the base interpreter (sys.base_prefix), never the
venv launcher: the launcher starts a second process, which the job's
one-process limit correctly blocks. Every temp folder is removed afterwards.
"""

import ctypes
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ossandbox  # noqa: E402
import workers  # noqa: E402

BASE_PY = os.path.join(sys.base_prefix, "python.exe")
ALL_MECHANISMS = ("job-object", "low-integrity", "handle-list")
SANDBOX_AVAILABLE, SANDBOX_REASON = ossandbox.available()
SBCL_AVAILABLE = os.path.exists(workers.resolve_sbcl())
WINDOWS_SANDBOX = sys.platform == "win32" and SANDBOX_AVAILABLE and os.path.exists(BASE_PY)
SKIP_WINDOWS = ("OS sandbox not available here: %s" % SANDBOX_REASON
                if not WINDOWS_SANDBOX else "")
SKIP_SBCL = "SBCL not found at %s" % workers.resolve_sbcl()


def _child(code, *args, timeout=60, keep=False):
    """Run CODE with the base interpreter inside the boundary; return (proc, stdout, stderr).

    Unless keep is True the child is released before returning, so its Low folder is removed.
    """
    proc = ossandbox.popen([BASE_PY, "-c", code] + list(args),
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
    out, err = proc.communicate(timeout=timeout)
    if not keep:
        ossandbox.release(proc)
    return proc, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


def _pid_alive(pid):
    """Whether PID is still running (Windows only)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, 0, pid)   # SYNCHRONIZE | QUERY_LIMITED
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259                                 # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


WRITE_CHILD = (
    "import sys\n"
    "path = sys.argv[1]\n"
    "try:\n"
    "    with open(path, 'w') as handle:\n"
    "        handle.write('x')\n"
    "    print('WRITE OK')\n"
    "except OSError as exc:\n"
    "    print('WRITE DENIED %s' % exc.errno)\n"
)

LOW_WRITE_CHILD = (
    "import os\n"
    "temp = os.environ.get('TEMP', '')\n"
    "print('TEMP=' + temp)\n"
    "try:\n"
    "    with open(os.path.join(temp, 'inside.txt'), 'w') as handle:\n"
    "        handle.write('x')\n"
    "    print('LOW WRITE OK')\n"
    "except OSError as exc:\n"
    "    print('LOW WRITE DENIED %s' % exc.errno)\n"
)

SPAWN_CHILD = (
    "import subprocess, sys\n"
    "try:\n"
    "    subprocess.run([sys.argv[1], '/c', 'echo', 'hi'], check=True)\n"
    "    print('SPAWN OK')\n"
    "except Exception as exc:\n"
    "    print('SPAWN DENIED %s' % type(exc).__name__)\n"
)

ALLOCATE_CHILD = (
    "blob = b'\\x01' * (400 * 1024 * 1024)\n"
    "print('ALLOCATED', len(blob))\n"
)

SLEEP_CHILD = "import time\ntime.sleep(120)\n"


class PortableBehaviourTests(unittest.TestCase):
    """Runs on every platform: switches, fallback records and status()."""

    def test_disabled_by_env_gives_plain_popen(self):
        with mock.patch.dict(os.environ, {ossandbox.ENV_VAR: "0"}):
            proc = ossandbox.popen([sys.executable, "-c", "pass"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                self.assertIsInstance(proc, subprocess.Popen)
            finally:
                proc.wait(timeout=60)
            state = ossandbox.status()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["mechanisms"], [])
        self.assertIn("disabled", state["fallback"])

    def test_strict_raises_when_sandbox_unavailable(self):
        with mock.patch.object(ossandbox, "available", return_value=(False, "forced off")):
            with self.assertRaises(ossandbox.SandboxError):
                ossandbox.popen([sys.executable, "-c", "pass"], strict=True)
            with mock.patch.dict(os.environ, {ossandbox.ENV_VAR: "strict"}):
                with self.assertRaises(ossandbox.SandboxError):
                    ossandbox.popen([sys.executable, "-c", "pass"])

    def test_default_mode_falls_back_and_records_why(self):
        with mock.patch.object(ossandbox, "available", return_value=(False, "forced off")), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ossandbox.ENV_VAR, None)
            proc = ossandbox.popen([sys.executable, "-c", "pass"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                self.assertIsInstance(proc, subprocess.Popen)
            finally:
                proc.wait(timeout=60)
            state = ossandbox.status()
        self.assertEqual(state["fallback"], "forced off")
        self.assertEqual(state["mechanisms"], [])

    def test_unsupported_argument_falls_back_or_raises_in_strict(self):
        with mock.patch.object(ossandbox, "available", return_value=(True, "forced on")):
            with self.assertRaises(ossandbox.SandboxError):
                ossandbox.popen([sys.executable, "-c", "pass"], strict=True, text=True)
        proc = ossandbox.popen([sys.executable, "-c", "pass"], text=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertIsInstance(proc, subprocess.Popen)
        finally:
            proc.wait(timeout=60)
        self.assertIn("not supported", ossandbox.status()["fallback"])

    def test_status_shape(self):
        state = ossandbox.status()
        for key in ("enabled", "mode", "mechanisms", "fallback", "not_enforced"):
            self.assertIn(key, state)
        self.assertTrue(any("network" in item for item in state["not_enforced"]))


@unittest.skipUnless(WINDOWS_SANDBOX, SKIP_WINDOWS or "sandbox unavailable")
class WindowsBoundaryTests(unittest.TestCase):
    """The real Win32 boundary on this machine."""

    def setUp(self):
        self.medium = tempfile.mkdtemp(prefix="gg-ossb-medium-")

    def tearDown(self):
        for path in (os.path.join(self.medium, "probe.txt"),):
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            os.rmdir(self.medium)
        except OSError:
            pass

    def test_write_into_medium_folder_is_denied(self):
        target = os.path.join(self.medium, "probe.txt")
        proc, out, _ = _child(WRITE_CHILD, target)
        self.assertIn("WRITE DENIED", out)
        self.assertFalse(os.path.exists(target))
        self.assertIn("low-integrity", proc.mechanisms)

    def test_write_into_repo_tests_folder_is_denied(self):
        target = str(ROOT / "tests" / ("ossb-probe-%d.txt" % os.getpid()))
        self.addCleanup(lambda: os.path.exists(target) and os.unlink(target))
        proc, out, _ = _child(WRITE_CHILD, target)
        self.assertIn("WRITE DENIED", out)
        self.assertFalse(os.path.exists(target))

    def test_write_into_low_temp_folder_succeeds(self):
        proc, out, _ = _child(LOW_WRITE_CHILD, keep=True)
        self.assertIn("LOW WRITE OK", out)
        temp_line = [line for line in out.splitlines() if line.startswith("TEMP=")][0]
        low_dir = temp_line[len("TEMP="):]
        try:
            self.assertTrue(os.path.exists(os.path.join(low_dir, "inside.txt")))
        finally:
            proc.close()
        self.assertFalse(os.path.exists(low_dir))

    def test_child_cannot_start_another_process(self):
        cmd = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
        proc, out, _ = _child(SPAWN_CHILD, cmd)
        self.assertIn("SPAWN DENIED", out)
        self.assertNotIn("hi", out)
        self.assertNotIn("SPAWN OK", out)

    def test_memory_limit_stops_large_allocation(self):
        proc = ossandbox.popen([BASE_PY, "-c", ALLOCATE_CHILD], memory_mb=200,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        out, _ = proc.communicate(timeout=60)
        proc.close()
        self.assertNotIn(b"ALLOCATED", out)
        self.assertNotEqual(proc.returncode, 0)

    def test_closing_the_job_kills_the_child(self):
        proc = ossandbox.popen([BASE_PY, "-c", SLEEP_CHILD],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        self.addCleanup(proc.close)
        started = time.monotonic()
        proc.close_job()
        proc.wait(timeout=20)
        # The child sleeps for 120 s, so ending within 20 s means the job killed it.
        # KILL_ON_JOB_CLOSE reports exit code 0 on this machine, so the code is not checked.
        self.assertLess(time.monotonic() - started, 20)
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(_pid_alive(proc.pid))

    def test_parent_exit_kills_the_child(self):
        middle = (
            "import os, subprocess, sys\n"
            "sys.path.insert(0, %r)\n"
            "import ossandbox\n"
            "child = ossandbox.popen([%r, '-c', 'import time; time.sleep(120)'],\n"
            "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "print(child.pid, flush=True)\n"
            "os._exit(0)\n" % (str(ROOT), BASE_PY))
        outer = subprocess.run([BASE_PY, "-c", middle], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=60)
        pid = int(outer.stdout.decode().split()[0])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.2)
        self.assertFalse(_pid_alive(pid), "child %d survived its parent" % pid)

    def test_status_reports_what_was_applied(self):
        proc = ossandbox.popen([BASE_PY, "-c", "print(1)"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.communicate(timeout=60)
        proc.close()
        state = ossandbox.status()
        self.assertEqual(tuple(state["mechanisms"]), ALL_MECHANISMS)
        self.assertIsNone(state["fallback"])
        self.assertEqual(tuple(proc.mechanisms), ALL_MECHANISMS)

    def test_release_removes_the_low_folder_and_job(self):
        # Only the folder each child was given is checked: other live children in
        # this process may create and remove their own folders at any time.
        for _ in range(3):
            proc = ossandbox.popen([BASE_PY, "-c", "import os; print(os.environ['TEMP'])"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
            out, _ = proc.communicate(timeout=60)
            low_dir = out.decode("utf-8", errors="replace").strip()
            self.assertTrue(os.path.isdir(low_dir))
            ossandbox.release(proc)
            self.assertIsNone(proc._job)
            self.assertFalse(os.path.exists(low_dir))

    def test_forced_mechanism_failure_is_dropped_in_default_mode(self):
        forced = ossandbox._Mechanism("job-object", "forced failure for the test")
        with mock.patch.object(ossandbox, "_job_object", side_effect=forced), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ossandbox.ENV_VAR, None)
            proc = ossandbox.popen([BASE_PY, "-c", "print('still runs')"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
            out, _ = proc.communicate(timeout=60)
            proc.close()
        self.assertIn(b"still runs", out)
        self.assertEqual(tuple(proc.mechanisms), ("low-integrity", "handle-list"))
        self.assertIn("forced failure", ossandbox.status()["fallback"])

    def test_forced_mechanism_failure_raises_in_strict_mode(self):
        forced = ossandbox._Mechanism("low-integrity", "forced failure for the test")
        with mock.patch.object(ossandbox, "_low_token", side_effect=forced):
            with self.assertRaises(ossandbox.SandboxError):
                ossandbox.popen([BASE_PY, "-c", "pass"], strict=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with mock.patch.dict(os.environ, {ossandbox.ENV_VAR: "strict"}):
                with self.assertRaises(ossandbox.SandboxError):
                    ossandbox.popen([BASE_PY, "-c", "pass"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@unittest.skipUnless(WINDOWS_SANDBOX and SBCL_AVAILABLE,
                     SKIP_WINDOWS or SKIP_SBCL)
class SbclBoundaryTests(unittest.TestCase):
    """Real SBCL children; the Lisp-level lockdown is bypassed with sandbox=False."""

    def test_warm_server_runs_inside_the_boundary(self):
        import lispserver
        srv = lispserver.LispServer("(defun sq (x) (* x x))", timeout_s=20.0)
        try:
            self.assertEqual(srv.eval("(sq 7)")["return_value"], "49")
            self.assertEqual(tuple(srv.proc.mechanisms), ALL_MECHANISMS)
        finally:
            srv.close()

    def test_cold_worker_runs_inside_the_boundary(self):
        result = workers.run_lisp("(+ 1 2)", timeout_s=30.0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["return_value"], "3")
        self.assertEqual(tuple(ossandbox.status()["mechanisms"]), ALL_MECHANISMS)

    def test_lisp_write_into_repo_is_refused_by_the_os_alone(self):
        target = (ROOT / "tests" / ("ossb-sbcl-probe-%d.txt" % os.getpid()))
        self.addCleanup(lambda: target.exists() and target.unlink())
        code = ('(with-open-file (s "%s" :direction :output :if-exists :supersede) '
                '(write-line "pwned" s))' % str(target).replace("\\", "/"))
        result = workers.run_lisp(code, sandbox=False, timeout_s=30.0)
        self.assertFalse(result["ok"])
        self.assertFalse(target.exists())

    def test_lisp_write_into_medium_temp_is_refused_by_the_os_alone(self):
        folder = tempfile.mkdtemp(prefix="gg-ossb-sbcl-")
        self.addCleanup(lambda: os.path.isdir(folder) and os.rmdir(folder))
        target = os.path.join(folder, "probe.txt")
        code = ('(with-open-file (s "%s" :direction :output :if-exists :supersede) '
                '(write-line "pwned" s))' % target.replace("\\", "/"))
        result = workers.run_lisp(code, sandbox=False, timeout_s=30.0)
        self.assertFalse(result["ok"])
        self.assertFalse(os.path.exists(target))

    def test_lisp_run_program_is_refused_by_the_os_alone(self):
        code = ('(sb-ext:run-program "C:/Windows/System32/cmd.exe" '
                '(list "/c" "echo" "hi") :output t)')
        result = workers.run_lisp(code, sandbox=False, timeout_s=30.0)
        self.assertFalse(result["ok"])
        self.assertNotIn("hi", result["stdout"])


if __name__ == "__main__":
    unittest.main()
