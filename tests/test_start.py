"""start: port choice, readiness wait, --status, and remembered mounts."""
import contextlib
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mount  # noqa: E402
import projects  # noqa: E402
import start  # noqa: E402


class FakeDashboard:
    """A localhost stand-in on an ephemeral port.

    With ``graygoo=True`` it answers like the dashboard's config and projects
    endpoints; otherwise every path is a plain 404, like some other program.
    """

    def __init__(self, graygoo=True, projects_listing=None):
        listing = projects_listing or []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if fake.graygoo and self.path == "/api/agent/config":
                    return self._json({"live": {"available": False}})
                if fake.graygoo and self.path == "/api/agent/projects":
                    return self._json({"projects": listing})
                self.send_error(404)

            def _json(self, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        self.graygoo = graygoo
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d/" % self.port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def free_unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PortTests(unittest.TestCase):
    def test_free_preferred_port_is_used(self):
        port, status = start.pick_port(8150, probe=lambda p: "free")
        self.assertEqual((port, status), (8150, "free"))

    def test_taken_by_other_program_moves_to_next_free_port(self):
        probe = {8150: "other", 8151: "free"}.get
        port, status = start.pick_port(8150, probe=lambda p: probe(p, "other"))
        self.assertEqual((port, status), (8151, "free"))

    def test_existing_graygoo_dashboard_is_reused(self):
        probe = {8150: "other", 8152: "graygoo"}.get
        port, status = start.pick_port(8150, probe=lambda p: probe(p, "other"))
        self.assertEqual((port, status), (8152, "graygoo"))

    def test_no_free_port_gives_none(self):
        self.assertEqual(start.pick_port(8150, probe=lambda p: "other"), (None, None))

    def test_probe_reports_non_graygoo_listener_as_other(self):
        other = FakeDashboard(graygoo=False)
        try:
            self.assertEqual(start.probe_port(other.port), "other")
            self.assertFalse(start.is_graygoo(other.url))
        finally:
            other.close()

    def test_is_graygoo_true_for_a_dashboard(self):
        dash = FakeDashboard(graygoo=True)
        try:
            self.assertTrue(start.is_graygoo(dash.url))
            self.assertEqual(start.probe_port(dash.port), "graygoo")
        finally:
            dash.close()

    def test_is_graygoo_false_when_nothing_listens(self):
        self.assertFalse(start.is_graygoo("http://127.0.0.1:%d/" % free_unused_port(),
                                         timeout=0.5))


class ReadinessTests(unittest.TestCase):
    def test_wait_ready_succeeds_once_dashboard_answers(self):
        dash = FakeDashboard(graygoo=True)
        try:
            self.assertTrue(start.wait_ready(dash.url, timeout=5, sleep=lambda s: None))
        finally:
            dash.close()

    def test_wait_ready_times_out_with_false(self):
        url = "http://127.0.0.1:%d/" % free_unused_port()
        began = time.monotonic()
        self.assertFalse(start.wait_ready(url, timeout=0.3, sleep=lambda s: time.sleep(0.05),
                                          check=lambda u: start.is_graygoo(u, timeout=0.2)))
        self.assertLess(time.monotonic() - began, 5)


class StatusTests(unittest.TestCase):
    def test_status_reports_running_dashboard_projects_and_mounts(self):
        listing = [{"id": "scratch", "mount": None},
                   {"id": "demo-1234", "mount": {"url": "http://127.0.0.1:8201/"}}]
        dash = FakeDashboard(graygoo=True, projects_listing=listing)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = start.main(["--status", "--port", str(dash.port)],
                                  open_browser=lambda u: self.fail("must not open"))
        finally:
            dash.close()
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("dashboard running at %s" % dash.url, text)
        self.assertIn("projects: 2 (scratch, demo-1234)", text)
        self.assertIn("  demo-1234 at http://127.0.0.1:8201/", text)
        self.assertNotIn("  scratch at", text)

    def test_status_without_dashboard_exits_1(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with unittest.mock.patch.object(start, "PORT_RANGE", range(0)):
                code = start.main(["--status", "--port", str(free_unused_port())])
        self.assertEqual(code, 1)
        self.assertIn("no GrayGoo dashboard", out.getvalue())

    def test_running_dashboard_is_opened_not_restarted(self):
        dash = FakeDashboard(graygoo=True)
        opened = []
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = start.main(["--port", str(dash.port)], open_browser=opened.append)
        finally:
            dash.close()
        self.assertEqual(code, 0)
        self.assertEqual(opened, [dash.url])
        self.assertIn("already running", out.getvalue())


class MountsFileTests(unittest.TestCase):
    def test_round_trip_through_start_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "apps" / "mounts.json"
            mount.write_mounts(path, {"demo-1234": 8201, "scratch": 8202})
            self.assertEqual(start.remembered_mounts(path),
                             {"demo-1234": 8201, "scratch": 8202})
            self.assertFalse(path.with_name("mounts.json.tmp").exists())

    def test_missing_or_corrupt_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mounts.json"
            self.assertEqual(start.remembered_mounts(path), {})
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(start.remembered_mounts(path), {})
            path.write_text('{"mounts": [1, 2]}', encoding="utf-8")
            self.assertEqual(start.remembered_mounts(path), {})
            path.write_text('{"mounts": {"ok": 8201, "bad": "x", "neg": -1, "flag": true}}',
                            encoding="utf-8")
            self.assertEqual(start.remembered_mounts(path), {"ok": 8201})


class RestoreTests(unittest.TestCase):
    class FakeRegistry:
        """Enough of a tool registry for a mount that is never called."""
        def for_mode(self, mode):
            return self

    def manager(self, tmp, restore=False):
        return mount.MountManager(tmp, lambda pid: self.FakeRegistry(), restore=restore)

    def test_start_remembers_port_and_stop_forgets_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = projects.ProjectStore(tmp).create("Remember Me")[0]
            mgr = self.manager(tmp)
            info, err = mgr.start(project["id"])
            self.assertIsNone(err)
            self.assertEqual(mount.read_mounts(mgr.mounts_path),
                             {project["id"]: urlsplit(info["url"]).port})
            mgr.stop(project["id"])
            self.assertEqual(mount.read_mounts(mgr.mounts_path), {})

    def test_restore_skips_unknown_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = projects.ProjectStore(tmp).create("Known App")[0]
            mount.write_mounts(mount.mounts_path(tmp),
                               {"ghost-9999": free_unused_port(), project["id"]: free_unused_port()})
            mgr = self.manager(tmp)
            restored = mgr.restore()
            try:
                self.assertEqual([r["project"] for r in restored], [project["id"]])
                self.assertTrue(restored[0]["url"].startswith("http://127.0.0.1:"))
                self.assertIsNone(mgr.info("ghost-9999"))
                # the entry is kept, not erased: a project restored later comes back
                self.assertIn("ghost-9999", mount.read_mounts(mgr.mounts_path))
            finally:
                mgr.stop(project["id"])

    def test_restore_on_constructor_mounts_remembered_apps(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = projects.ProjectStore(tmp).create("Auto Mount")[0]
            mount.write_mounts(mount.mounts_path(tmp), {project["id"]: free_unused_port()})
            mgr = self.manager(tmp, restore=True)
            try:
                self.assertIsNotNone(mgr.info(project["id"]))
            finally:
                mgr.stop(project["id"])

    def test_restore_falls_back_when_remembered_port_is_taken(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = projects.ProjectStore(tmp).create("Busy Port")[0]
            blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            taken = blocker.getsockname()[1]
            mount.write_mounts(mount.mounts_path(tmp), {project["id"]: taken})
            mgr = self.manager(tmp)
            try:
                restored = mgr.restore()
                self.assertEqual(len(restored), 1)
                self.assertNotEqual(mgr.info(project["id"])["port"], taken)
            finally:
                mgr.stop(project["id"])
                blocker.close()


if __name__ == "__main__":
    unittest.main()
