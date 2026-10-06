"""Offline proof of the canonical A/B/C/D comparison (Family W).

Mirrors tests/test_run_abcd.py on the API-workflow transfer split
(9 tasks, stub only, zero live calls):

    A  9/9, all NOVEL, full model cost on every check.
    B  9/9, 8 ADAPT + 1 NOVEL (W-NOV-01 retrieves nothing).
    C  7/9: reuse wins 5, the trap adapts, novelty falls back, and
       both composites FAIL on the single-capability path (paginate
       alone leaves normalize/dedup undone).
    D  9/9 with 5 REUSE + 2 COMPOSE + 1 ADAPT + 1 NOVEL, including
       a searched 3-chain (paginate->normalize->dedup).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks import run_abcd, runner  # noqa: E402

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAMILY_W = ROOT / "benchmarks" / "family-w"
RECORDED = FAMILY_W / "recorded" / "stub_all_pass.json"

TOTAL_CHECKS = 18
TOTAL_TASKS = 9


def _run_arm(arm, tmp):
    tasks = sorted(
        (t for t in runner.load_tasks(FAMILY_W)
         if t.get("split") == "transfer"),
        key=lambda t: t["id"])
    self_check = runner.validate_tasks(tasks)
    assert not self_check, self_check
    return run_abcd.run_arm(
        arm, tasks, lambda: run_abcd.CountingStub(RECORDED), str(tmp),
        tasks_dir=FAMILY_W)


class FamilyWComparisonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.payloads = {arm: _run_arm(arm, cls.tmp.name + "-" + arm)
                        for arm in "abcd"}

    def _record(self, arm, task_id):
        for record in self.payloads[arm]["records"]:
            if record["id"] == task_id:
                return record
        self.fail("no record for %s/%s" % (arm, task_id))

    def test_arm_a_all_novel_full_cost(self):
        payload = self.payloads["a"]
        self.assertEqual(payload["tasks_passed"], "9/9")
        agg = payload["aggregate"]
        self.assertEqual(agg["outcomes"]["NOVEL"], 9)
        self.assertAlmostEqual(agg["calls_per_task"],
                               TOTAL_CHECKS / TOTAL_TASKS)

    def test_arm_b_adapt_except_novelty_probe(self):
        payload = self.payloads["b"]
        self.assertEqual(payload["tasks_passed"], "9/9")
        agg = payload["aggregate"]
        self.assertEqual(agg["outcomes"]["ADAPT"], 8)
        self.assertEqual(agg["outcomes"]["NOVEL"], 1)
        self.assertEqual(self._record("b", "W-NOV-01")["outcome"], "NOVEL")

    def test_arm_c_reuse_adapt_novelty_and_misfires(self):
        payload = self.payloads["c"]
        self.assertEqual(payload["tasks_passed"], "7/9")
        for task_id in ("W-REU-01", "W-REU-02", "W-REU-03", "W-REU-04",
                        "W-REU-05"):
            with self.subTest(task=task_id):
                record = self._record("c", task_id)
                self.assertEqual(record["outcome"], "REUSE")
                self.assertTrue(record["passed"])
                self.assertEqual(record["calls"], 0)
        trap = self._record("c", "W-ADP-01")
        self.assertEqual(trap["outcome"], "ADAPT")
        self.assertTrue(trap["passed"])
        novel = self._record("c", "W-NOV-01")
        self.assertEqual(novel["outcome"], "NOVEL")
        self.assertTrue(novel["passed"])
        for task_id in ("W-CMP-01", "W-CMP-02"):
            with self.subTest(task=task_id):
                record = self._record("c", task_id)
                self.assertEqual(record["outcome"], "REUSE")
                self.assertFalse(record["passed"])

    def test_arm_d_full_success_with_two_and_three_chains(self):
        payload = self.payloads["d"]
        self.assertEqual(payload["tasks_passed"], "9/9")
        agg = payload["aggregate"]
        self.assertEqual(agg["outcomes"],
                         {"REUSE": 5, "COMPOSE": 2, "ADAPT": 1, "NOVEL": 1})
        two = self._record("d", "W-CMP-01")
        self.assertEqual(two["via"],
                         ["seq:cap-paginate>cap-dedup"] * 2)
        three = self._record("d", "W-CMP-02")
        self.assertEqual(
            three["via"],
            ["seq:cap-paginate>cap-normalize>cap-dedup"] * 2)

    def test_dominance_trends_d_over_a(self):
        a = self.payloads["a"]["aggregate"]
        d = self.payloads["d"]["aggregate"]
        self.assertGreater(d["reuse_rate"], a["reuse_rate"])
        self.assertLess(d["calls_per_task"], a["calls_per_task"])
        self.assertLessEqual(d["tokens_per_task"], a["tokens_per_task"])
        self.assertGreaterEqual(d["success_rate"], a["success_rate"])

    def test_applicability_precision_full(self):
        app = self.payloads["d"]["applicability"]
        self.assertEqual(app["executed_tasks"], 7)
        self.assertAlmostEqual(app["retrieval_precision"], 1.0)
        self.assertEqual(app["false_positive_tasks"], 0)
        self.assertEqual(app["false_negative_checks"], 0)


if __name__ == "__main__":
    unittest.main()
