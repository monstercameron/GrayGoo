"""Render an HTML page to a PNG with a Chromium-family browser already installed.

Stdlib only. The browser is driven through its command line (``--screenshot``)
with a fresh ``--user-data-dir`` per run, so a capture never touches the user's
own profile or any running browser window. Every capture returns a plain dict
(see ``capture_url``); expected failures are reported in that dict, not raised.

Edge (and Chrome) on Windows is a launcher: it exits with code 0 within about
0.1 s while a child browser process keeps rendering and writes the PNG a second
or so later. So a capture is finished when a complete PNG appears at the output
path, not when the launcher exits.

Manual check (prints the result dict as JSON)::

    uv run python screenshot.py <url-or-html-file> <out.png>
"""

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"
NO_BROWSER_ERROR = "no Chromium-family browser found (set GRAYGOO_BROWSER to its path)"
_SWEPT = False                # stale temp folders are cleared once per process
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def find_browser():
    """Path of a Chromium-family browser to drive, or None."""
    override = os.environ.get("GRAYGOO_BROWSER")
    if override and os.path.isfile(override):
        return override
    program_files = os.environ.get("ProgramFiles")
    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    local_app_data = os.environ.get("LOCALAPPDATA")
    edge_roots = [p for p in (program_files_x86, program_files) if p]
    chrome_roots = [p for p in (program_files, program_files_x86, local_app_data) if p]
    candidates = [os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe") for root in edge_roots]
    candidates += [os.path.join(root, "Google", "Chrome", "Application", "chrome.exe") for root in chrome_roots]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def png_size(data):
    """(width, height) of PNG bytes, or None when DATA is not a PNG."""
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _result(ok, started, path="", nbytes=0, width=0, height=0, error=""):
    ms = (time.perf_counter() - started) * 1000
    return {
        "ok": ok,
        "path": path,
        "bytes": nbytes,
        "width": width,
        "height": height,
        "ms": round(ms, 1),
        "error": error,
    }


def _try_remove(path):
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _keep_removing(path, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if _try_remove(path):
            return


def _remove_dir(path):
    """Delete a temp directory without holding up the caller.

    The browser can keep files of its profile open for a moment after the
    picture exists (longer when the machine is busy). One quick retry, then a
    background thread keeps trying for a while, so the capture returns at once
    and the folder still goes away. ``sweep_stale`` catches whatever is left.
    """
    if _try_remove(path):
        return
    time.sleep(0.3)
    if _try_remove(path):
        return
    threading.Thread(target=_keep_removing, args=(path,), daemon=True).start()


def sweep_stale(older_than_s=120.0):
    """Remove this module's temp folders left behind by earlier runs. Returns how many."""
    removed, root, now = 0, tempfile.gettempdir(), time.time()
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        if name.startswith(("graygoo-chrome-", "graygoo-html-")):
            path = os.path.join(root, name)
            try:
                old = now - os.path.getmtime(path) > older_than_s
            except OSError:
                continue
            if old and _try_remove(path):
                removed += 1
    return removed


def _taskkill(pid):
    """Force-stop one PID and its children (Windows). Never by image name."""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            **_NO_WINDOW,
        )
    except OSError:
        pass


def _profile_pids(exe, profile):
    """PIDs of processes started from this run's private profile directory (Windows).

    The profile path is a fresh temp directory, so only this run's browser tree
    carries it on its command line. Nothing else is matched.
    """
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq $env:GG_EXE_NAME "
        "-and $_.CommandLine -and $_.CommandLine.Contains($env:GG_PROFILE) } | "
        "ForEach-Object { $_.ProcessId }"
    )
    env = dict(os.environ, GG_EXE_NAME=os.path.basename(exe), GG_PROFILE=profile)
    try:
        found = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=env,
            **_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(token) for token in found.stdout.split() if token.isdigit()]


def _stop_run(proc, exe, profile):
    """Stop this run's browser: the launcher (if still alive) and anything started from its profile."""
    if proc.poll() is None:
        if os.name == "nt":
            _taskkill(proc.pid)
        else:
            proc.kill()
    if os.name == "nt":
        for pid in _profile_pids(exe, profile):
            _taskkill(pid)


def _drain(stream, sink):
    """Collect stderr as it arrives, so a full pipe can never stall the browser."""
    try:
        fd = stream.fileno()
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            sink.append(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _finished_png(path):
    """Bytes of the PNG at PATH once it is complete (ends with IEND), else None."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if data.endswith(PNG_END) and png_size(data) is not None:
        return data
    return None


def _png_result(out, data, started):
    width, height = png_size(data)
    if width <= 0 or height <= 0:
        return _result(False, started, error=f"the screenshot has no usable size ({width}x{height})")
    return _result(True, started, path=out, nbytes=len(data), width=width, height=height)


def capture_url(url, out_path, width=1280, height=1400, wait_ms=2500, timeout_s=30.0, browser=None):
    """Load URL once in a headless browser and save a PNG of it at OUT_PATH.

    Returns {"ok", "path", "bytes", "width", "height", "ms", "error"}; never raises
    for expected failures (no browser, timeout, unwritable path, browser error).
    """
    started = time.perf_counter()
    exe = browser or find_browser()
    if not exe:
        return _result(False, started, error=NO_BROWSER_ERROR)
    out = os.path.abspath(out_path)
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.exists(out):
            os.remove(out)
    except OSError as exc:
        return _result(False, started, error=f"cannot prepare the output path: {exc}")

    global _SWEPT
    if not _SWEPT:                       # once per process: clear what earlier runs left behind
        _SWEPT = True
        sweep_stale()
    profile = tempfile.mkdtemp(prefix="graygoo-chrome-")
    try:
        cmd = [
            exe,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--mute-audio",
            f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            f"--virtual-time-budget={wait_ms}",
            f"--screenshot={out}",
            url,
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **_NO_WINDOW)
        except OSError as exc:
            return _result(False, started, error=f"could not start the browser: {exc}")

        chunks = []
        reader = None
        if proc.stderr is not None:
            reader = threading.Thread(target=_drain, args=(proc.stderr, chunks), daemon=True)
            reader.start()

        deadline = started + timeout_s
        while True:
            data = _finished_png(out)
            if data is not None:
                return _png_result(out, data, started)
            code = proc.poll()
            if code not in (None, 0):
                _stop_run(proc, exe, profile)
                stderr = _stderr_text(chunks, reader)
                detail = f" stderr: {stderr[:200]}" if stderr else ""
                return _result(
                    False, started, error=f"the browser (exit code {code}) wrote no screenshot.{detail}"
                )
            if time.perf_counter() >= deadline:
                _stop_run(proc, exe, profile)
                return _result(False, started, error=f"the browser did not finish within {timeout_s:g} s")
            time.sleep(0.05)
    finally:
        _remove_dir(profile)


def _stderr_text(chunks, reader):
    if reader is not None:
        reader.join(timeout=0.5)
    return b"".join(list(chunks)).decode("utf-8", "replace").strip()


def capture_html(html, out_path, width=1280, height=1400, wait_ms=2500, timeout_s=30.0, browser=None):
    """Write HTML (a str) to a temporary page, screenshot it, then delete the page folder.

    Same return shape as ``capture_url``.
    """
    started = time.perf_counter()
    page_dir = tempfile.mkdtemp(prefix="graygoo-html-")
    try:
        page = Path(page_dir) / "page.html"
        page.write_bytes(html.encode("utf-8"))
        result = capture_url(
            page.as_uri(),
            out_path,
            width=width,
            height=height,
            wait_ms=wait_ms,
            timeout_s=timeout_s,
            browser=browser,
        )
        result["ms"] = round((time.perf_counter() - started) * 1000, 1)
        return result
    except OSError as exc:
        return _result(False, started, error=f"could not write the temporary page: {exc}")
    finally:
        _remove_dir(page_dir)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: uv run python screenshot.py <url-or-html-file> <out.png>", file=sys.stderr)
        return 2
    source, out_path = args
    url = source
    if os.path.isfile(source):
        url = Path(source).resolve().as_uri()
    result = capture_url(url, out_path)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
