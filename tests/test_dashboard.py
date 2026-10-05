"""Backend tests for the GrayGoo dashboard. Stdlib unittest only.

No network binds: handler logic is exercised through server.dispatch()
and the pure collector functions, with stub job commands.
"""

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SERVER_PATH = ROOT / "dashboard" / "server.py"


def load_server():
    spec = importlib.util.spec_from_file_location("dashboard_server",
                                                  SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = load_server()

SLEEP_CMD = [sys.executable, "-c", "import time; time.sleep(30)"]


def make_ctx(commands):
    tmp = tempfile.TemporaryDirectory()
    manager = server.JobManager(ROOT, out_dir=Path(tmp.name),
                                commands=commands)
    ctx = SimpleNamespace(root=ROOT, manager=manager)
    return ctx, tmp


def wait_until(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class TestRedact(unittest.TestCase):
    def test_env_assignment_redacted(self):
        out = server.redact_text("CEREBRAS_API_KEY=sk-live-12345")
        self.assertNotIn("sk-live-12345", out)
        self.assertIn("[REDACTED]", out)

    def test_json_value_redacted(self):
        out = server.redact_text('{"cerebras_api_key": "abc123"}')
        self.assertNotIn("abc123", out)
        self.assertIn("[REDACTED]", out)

    def test_clean_text_untouched(self):
        self.assertEqual(server.redact_text("all systems go"),
                         "all systems go")

    def test_non_string_safe(self):
        self.assertIsInstance(server.redact_text(None), str)
        self.assertIsInstance(server.redact_text(123), str)


class TestStatusShape(unittest.TestCase):
    def test_build_status_keys(self):
        status = server.build_status(ROOT)
        for key in ("todos", "git", "spend", "presence"):
            self.assertIn(key, status)
        self.assertIn("checked", status["todos"])
        self.assertIn("open", status["todos"])
        self.assertIn("commits", status["git"])
        self.assertIn("budget_usd", status["spend"])
        self.assertEqual(status["spend"]["budget_usd"], 50.0)
        self.assertIn(status["presence"]["cerebras_key"],
                      ("present", "absent"))

    def test_dispatch_status_ok(self):
        ctx, tmp = make_ctx({})
        with tmp:
            code, payload = server.dispatch("GET", "/api/status", b"", ctx)
        self.assertEqual(code, 200)
        self.assertIn("todos", payload)

    def test_dispatch_pipeline_ok(self):
        ctx, tmp = make_ctx({})
        with tmp:
            code, payload = server.dispatch("GET", "/api/pipeline", b"",
                                            ctx)
        self.assertEqual(code, 200)
        ids = [s["id"] for s in payload["stages"]]
        self.assertEqual(ids, ["parse", "risk", "worker", "repair",
                               "patch", "transfer"])


class TestJobs(unittest.TestCase):
    def test_unknown_job_404(self):
        ctx, tmp = make_ctx({})
        with tmp:
            code, payload = server.dispatch("GET", "/api/jobs/nope",
                                            b"", ctx)
        self.assertEqual(code, 404)
        self.assertIn("error", payload)

    def test_stop_unknown_job_404(self):
        ctx, tmp = make_ctx({})
        with tmp:
            code, _ = server.dispatch("POST", "/api/jobs/nope/stop",
                                      b"{}", ctx)
        self.assertEqual(code, 404)

    def test_run_unknown_kind_400(self):
        ctx, tmp = make_ctx({})
        with tmp:
            code, _ = server.dispatch("POST", "/api/run",
                                      json.dumps({"job": "nope"}).encode(),
                                      ctx)
        self.assertEqual(code, 400)

    def test_busy_guard_409(self):
        ctx, tmp = make_ctx({"slow": list(SLEEP_CMD)})
        with tmp:
            manager = ctx.manager
            try:
                code1, payload1 = server.dispatch(
                    "POST", "/api/run",
                    json.dumps({"job": "slow"}).encode(), ctx)
                self.assertEqual(code1, 200)
                job_id = payload1["job_id"]
                self.assertTrue(wait_until(lambda: manager.busy()
                                           is not None))
                code2, payload2 = server.dispatch(
                    "POST", "/api/run",
                    json.dumps({"job": "slow"}).encode(), ctx)
                self.assertEqual(code2, 409)
                self.assertIn("error", payload2)
            finally:
                for jid in list(manager._jobs):
                    manager.stop(jid)
                wait_until(lambda: manager.busy() is None)
            self.assertIsNone(manager.busy())

    def test_job_lifecycle_and_stop(self):
        ctx, tmp = make_ctx({"slow": list(SLEEP_CMD),
                             "fast": [sys.executable, "-c",
                                      "print('hi')"]})
        with tmp:
            manager = ctx.manager
            try:
                job_id, err = manager.launch("slow")
                self.assertIsNone(err)
                snap = manager.get(job_id)
                self.assertEqual(snap["state"], "running")
                self.assertTrue(manager.stop(job_id))
                self.assertTrue(wait_until(
                    lambda: manager.get(job_id)["state"] != "running"))
                # Slot freed: a fast job can run to completion.
                job2, err2 = manager.launch("fast")
                self.assertIsNone(err2)
                self.assertTrue(wait_until(
                    lambda: manager.get(job2)["state"] != "running"))
                snap2 = manager.get(job2)
                self.assertEqual(snap2["state"], "done")
                self.assertEqual(snap2["exit_code"], 0)
                self.assertIn("hi", "\n".join(snap2["tail"]))
            finally:
                for jid in list(manager._jobs):
                    manager.stop(jid)
                wait_until(lambda: manager.busy() is None)


class TestTodosAndSpend(unittest.TestCase):
    def test_parse_todos_counts(self):
        todos = server.parse_todos(ROOT)
        self.assertTrue(todos["found"])
        self.assertGreater(todos["total"], 0)
        self.assertEqual(todos["total"],
                         todos["open"] + todos["checked"])

    def test_spend_unknown_without_usage_files(self):
        with tempfile.TemporaryDirectory() as empty:
            spend = server.spend_tracker(Path(empty))
        self.assertFalse(spend["known"])
        self.assertIsNone(spend["spent_usd"])
        self.assertEqual(spend["budget_usd"], 50.0)


if __name__ == "__main__":
    unittest.main()
