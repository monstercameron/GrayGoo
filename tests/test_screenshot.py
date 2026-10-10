"""Tests for the headless-browser screenshot helper (screenshot.py). Stdlib only."""

import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import zlib
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import screenshot

PAGE = (
    '<html><body style="margin:0;background:#c00">'
    '<h1 style="color:#fff;font:60px sans-serif">Hello shot</h1></body></html>'
)


def _minimal_png(width, height):
    """Signature plus a correct IHDR chunk; enough for png_size, no pixel data."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
    return screenshot.PNG_SIGNATURE + chunk + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))


class _FakeLauncher:
    """Stand-in for a Popen object: alive until EXIT_CODE is set, never writes a file."""

    pid = 4321
    stderr = None

    def __init__(self, exit_code=None):
        self.exit_code = exit_code
        self.killed = False

    def poll(self):
        return self.exit_code

    def kill(self):
        self.killed = True


class PngSizeTest(unittest.TestCase):
    def test_reads_width_and_height_from_ihdr(self):
        self.assertEqual(screenshot.png_size(_minimal_png(3, 2)), (3, 2))

    def test_non_png_bytes_give_none(self):
        self.assertIsNone(screenshot.png_size(b"not a png"))
        self.assertIsNone(screenshot.png_size(b""))


class FindBrowserTest(unittest.TestCase):
    def test_env_override_is_used_when_file_exists(self):
        with mock.patch.dict(os.environ, {"GRAYGOO_BROWSER": sys.executable}):
            self.assertEqual(screenshot.find_browser(), sys.executable)

    def test_env_override_is_ignored_when_file_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "no-such-browser.exe")
            with mock.patch.dict(os.environ, {"GRAYGOO_BROWSER": missing}):
                self.assertNotEqual(screenshot.find_browser(), missing)


class NoBrowserTest(unittest.TestCase):
    def test_reports_error_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "shot.png")
            with mock.patch.object(screenshot, "find_browser", return_value=None):
                result = screenshot.capture_html("<p>x</p>", out)
            self.assertFalse(result["ok"])
            self.assertEqual(
                result["error"],
                "no Chromium-family browser found (set GRAYGOO_BROWSER to its path)",
            )
            self.assertEqual(result["path"], "")
            self.assertFalse(os.path.exists(out))
            self.assertEqual(set(result), {"ok", "path", "bytes", "width", "height", "ms", "error"})


@unittest.skipUnless(screenshot.find_browser(), "no browser")
class RealBrowserTest(unittest.TestCase):
    def test_capture_html_renders_png_of_requested_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "shots", "hello.png")
            result = screenshot.capture_html(PAGE, out, width=800, height=600, wait_ms=500)
            self.assertTrue(result["ok"], result["error"])
            self.assertTrue(os.path.exists(out))
            self.assertGreater(result["bytes"], 1000)
            # If Edge scales the image (device pixel ratio), this relaxes to >= in the report.
            self.assertEqual((result["width"], result["height"]), (800, 600))
            with open(out, "rb") as fh:
                data = fh.read()
            self.assertEqual(screenshot.png_size(data), (result["width"], result["height"]))
            self.assertEqual(result["bytes"], len(data))
            self.assertEqual(result["path"], os.path.abspath(out))

    def test_stale_file_at_out_path_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "hello.png")
            with open(out, "wb") as fh:
                fh.write(b"old")
            result = screenshot.capture_html(PAGE, out, width=800, height=600, wait_ms=500)
            self.assertTrue(result["ok"], result["error"])
            with open(out, "rb") as fh:
                self.assertTrue(fh.read().startswith(screenshot.PNG_SIGNATURE))

    def test_temp_profile_and_html_folder_are_removed(self):
        real_popen = subprocess.Popen
        seen = []

        def spy(cmd, *args, **kwargs):
            seen.append(list(cmd))
            return real_popen(cmd, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "page.png")
            with mock.patch.object(screenshot.subprocess, "Popen", side_effect=spy):
                result = screenshot.capture_html(PAGE, out, width=800, height=600, wait_ms=500)
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(len(seen), 1)
        cmd = seen[0]
        profile = [a[len("--user-data-dir="):] for a in cmd if a.startswith("--user-data-dir=")]
        self.assertEqual(len(profile), 1)
        # the browser may hold its profile open for a moment: removal is then finished in the background
        for _ in range(160):                 # up to 40 s: the full suite keeps the machine busy
            if not os.path.exists(profile[0]):
                break
            time.sleep(0.25)
        self.assertFalse(os.path.exists(profile[0]))
        html_dir = os.path.dirname(urllib.request.url2pathname(urlparse(cmd[-1]).path))
        self.assertTrue(cmd[-1].startswith("file:///"))
        self.assertFalse(os.path.exists(html_dir))


class TimeoutTest(unittest.TestCase):
    def test_timeout_stops_only_that_process_tree(self):
        fake = _FakeLauncher()
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "late.png")
            with mock.patch.object(screenshot.subprocess, "Popen", return_value=fake) as popen, \
                    mock.patch.object(screenshot.subprocess, "run") as run, \
                    mock.patch.object(screenshot, "_profile_pids", return_value=[]):
                result = screenshot.capture_url(
                    "file:///C:/fake/page.html", out, timeout_s=0.5, browser="C:/fake/browser.exe"
                )
            self.assertFalse(os.path.exists(out))
        self.assertFalse(result["ok"])
        self.assertIn("did not finish within 0.5 s", result["error"])
        self.assertEqual(result["path"], "")
        cmd = popen.call_args.args[0]
        self.assertIn("--headless=new", cmd)
        self.assertIn("--screenshot=" + os.path.abspath(out), cmd)
        if os.name == "nt":
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "4321", "/T", "/F"])
            self.assertFalse(fake.killed)
        else:
            run.assert_not_called()
            self.assertTrue(fake.killed)

    def test_launcher_error_exit_fails_early_without_a_file(self):
        fake = _FakeLauncher(exit_code=2)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "broken.png")
            with mock.patch.object(screenshot.subprocess, "Popen", return_value=fake), \
                    mock.patch.object(screenshot.subprocess, "run") as run, \
                    mock.patch.object(screenshot, "_profile_pids", return_value=[]):
                result = screenshot.capture_url(
                    "file:///C:/fake/page.html", out, timeout_s=30, browser="C:/fake/browser.exe"
                )
            self.assertFalse(os.path.exists(out))
        self.assertFalse(result["ok"])
        self.assertIn("exit code 2", result["error"])
        self.assertLess(result["ms"], 5000)
        run.assert_not_called()  # the launcher already exited, so its PID is not touched


if __name__ == "__main__":
    unittest.main()
