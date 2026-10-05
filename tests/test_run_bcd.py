"""Focused tests for run_bcd.py partial runs and A references.

Covers --phase2-ids validation, the partial comparison block, and
transfer-2 A-reference loading. Stub/offline only; the loader test
skips when the (gitignored) transfer2 validation artifacts are
absent. No live calls.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks import run_bcd, runner

FAMILY_DIR = Path(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))) / "benchmarks" / "family-a"


def _record(task_id, passed):
    return {
        "phase": "transfer",
        "memory": {"retrieval": {}},
        "summary": {"tasks": [{
            "id": task_id, "split": "transfer", "passed": passed,
            "checks": [], "usage": [],
        }]},
    }


class Phase2IdsTest(unittest.TestCase):
    def test_rejects_unknown_and_exposure_ids(self):
        tasks = runner.load_tasks(FAMILY_DIR)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stub = runner.StubAdapter(
            FAMILY_DIR / "recorded" / "stub_all_pass.json")
        with self.assertRaises(ValueError):
            run_bcd.run_baseline(
                "b", tasks, tmp.name, stub, {},
                phase2_ids=["A-TRN-09", "A-EXP-01", "BOGUS"])

    def test_accepts_transfer_ids(self):
        tasks = runner.load_tasks(FAMILY_DIR)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stub = runner.StubAdapter(
            FAMILY_DIR / "recorded" / "stub_all_pass.json")
        with contextlib.redirect_stdout(io.StringIO()):
            summary = run_bcd.run_baseline(
                "b", tasks, tmp.name, stub, {},
                phase2_ids=["A-TRN-09", "A-TRN-10"])
        self.assertEqual(summary["tasks_total"], 18 + 2)
        self.assertTrue(
            summary["comparison_vs_a_stripped"]["partial"])


class ComparisonBlockTest(unittest.TestCase):
    def test_partial_with_transfer2_table(self):
        phase2 = [_record("A-TRN-09", True), _record("A-TRN-11", False)]
        a2 = {"passed": 6, "total": 8,
              "fail_ids": {"A-TRN-09", "A-TRN-10"}}
        block = run_bcd._comparison_block(
            10, 12, {}, phase2, True, a2,
            set(run_bcd.A_STRIPPED["fail_ids"]) | a2["fail_ids"])
        self.assertTrue(block["partial"])
        self.assertTrue(block["population_mismatch"])
        t2 = block["transfer2_vs_a"]
        self.assertEqual((t2["run_passed"], t2["run_total"]), (1, 2))
        self.assertEqual(t2["improvements"], ["A-TRN-09"])
        self.assertEqual(t2["regressions"], ["A-TRN-11"])

    def test_withheld_without_reference(self):
        phase2 = [_record("A-TRN-09", True)]
        block = run_bcd._comparison_block(
            10, 12, {}, phase2, True,
            {"passed": 0, "total": 0, "fail_ids": set()},
            set(run_bcd.A_STRIPPED["fail_ids"]))
        self.assertIn("withheld", block["transfer2_vs_a"])

    def test_full_original_run_has_no_mismatch(self):
        block = run_bcd._comparison_block(
            30, 35, {}, [], False,
            {"passed": 0, "total": 0, "fail_ids": set()},
            set(run_bcd.A_STRIPPED["fail_ids"]))
        self.assertFalse(block["partial"])
        self.assertFalse(block["population_mismatch"])
        self.assertNotIn("transfer2_vs_a", block)


class Transfer2LoaderTest(unittest.TestCase):
    def test_loads_validation_outcomes(self):
        transfer2_dir = (Path(run_bcd.__file__).resolve().parent.parent
                         / "artifacts" / "transfer2" / "stripped")
        if not transfer2_dir.is_dir():
            self.skipTest("transfer2 validation artifacts absent")
        a2 = run_bcd.load_a_transfer2(str(FAMILY_DIR))
        self.assertEqual(a2["total"], 8)
        self.assertEqual(a2["passed"], 6)
        self.assertEqual(a2["fail_ids"], {"A-TRN-09", "A-TRN-10"})


if __name__ == "__main__":
    unittest.main()
