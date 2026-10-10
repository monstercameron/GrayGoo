"""OS-level boundary for the SBCL child processes that run model-written Lisp.

The Lisp-level lockdown (src/worker/worker.lisp) and the trust lint are speed
bumps: code the model writes can reach SB-ALIEN and SB-UNIX internals. This
module starts those children inside a Windows boundary built directly on the
Win32 API through ctypes (stdlib only; no pywin32).

Mechanisms, each independently optional and each reported in status():

* job-object: the child is assigned to a Job Object with KILL_ON_JOB_CLOSE,
  an active-process limit of 1 (it cannot start another process), a process
  memory limit, and UI restrictions (no clipboard, desktop, system-parameter
  or exit-windows access; no global atoms; no handles to other jobs' UI).
  The child is created suspended, assigned to the job, and only then resumed,
  so it never runs an instruction outside the job.
* low-integrity: the child runs with a duplicate of this process's token whose
  mandatory integrity level is Low (S-1-16-4096). Low processes cannot write
  to Medium objects such as the user's profile and the repository. TEMP and
  TMP point at a fresh folder labelled Low under the system temp directory.
* handle-list: the child inherits only the three standard pipe handles
  (PROC_THREAD_ATTRIBUTE_HANDLE_LIST), never any other inheritable handle.

NOT enforced: outbound network (only AppContainer can block it, not attempted
here); reads of files the user can read (Low integrity still reads Medium
files); the profile's LocalLow folder (writable at Low integrity by Windows
design); CPU and wall-clock limits (callers keep their own timeouts).

Environment variable GRAYGOO_OS_SANDBOX: 0, off, false or no starts plain
processes; strict makes any failure to apply a mechanism raise SandboxError;
anything else (the default) applies what it can, drops a mechanism that fails,
and starts a plain process when no mechanism works. The reason is recorded in
status()["fallback"].

Callers use :func:`popen` where they used subprocess.Popen. The result is a
:class:`SandboxedProcess` on Windows (the subset of the Popen interface the
GrayGoo callers use) or a real subprocess.Popen otherwise.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

__all__ = [
    "DEFAULT_MEMORY_MB",
    "ENV_VAR",
    "MECHANISMS",
    "NOT_ENFORCED",
    "SandboxError",
    "SandboxedProcess",
    "available",
    "popen",
    "release",
    "status",
]

ENV_VAR = "GRAYGOO_OS_SANDBOX"
DEFAULT_MEMORY_MB = 1024
MECHANISMS = ("job-object", "low-integrity", "handle-list")
NOT_ENFORCED = (
    "outbound network (blocking it needs AppContainer; not attempted)",
    "reads of files the user can read (Low integrity still reads Medium files)",
    "the profile LocalLow folder (writable at Low integrity by Windows design)",
    "CPU time and wall-clock limits (callers keep their own timeouts)",
)

# Keyword arguments the Windows path understands; anything else falls back.
_SUPPORTED_KWARGS = frozenset({"stdin", "stdout", "stderr", "cwd", "env",
                               "creationflags", "close_fds"})

#: Reason the last child was started without some mechanism, or None.
last_fallback = None
_last_mechanisms = ()
_state_lock = threading.Lock()
_probe_result = None
_api_cache = None

# Win32 constants (values from the SDK headers).
_TOKEN_DUP_ADJ = 0x0002 | 0x0008 | 0x0080 | 0x0001  # duplicate, query, adjust default, assign primary
_TOKEN_ALL_ACCESS = 0xF01FF
_SECURITY_IMPERSONATION = 2
_TOKEN_PRIMARY = 1
_TOKEN_INTEGRITY_LEVEL = 25
_SE_GROUP_INTEGRITY = 0x20
_SE_FILE_OBJECT = 1
_LABEL_SECURITY_INFORMATION = 0x10
_SDDL_REVISION_1 = 1
_JOB_EXTENDED_LIMIT_CLASS = 9
_JOB_UI_RESTRICTIONS_CLASS = 4
_JOB_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_LIMIT_KILL_ON_CLOSE = 0x00002000
_JOB_UI_ALL = 0x000000FF  # every JOB_OBJECT_UILIMIT_* flag
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_STARTF_USESTDHANDLES = 0x00000100
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NO_WINDOW = 0x08000000
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_HANDLE_FLAG_INHERIT = 0x00000001
_DUPLICATE_SAME_ACCESS = 0x00000002
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ_WRITE = 0x00000003
_OPEN_EXISTING = 3
_STD_INPUT = 0xFFFFFFF6   # -10
_STD_OUTPUT = 0xFFFFFFF5  # -11
_STD_ERROR = 0xFFFFFFF4   # -12
_STD_IDS = {"stdin": _STD_INPUT, "stdout": _STD_OUTPUT, "stderr": _STD_ERROR}
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_INFINITE = 0xFFFFFFFF
_STILL_ACTIVE_TICKS = 2000


class SandboxError(OSError):
    """The OS boundary could not be applied and strict mode forbids running without it.

    Subclasses OSError so callers that already handle spawn failures
    (``except OSError``) report it instead of crashing.
    """


class _Mechanism(Exception):
    """One mechanism could not be set up; ``name`` says which one."""

    def __init__(self, name, detail):
        super().__init__("%s: %s" % (name, detail))
        self.name = name


class _Unsupported(Exception):
    """A Popen argument the sandboxed path does not implement."""


def _mode():
    raw = os.environ.get(ENV_VAR, "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return "off"
    if raw == "strict":
        return "strict"
    return "on"


def _winerror():
    return ctypes.get_last_error()


def _api():
    """Bind the Win32 functions and structures used here (cached). Windows only."""
    global _api_cache
    if _api_cache is not None:
        return _api_cache
    from ctypes import wintypes
    from types import SimpleNamespace

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD),
                    ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD),
                    ("lpReserved", ctypes.c_void_p),
                    ("lpDesktop", ctypes.c_void_p),
                    ("lpTitle", ctypes.c_void_p),
                    ("dwX", wintypes.DWORD),
                    ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD),
                    ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD),
                    ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p),
                    ("hStdInput", ctypes.c_void_p),
                    ("hStdOutput", ctypes.c_void_p),
                    ("hStdError", ctypes.c_void_p)]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFOW),
                    ("lpAttributeList", ctypes.c_void_p)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", ctypes.c_void_p),
                    ("hThread", ctypes.c_void_p),
                    ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD)]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
        _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    VP = ctypes.c_void_p
    DW = wintypes.DWORD
    BO = wintypes.BOOL
    WSTR = wintypes.LPCWSTR
    INT = ctypes.c_int
    SZ = ctypes.c_size_t

    def bind(dll, name, restype, *argtypes):
        function = getattr(dll, name)
        function.restype = restype
        function.argtypes = list(argtypes)
        return function

    _api_cache = SimpleNamespace(
        SECURITY_ATTRIBUTES=SECURITY_ATTRIBUTES,
        STARTUPINFOEXW=STARTUPINFOEXW,
        PROCESS_INFORMATION=PROCESS_INFORMATION,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION=JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOBOBJECT_BASIC_UI_RESTRICTIONS=JOBOBJECT_BASIC_UI_RESTRICTIONS,
        TOKEN_MANDATORY_LABEL=TOKEN_MANDATORY_LABEL,
        CreateJobObjectW=bind(k32, "CreateJobObjectW", VP, VP, WSTR),
        SetInformationJobObject=bind(k32, "SetInformationJobObject", BO, VP, INT, VP, DW),
        AssignProcessToJobObject=bind(k32, "AssignProcessToJobObject", BO, VP, VP),
        CloseHandle=bind(k32, "CloseHandle", BO, VP),
        CreatePipe=bind(k32, "CreatePipe", BO, VP, VP, VP, DW),
        SetHandleInformation=bind(k32, "SetHandleInformation", BO, VP, DW, DW),
        CreateFileW=bind(k32, "CreateFileW", VP, WSTR, DW, DW, VP, DW, DW, VP),
        GetStdHandle=bind(k32, "GetStdHandle", VP, DW),
        DuplicateHandle=bind(k32, "DuplicateHandle", BO, VP, VP, VP, VP, DW, BO, DW),
        CreateProcessW=bind(k32, "CreateProcessW", BO, WSTR, VP, VP, VP, BO, DW,
                            VP, WSTR, VP, VP),
        CreateProcessAsUserW=bind(k32, "CreateProcessAsUserW", BO, VP, WSTR, VP, VP,
                                  VP, BO, DW, VP, WSTR, VP, VP),
        InitializeProcThreadAttributeList=bind(k32, "InitializeProcThreadAttributeList",
                                               BO, VP, DW, DW, VP),
        UpdateProcThreadAttribute=bind(k32, "UpdateProcThreadAttribute", BO, VP, DW,
                                       SZ, VP, SZ, VP, VP),
        DeleteProcThreadAttributeList=bind(k32, "DeleteProcThreadAttributeList", None, VP),
        ResumeThread=bind(k32, "ResumeThread", DW, VP),
        TerminateProcess=bind(k32, "TerminateProcess", BO, VP, DW),
        WaitForSingleObject=bind(k32, "WaitForSingleObject", DW, VP, DW),
        GetExitCodeProcess=bind(k32, "GetExitCodeProcess", BO, VP, VP),
        GetCurrentProcess=bind(k32, "GetCurrentProcess", VP),
        OpenProcessToken=bind(adv, "OpenProcessToken", BO, VP, DW, VP),
        DuplicateTokenEx=bind(adv, "DuplicateTokenEx", BO, VP, DW, VP, INT, INT, VP),
        ConvertStringSidToSidW=bind(adv, "ConvertStringSidToSidW", BO, WSTR, VP),
        GetLengthSid=bind(adv, "GetLengthSid", DW, VP),
        SetTokenInformation=bind(adv, "SetTokenInformation", BO, VP, INT, VP, DW),
        LocalFree=bind(k32, "LocalFree", VP, VP),
        ConvertStringSecurityDescriptorToSecurityDescriptorW=bind(
            adv, "ConvertStringSecurityDescriptorToSecurityDescriptorW", BO, WSTR, DW, VP, VP),
        GetSecurityDescriptorSacl=bind(adv, "GetSecurityDescriptorSacl", BO, VP, VP, VP, VP),
        SetNamedSecurityInfoW=bind(adv, "SetNamedSecurityInfoW", DW, WSTR, INT, DW,
                                   VP, VP, VP, VP),
    )
    return _api_cache


def _invalid_handle_value():
    return (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1


def _probe_windows():
    try:
        api = _api()
    except (OSError, AttributeError) as exc:
        return False, "Win32 API could not be bound through ctypes: %s" % exc
    job = api.CreateJobObjectW(None, None)
    if not job:
        return False, "CreateJobObjectW failed (winerror %d)" % _winerror()
    api.CloseHandle(job)
    try:
        token = _low_token(api)
    except _Mechanism as exc:
        return False, str(exc)
    api.CloseHandle(token)
    return True, "job object, low integrity token, handle list"


def available():
    """Return ``(True, description)`` when the boundary can be applied here, else ``(False, reason)``.

    Probes once and caches the answer. Never starts a child process.
    """
    global _probe_result
    if sys.platform != "win32":
        return False, ("not Windows: job objects and integrity levels are "
                       "unavailable; plain processes are used")
    if _probe_result is None:
        _probe_result = _probe_windows()
    return _probe_result


def status():
    """What the boundary did for the last child: enabled, mechanisms, fallback."""
    with _state_lock:
        mechanisms = list(_last_mechanisms)
        fallback = last_fallback
    mode = _mode()
    return {
        "enabled": mode != "off",
        "mode": mode,
        "mechanisms": mechanisms,
        "fallback": fallback,
        "not_enforced": list(NOT_ENFORCED),
    }


def _record(mechanisms, fallback):
    global last_fallback, _last_mechanisms
    with _state_lock:
        last_fallback = fallback
        _last_mechanisms = tuple(mechanisms)


def _close_quietly(api, handle):
    if handle:
        api.CloseHandle(handle)


def _remove_dir(path):
    if path:
        shutil.rmtree(path, ignore_errors=True)


def _low_token(api):
    """A primary token duplicated from this process, with integrity level Low."""
    proc_token = ctypes.c_void_p()
    if not api.OpenProcessToken(api.GetCurrentProcess(), _TOKEN_DUP_ADJ,
                                ctypes.addressof(proc_token)):
        raise _Mechanism("low-integrity",
                         "OpenProcessToken failed (winerror %d)" % _winerror())
    try:
        primary = ctypes.c_void_p()
        if not api.DuplicateTokenEx(proc_token.value, _TOKEN_ALL_ACCESS, None,
                                    _SECURITY_IMPERSONATION, _TOKEN_PRIMARY,
                                    ctypes.addressof(primary)):
            raise _Mechanism("low-integrity",
                             "DuplicateTokenEx failed (winerror %d)" % _winerror())
        try:
            sid = ctypes.c_void_p()
            if not api.ConvertStringSidToSidW("S-1-16-4096", ctypes.addressof(sid)):
                raise _Mechanism("low-integrity",
                                 "ConvertStringSidToSidW failed (winerror %d)" % _winerror())
            try:
                label = api.TOKEN_MANDATORY_LABEL()
                label.Label.Sid = sid.value
                label.Label.Attributes = _SE_GROUP_INTEGRITY
                size = ctypes.sizeof(api.TOKEN_MANDATORY_LABEL) + api.GetLengthSid(sid.value)
                if not api.SetTokenInformation(primary.value, _TOKEN_INTEGRITY_LEVEL,
                                               ctypes.addressof(label), size):
                    raise _Mechanism("low-integrity",
                                     "SetTokenInformation(TokenIntegrityLevel) failed "
                                     "(winerror %d)" % _winerror())
            finally:
                api.LocalFree(sid.value)
        except BaseException:
            _close_quietly(api, primary.value)
            raise
        return primary.value
    finally:
        _close_quietly(api, proc_token.value)


def _low_directory(api):
    """A fresh folder under the system temp directory, labelled Low (object inherit)."""
    from ctypes import wintypes
    path = tempfile.mkdtemp(prefix="graygoo-lowbox-")
    try:
        descriptor = ctypes.c_void_p()
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                "S:(ML;OICI;NW;;;LW)", _SDDL_REVISION_1,
                ctypes.addressof(descriptor), None):
            raise _Mechanism("low-integrity",
                             "label SDDL rejected (winerror %d)" % _winerror())
        try:
            present = wintypes.BOOL(0)
            defaulted = wintypes.BOOL(0)
            sacl = ctypes.c_void_p()
            if not api.GetSecurityDescriptorSacl(descriptor.value,
                                                 ctypes.addressof(present),
                                                 ctypes.addressof(sacl),
                                                 ctypes.addressof(defaulted)):
                raise _Mechanism("low-integrity",
                                 "GetSecurityDescriptorSacl failed (winerror %d)" % _winerror())
            status_code = api.SetNamedSecurityInfoW(path, _SE_FILE_OBJECT,
                                                    _LABEL_SECURITY_INFORMATION,
                                                    None, None, None, sacl.value)
            if status_code != 0:
                raise _Mechanism("low-integrity",
                                 "SetNamedSecurityInfoW(label) failed (error %d)" % status_code)
        finally:
            api.LocalFree(descriptor.value)
    except BaseException:
        _remove_dir(path)
        raise
    return path


def _job_object(api, memory_mb):
    """A kill-on-close job: one active process, a memory cap, and UI restrictions."""
    job = api.CreateJobObjectW(None, None)
    if not job:
        raise _Mechanism("job-object", "CreateJobObjectW failed (winerror %d)" % _winerror())
    try:
        limits = api.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        basic = limits.BasicLimitInformation
        basic.LimitFlags = (_JOB_LIMIT_ACTIVE_PROCESS | _JOB_LIMIT_PROCESS_MEMORY
                            | _JOB_LIMIT_KILL_ON_CLOSE)
        basic.ActiveProcessLimit = 1
        limits.ProcessMemoryLimit = int(memory_mb) * 1024 * 1024
        if not api.SetInformationJobObject(job, _JOB_EXTENDED_LIMIT_CLASS,
                                           ctypes.addressof(limits),
                                           ctypes.sizeof(limits)):
            raise _Mechanism("job-object", "SetInformationJobObject(limits) failed "
                             "(winerror %d)" % _winerror())
        ui = api.JOBOBJECT_BASIC_UI_RESTRICTIONS()
        ui.UIRestrictionsClass = _JOB_UI_ALL
        if not api.SetInformationJobObject(job, _JOB_UI_RESTRICTIONS_CLASS,
                                           ctypes.addressof(ui), ctypes.sizeof(ui)):
            raise _Mechanism("job-object", "SetInformationJobObject(ui) failed "
                             "(winerror %d)" % _winerror())
    except BaseException:
        _close_quietly(api, job)
        raise
    return job


def _pipe(api, sa):
    read_end = ctypes.c_void_p()
    write_end = ctypes.c_void_p()
    if not api.CreatePipe(ctypes.addressof(read_end), ctypes.addressof(write_end),
                          ctypes.addressof(sa), 0):
        raise OSError(0, "CreatePipe failed", None, _winerror())
    return read_end.value, write_end.value


def _nul(api, sa):
    handle = api.CreateFileW("NUL", _GENERIC_READ | _GENERIC_WRITE, _FILE_SHARE_READ_WRITE,
                             ctypes.addressof(sa), _OPEN_EXISTING, 0, None)
    if not handle or handle == _invalid_handle_value():
        raise OSError(0, "CreateFileW(NUL) failed", None, _winerror())
    return handle


def _inherit_std(api, std_id, sa):
    current = api.GetStdHandle(std_id)
    if not current or current == _invalid_handle_value():
        return _nul(api, sa)
    duplicate = ctypes.c_void_p()
    process = api.GetCurrentProcess()
    if not api.DuplicateHandle(process, current, process, ctypes.addressof(duplicate),
                               0, True, _DUPLICATE_SAME_ACCESS):
        return _nul(api, sa)
    return duplicate.value


def _handle_list(api, handles):
    """Attribute list that lets the child inherit exactly HANDLES. Returns (buffer, array)."""
    size = ctypes.c_size_t(0)
    api.InitializeProcThreadAttributeList(None, 1, 0, ctypes.addressof(size))
    if size.value == 0:
        raise _Mechanism("handle-list", "attribute list size query failed (winerror %d)"
                         % _winerror())
    buffer = ctypes.create_string_buffer(size.value)
    if not api.InitializeProcThreadAttributeList(ctypes.addressof(buffer), 1, 0,
                                                 ctypes.addressof(size)):
        raise _Mechanism("handle-list", "InitializeProcThreadAttributeList failed "
                         "(winerror %d)" % _winerror())
    array = (ctypes.c_void_p * len(handles))(*handles)
    if not api.UpdateProcThreadAttribute(ctypes.addressof(buffer), 0,
                                         _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                         ctypes.addressof(array), ctypes.sizeof(array),
                                         None, None):
        api.DeleteProcThreadAttributeList(ctypes.addressof(buffer))
        raise _Mechanism("handle-list", "UpdateProcThreadAttribute failed (winerror %d)"
                         % _winerror())
    return buffer, array


def _environment_block(env):
    items = sorted(((str(k), str(v)) for k, v in env.items()), key=lambda kv: kv[0].upper())
    data = ("".join("%s=%s\0" % kv for kv in items) + "\0").encode("utf-16-le")
    return ctypes.create_string_buffer(data, len(data))


def _command_line(cmd):
    if isinstance(cmd, (list, tuple)):
        return subprocess.list2cmdline([os.fspath(part) for part in cmd])
    if isinstance(cmd, str):
        return cmd
    raise TypeError("cmd must be a list or str, got %s" % type(cmd).__name__)


def _create(cmd, kwargs, api, token, lowdir, job, with_handle_list):
    """Start CMD suspended, attach it to JOB, then resume it.

    Returns a SandboxedProcess that owns JOB and LOWDIR. TOKEN is borrowed:
    the caller closes it. Raises _Mechanism for a mechanism failure, or OSError
    when the process itself cannot start.
    """
    import msvcrt
    owned = []        # every handle this call created, closed on failure
    child_side = []   # the child ends of the pipes, closed in the parent after the start
    parent_side = {}  # stream name -> parent handle, wrapped in file objects on success
    attr_buffer = attr_array = None
    try:
        sa = api.SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(sa)
        sa.bInheritHandle = 1
        std = {}
        for name in ("stdin", "stdout", "stderr"):
            spec = kwargs.get(name)
            if spec == subprocess.PIPE:
                read_end, write_end = _pipe(api, sa)
                owned.extend((read_end, write_end))
                if name == "stdin":
                    child, parent = read_end, write_end
                else:
                    child, parent = write_end, read_end
                if not api.SetHandleInformation(parent, _HANDLE_FLAG_INHERIT, 0):
                    raise OSError(0, "SetHandleInformation failed", None, _winerror())
                std[name] = child
                parent_side[name] = parent
            elif spec == subprocess.DEVNULL:
                std[name] = _nul(api, sa)
                owned.append(std[name])
            elif spec is None:
                std[name] = _inherit_std(api, _STD_IDS[name], sa)
                owned.append(std[name])
            else:
                raise _Unsupported("%s=%r" % (name, spec))
            child_side.append(std[name])
        handles = []
        for name in ("stdin", "stdout", "stderr"):
            if std[name] not in handles:
                handles.append(std[name])
        if with_handle_list:
            attr_buffer, attr_array = _handle_list(api, handles)

        startup = api.STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(api.STARTUPINFOEXW)
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = std["stdin"]
        startup.StartupInfo.hStdOutput = std["stdout"]
        startup.StartupInfo.hStdError = std["stderr"]
        startup.lpAttributeList = (ctypes.addressof(attr_buffer)
                                   if attr_buffer is not None else None)

        env = kwargs.get("env")
        env = dict(os.environ) if env is None else dict(env)
        if lowdir is not None:
            for key in list(env):
                if key.upper() in ("TEMP", "TMP"):
                    del env[key]
            env["TEMP"] = lowdir
            env["TMP"] = lowdir
        env_block = _environment_block(env)
        cmd_buffer = ctypes.create_unicode_buffer(_command_line(cmd))
        cwd = kwargs.get("cwd")
        cwd = os.fspath(cwd) if cwd is not None else None
        flags = (_CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT | _EXTENDED_STARTUPINFO_PRESENT
                 | _CREATE_NO_WINDOW | int(kwargs.get("creationflags") or 0))
        info = api.PROCESS_INFORMATION()
        if token is not None:
            started = api.CreateProcessAsUserW(
                token, None, ctypes.addressof(cmd_buffer), None, None, True, flags,
                ctypes.addressof(env_block), cwd, ctypes.addressof(startup),
                ctypes.addressof(info))
            if not started:
                raise _Mechanism("low-integrity", "CreateProcessAsUserW failed (winerror %d)"
                                 % _winerror())
        else:
            started = api.CreateProcessW(
                None, ctypes.addressof(cmd_buffer), None, None, True, flags,
                ctypes.addressof(env_block), cwd, ctypes.addressof(startup),
                ctypes.addressof(info))
            if not started:
                raise OSError(0, "CreateProcessW failed", None, _winerror())
        process_handle = info.hProcess
        thread_handle = info.hThread
        try:
            if job is not None and not api.AssignProcessToJobObject(job, process_handle):
                raise _Mechanism("job-object", "AssignProcessToJobObject failed (winerror %d)"
                                 % _winerror())
            if api.ResumeThread(thread_handle) == 0xFFFFFFFF:
                raise OSError(0, "ResumeThread failed", None, _winerror())
        except BaseException:
            api.TerminateProcess(process_handle, 1)
            api.WaitForSingleObject(process_handle, 2000)
            _close_quietly(api, thread_handle)
            _close_quietly(api, process_handle)
            raise
        api.CloseHandle(thread_handle)
        for handle in child_side:
            api.CloseHandle(handle)
        owned = []
        streams = {}
        for name, handle in parent_side.items():
            if name == "stdin":
                streams[name] = os.fdopen(msvcrt.open_osfhandle(handle, os.O_WRONLY), "wb")
            else:
                streams[name] = os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), "rb")
        applied = []
        if job is not None:
            applied.append("job-object")
        if token is not None:
            applied.append("low-integrity")
        if attr_buffer is not None:
            applied.append("handle-list")
        return SandboxedProcess(cmd, info.dwProcessId, process_handle, job, lowdir,
                                applied, streams, api)
    except BaseException:
        # owned holds every handle this call created and has not yet closed
        for handle in owned:
            _close_quietly(api, handle)
        raise
    finally:
        if attr_buffer is not None:
            api.DeleteProcThreadAttributeList(ctypes.addressof(attr_buffer))


def _spawn_sandboxed(cmd, kwargs, memory_mb, strict):
    """Start CMD with as many mechanisms as work. Returns (process or None, skipped reasons)."""
    api = _api()
    want = list(MECHANISMS)
    skipped = []
    while want:
        job = token = lowdir = None
        try:
            if "job-object" in want:
                job = _job_object(api, memory_mb)
            if "low-integrity" in want:
                token = _low_token(api)
                lowdir = _low_directory(api)
            proc = _create(cmd, kwargs, api, token, lowdir, job, "handle-list" in want)
        except _Mechanism as exc:
            _close_quietly(api, job)
            _close_quietly(api, token)
            _remove_dir(lowdir)
            if strict:
                raise SandboxError("OS sandbox could not be applied: %s" % exc) from None
            skipped.append(str(exc))
            want.remove(exc.name)
            continue
        except BaseException:
            _close_quietly(api, job)
            _close_quietly(api, token)
            _remove_dir(lowdir)
            raise
        _close_quietly(api, token)
        return proc, skipped
    return None, skipped


def _plain(cmd, kwargs, reason):
    _record((), reason)
    return subprocess.Popen(cmd, **kwargs)


def popen(cmd, *, strict=None, memory_mb=DEFAULT_MEMORY_MB, **popen_kwargs):
    """Start CMD inside the OS boundary; a Popen-compatible object.

    ``strict`` (default: from GRAYGOO_OS_SANDBOX) makes any failure raise
    SandboxError. Otherwise the child starts with the mechanisms that worked,
    or as a plain subprocess.Popen when none did; the reason is recorded.
    ``memory_mb`` is the job's per-process memory cap.
    """
    mode = _mode()
    if strict is None:
        strict = mode == "strict"
    if mode == "off":
        return _plain(cmd, popen_kwargs, "disabled by %s=0" % ENV_VAR)
    ok, reason = available()
    if not ok:
        if strict:
            raise SandboxError(reason)
        return _plain(cmd, popen_kwargs, reason)
    unknown = sorted(set(popen_kwargs) - _SUPPORTED_KWARGS)
    if unknown or popen_kwargs.get("close_fds", True) is False:
        reason = "popen arguments not supported by the sandbox: %s" % (
            ", ".join(unknown) or "close_fds=False")
        if strict:
            raise SandboxError(reason)
        return _plain(cmd, popen_kwargs, reason)
    try:
        proc, skipped = _spawn_sandboxed(cmd, popen_kwargs, int(memory_mb), strict)
    except _Unsupported as exc:
        reason = "popen arguments not supported by the sandbox: %s" % exc
        if strict:
            raise SandboxError(reason) from None
        return _plain(cmd, popen_kwargs, reason)
    if proc is None:
        reason = "; ".join(skipped) or "no mechanism applied"
        if strict:
            raise SandboxError(reason)
        return _plain(cmd, popen_kwargs, reason)
    _record(proc.mechanisms, "; ".join(skipped) or None)
    return proc


def release(proc):
    """Free what the boundary holds for PROC (job handle, Low folder). Safe for any Popen."""
    if isinstance(proc, SandboxedProcess):
        proc.close()


def _left(deadline):
    return None if deadline is None else max(0.0, deadline - time.monotonic())


class SandboxedProcess:
    """A child started by :func:`popen` on Windows, with the Popen methods the callers use.

    Provides pid, args, returncode, stdin/stdout/stderr (file objects or None),
    poll, wait, kill, terminate, communicate, close and the context manager.
    Closing the job (close_job, or close, or garbage collection) kills the
    child through KILL_ON_JOB_CLOSE. ``mechanisms`` names what was applied.
    """

    def __init__(self, args, pid, process_handle, job, lowdir, mechanisms, streams, api):
        self.args = args
        self.pid = pid
        self.returncode = None
        self.stdin = streams.get("stdin")
        self.stdout = streams.get("stdout")
        self.stderr = streams.get("stderr")
        self.mechanisms = tuple(mechanisms)
        self._api = api
        self._process = process_handle
        self._job = job
        self._lowdir = lowdir
        self._lock = threading.RLock()
        self._readers = {}
        self._chunks = {}
        self._stdin_closed = False

    def _reap(self):
        code = ctypes.c_ulong(0)
        self._api.GetExitCodeProcess(self._process, ctypes.addressof(code))
        self.returncode = code.value
        self.close_job()

    def poll(self):
        with self._lock:
            if self.returncode is None and self._process:
                if self._api.WaitForSingleObject(self._process, 0) == _WAIT_OBJECT_0:
                    self._reap()
            return self.returncode

    def wait(self, timeout=None):
        if self.returncode is not None or not self._process:
            return self.returncode
        millis = _INFINITE if timeout is None else max(0, int(timeout * 1000))
        state = self._api.WaitForSingleObject(self._process, millis)
        if state == _WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if state != _WAIT_OBJECT_0:
            raise OSError(0, "WaitForSingleObject failed", None, _winerror())
        with self._lock:
            if self.returncode is None:
                self._reap()
        return self.returncode

    def kill(self):
        if self.returncode is None and self._process:
            self._api.TerminateProcess(self._process, 1)

    terminate = kill

    def close_job(self):
        """Close the job handle. KILL_ON_JOB_CLOSE ends the child if it is still running."""
        with self._lock:
            if self._job:
                self._api.CloseHandle(self._job)
                self._job = None

    def _drain(self, name, stream):
        try:
            data = stream.read()
        except (OSError, ValueError):
            data = b""
        self._chunks[name] = data

    def communicate(self, input=None, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        if self.stdin is not None and not self._stdin_closed:
            self._stdin_closed = True
            try:
                if input:
                    self.stdin.write(input)
                self.stdin.close()
            except OSError:
                pass
        for name in ("stdout", "stderr"):
            stream = getattr(self, name)
            if stream is not None and name not in self._readers:
                reader = threading.Thread(target=self._drain, args=(name, stream), daemon=True)
                self._readers[name] = reader
                reader.start()
        for reader in self._readers.values():
            reader.join(_left(deadline))
            if reader.is_alive():
                raise subprocess.TimeoutExpired(self.args, timeout)
        self.wait(timeout=_left(deadline))
        out = self._chunks.get("stdout", b"") if self.stdout is not None else None
        err = self._chunks.get("stderr", b"") if self.stderr is not None else None
        return out, err

    def close(self):
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        with self._lock:
            if self.returncode is None and self._process:
                self.close_job()          # KILL_ON_JOB_CLOSE ends a running child
                if self._api.WaitForSingleObject(self._process, 5000) == _WAIT_OBJECT_0:
                    self._reap()
            if self._process:
                self._api.CloseHandle(self._process)
                self._process = None
            _remove_dir(self._lowdir)
            self._lowdir = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
