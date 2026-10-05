"""Offline proof of the canonical A/B/C/D comparison (Family R).

Runs all four arms with the counting stub (zero live calls) and pins
the honest separation the thesis predicts:

    A  7/7, all NOVEL, full model cost on every check.
    B  7/7, all ADAPT, same calls as A at higher token cost.
    C  5/7: reuse wins 4, the trap abstains correctly, and the two
       composite tasks FAIL on the single-capability path -- the
       misfire that motivates composition (arm D), pinned here so a
       future applicability upgrade must move it deliberately.
    D  7/7 with 4 REUSE + 2 COMPOSE + 1 NOVEL at 1 total model call.

Token figures are chars/4 estimates on the stub path (see
CountingStub); the live comparison re-measures with real usage.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks import run_abcd, runner  # noqa: E402

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAMILY_R = ROOT / "benchmarks" / "family-r"
RECORDED = FAMILY_R / "recorded" / "stub_all_pass.json"


def _run_arm(arm, tmp):
    tasks = sorted(runner.load_tasks(FAMILY_R), key=lambda t: t["id"])
    self_check = runner.validate_tasks(tasks)
    assert not self_check, self_check
    return run_abcd.run_arm(
        arm, tasks,
        lambda: run_abcd.CountingStub(RECORDED), str(tmp))


class CanonicalComparisonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.payloads = {arm: _run_arm(arm, cls.tmp.name
                                      + "-" + arm)
                        for arm in "abcd"}

    def _record(self, arm, task_id):
        for record in self.payloads[arm]["records"]:
            if record["id"] == task_id:
                return record
        self.fail("no record for %s/%s" % (arm, task_id))

    def test_arm_a_all_novel_full_cost(self):
        payload = self.payloads["a"]
        self.assertEqual(payload["tasks_passed"], "7/7")
        agg = payload["aggregate"]
        self.assertEqual(agg["outcomes"]["NOVEL"], 7)
        self.assertAlmostEqual(agg["calls_per_task"], 13 / 7)
        self.assertAlmostEqual(agg["success_rate"], 1.0)

    def test_arm_b_all_adapt_costs_more_tokens(self):
        payload = self.payloads["b"]
        self.assertEqual(payload["tasks_passed"], "7/7")
        agg = payload["aggregate"]
        self.assertEqual(agg["outcomes"]["ADAPT"], 7)
        self.assertAlmostEqual(
            agg["calls_per_task"],
            self.payloads["a"]["aggregate"]["calls_per_task"])
        self.assertGreater(
            agg["tokens_per_task"],
            self.payloads["a"]["aggregate"]["tokens_per_task"])

    def test_arm_c_reuse_wins_trap_abstains_composites_misfire(self):
        payload = self.payloads["c"]
        self.assertEqual(payload["tasks_passed"], "5/7")
        for task_id in ("R-REU-01", "R-REU-02", "R-REU-03", "R-REU-04"):
            with self.subTest(task=task_id):
                record = self._record("c", task_id)
                self.assertEqual(record["outcome"], "REUSE")
                self.assertTrue(record["passed"])
                self.assertEqual(record["calls"], 0)
        trap = self._record("c", "R-ADP-01")
        self.assertEqual(trap["outcome"], "NOVEL")
        self.assertTrue(trap["passed"])
        self.assertEqual(trap["calls"], 1)
        for task_id in ("R-CMP-01", "R-CMP-02"):
            with self.subTest(task=task_id):
                record = self._record("c", task_id)
                self.assertEqual(record["outcome"], "REUSE")
                self.assertFalse(record["passed"])

    def test_arm_d_full_success_one_call(self):
        payload = self.payloads["d"]
        self.assertEqual(payload["tasks_passed"], "7/7")
        agg = payload["aggregate"]
        self.assertEqual(agg["outcomes"],
                         {"REUSE": 4, "COMPOSE": 2, "ADAPT": 0, "NOVEL": 1})
        self.assertAlmostEqual(agg["calls_per_task"], 1 / 7)
        self.assertAlmostEqual(agg["success_rate"], 1.0)

    def test_dominance_trends_d_over_a(self):
        a = self.payloads["a"]["aggregate"]
        d = self.payloads["d"]["aggregate"]
        self.assertGreater(d["reuse_rate"], a["reuse_rate"])
        self.assertLess(d["calls_per_task"], a["calls_per_task"])
        self.assertLessEqual(d["tokens_per_task"], a["tokens_per_task"])
        self.assertGreaterEqual(d["success_rate"], a["success_rate"])

    def test_compression_counts_capabilities_as_warning_input(self):
        payload = self.payloads["d"]
        self.assertEqual(payload["capability_count"], 7)
        sig = payload["compression"]
        self.assertAlmostEqual(sig["tasks_per_capability"], 1.0)
        self.assertTrue(sig["warning"])


if __name__ == "__main__":
    unittest.main()
