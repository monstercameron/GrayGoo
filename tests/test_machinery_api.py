"""Backend tests for GET /api/agent/machinery (the Thesis section's evidence).

No network binds: the route is exercised through server.dispatch() with both
data modules faked in sys.modules. A None entry makes `import` fail, which is
how a module that does not exist yet looks to the route.
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SERVER_PATH = ROOT / "dashboard" / "server.py"


def load_server():
    spec = importlib.util.spec_from_file_location("dashboard_server_machinery",
                                                  SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = load_server()

REPORT = {"mode": "live", "project": None, "builds": 39, "functions_saved": 180,
          "spent_usd": 2.31, "model_calls": 700, "seconds": 2100.0,
          "window": {"first": 1791600000.0, "last": 1791640000.0},
          "periods": [], "mechanisms": [], "saved": {"calls": 457, "usd": 0.9, "seconds": 1200.0},
          "notes": ["first note"]}
REPLAY = {"cases": 612, "sessions": 39, "raw_pass": 301, "fixed_pass": 398,
          "raw_rate": 0.492, "fixed_rate": 0.650, "rescued": 101, "broken": 4,
          "rescued_usd": 0.42, "by_kind": {}, "by_fix": [], "notes": [],
          "generated": 1791640000.0, "seconds": 240.0}


def fake_module(name, **attrs):
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def make_ctx():
    # The agent block runs _mounts(ctx) first; a preset ctx.mounts skips it.
    return SimpleNamespace(root=ROOT, manager=SimpleNamespace(),
                           agent=SimpleNamespace(), mounts=object())


class MachineryRouteTests(unittest.TestCase):
    def get(self, path, modules):
        with mock.patch.dict(sys.modules, modules):
            code, payload = server.dispatch("GET", path, b"", make_ctx())
        self.assertEqual(code, 200)
        return payload

    def test_both_modules_present(self):
        spec = fake_module("specialization", report=mock.Mock(return_value=REPORT))
        replay = fake_module("replay_evidence", load=mock.Mock(return_value=REPLAY))
        payload = self.get("/api/agent/machinery",
                           {"specialization": spec, "replay_evidence": replay})
        self.assertEqual(payload, {"report": REPORT, "replay": REPLAY})

    def test_both_modules_missing(self):
        payload = self.get("/api/agent/machinery",
                           {"specialization": None, "replay_evidence": None})
        self.assertEqual(payload, {"report": None, "replay": None})

    def test_report_module_missing(self):
        replay = fake_module("replay_evidence", load=mock.Mock(return_value=REPLAY))
        payload = self.get("/api/agent/machinery",
                           {"specialization": None, "replay_evidence": replay})
        self.assertEqual(payload, {"report": None, "replay": REPLAY})

    def test_replay_module_missing(self):
        spec = fake_module("specialization", report=mock.Mock(return_value=REPORT))
        payload = self.get("/api/agent/machinery",
                           {"specialization": spec, "replay_evidence": None})
        self.assertEqual(payload, {"report": REPORT, "replay": None})

    def test_report_raising_becomes_null(self):
        spec = fake_module("specialization",
                           report=mock.Mock(side_effect=RuntimeError("boom")))
        replay = fake_module("replay_evidence", load=mock.Mock(return_value=REPLAY))
        payload = self.get("/api/agent/machinery",
                           {"specialization": spec, "replay_evidence": replay})
        self.assertEqual(payload, {"report": None, "replay": REPLAY})

    def test_replay_raising_becomes_null(self):
        spec = fake_module("specialization", report=mock.Mock(return_value=REPORT))
        replay = fake_module("replay_evidence",
                             load=mock.Mock(side_effect=ValueError("bad log")))
        payload = self.get("/api/agent/machinery",
                           {"specialization": spec, "replay_evidence": replay})
        self.assertEqual(payload, {"report": REPORT, "replay": None})

    def test_non_dict_output_becomes_null(self):
        spec = fake_module("specialization", report=mock.Mock(return_value=["not", "a", "dict"]))
        replay = fake_module("replay_evidence", load=mock.Mock(return_value=None))
        payload = self.get("/api/agent/machinery",
                           {"specialization": spec, "replay_evidence": replay})
        self.assertEqual(payload, {"report": None, "replay": None})

    def test_project_query_is_passed_through(self):
        report = mock.Mock(return_value=REPORT)
        spec = fake_module("specialization", report=report)
        replay = fake_module("replay_evidence", load=mock.Mock(return_value=REPLAY))
        modules = {"specialization": spec, "replay_evidence": replay}
        self.get("/api/agent/machinery?project=alpha", modules)
        self.assertEqual(report.call_args.kwargs["project"], "alpha")
        self.get("/api/agent/machinery", modules)
        self.assertIsNone(report.call_args.kwargs["project"])
        self.get("/api/agent/machinery?project=", modules)
        self.assertIsNone(report.call_args.kwargs["project"])


if __name__ == "__main__":
    unittest.main()
