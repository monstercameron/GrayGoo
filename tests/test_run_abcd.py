"""Offline proof of the canonical A/B/C/D comparison (Family R).

Runs all four arms with the counting stub (zero live calls) and pins
the honest separation the thesis predicts:

    A  8/8, all NOVEL, full model cost on every check.
    B  8/8, 7 ADAPT + 1 NOVEL (nothing retrieves for R-NOV-01),
       same calls as A at higher token cost.
    C  6/8: reuse wins 4, the trap adapts (retrieved context + one
       model call), the novelty probe falls back to scratch, and the
       two composite tasks FAIL on the single-capability path -- the
       misfire that motivates composition (arm D), pinned here so a
       future applicability upgrade must move it deliberately.
    D  8/8 with 4 REUSE + 2 COMPOSE + 1 ADAPT + 1 NOVEL at 3 total
       model calls (trap 1 check + novelty 2 checks).

Token figures are chars/4 estimates on the stub path (see
CountingStub); the live comparison re-measures with real usage.
"""

import json
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

# 13 checks on the original 7 tasks + 2 on the novelty probe.
TOTAL_CHECKS = 15
TOTAL_TASKS = 8


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
        self.assertEqual(payload["tasks_passed"], "8/8")
        agg = payload["aggregate"]
        self.assertEqual(agg["outcomes"]["NOVEL"], 8)
        self.assertAlmostEqual(agg["calls_per_task"],
                               TOTAL_CHECKS / TOTAL_TASKS)
        self.assertAlmostEqual(agg["success_rate"], 1.0)

    def test_arm_b_adapt_except_novelty_probe(self):
        payload = self.payloads["b"]
        self.assertEqual(payload["tasks_passed"], "8/8")
        agg = payload["aggregate"]
        self.assertEqual(agg["outcomes"]["ADAPT"], 7)
        self.assertEqual(agg["outcomes"]["NOVEL"], 1)
        self.assertEqual(self._record("b", "R-NOV-01")["outcome"], "NOVEL")
        self.assertAlmostEqual(
            agg["calls_per_task"],
            self.payloads["a"]["aggregate"]["calls_per_task"])
        self.assertGreater(
            agg["tokens_per_task"],
            self.payloads["a"]["aggregate"]["tokens_per_task"])

    def test_arm_c_reuse_adapt_novelty_and_misfires(self):
        payload = self.payloads["c"]
        self.assertEqual(payload["tasks_passed"], "6/8")
        for task_id in ("R-REU-01", "R-REU-02", "R-REU-03", "R-REU-04"):
            with self.subTest(task=task_id):
                record = self._record("c", task_id)
                self.assertEqual(record["outcome"], "REUSE")
                self.assertTrue(record["passed"])
                self.assertEqual(record["calls"], 0)
        trap = self._record("c", "R-ADP-01")
        self.assertEqual(trap["outcome"], "ADAPT")
        self.assertTrue(trap["passed"])
        self.assertEqual(trap["calls"], 1)
        novel = self._record("c", "R-NOV-01")
        self.assertEqual(novel["outcome"], "NOVEL")
        self.assertTrue(novel["passed"])
        self.assertEqual(novel["calls"], 2)
        for task_id in ("R-CMP-01", "R-CMP-02"):
            with self.subTest(task=task_id):
                record = self._record("c", task_id)
                self.assertEqual(record["outcome"], "REUSE")
                self.assertFalse(record["passed"])

    def test_arm_d_full_success_three_calls(self):
        payload = self.payloads["d"]
        self.assertEqual(payload["tasks_passed"], "8/8")
        agg = payload["aggregate"]
        self.assertEqual(agg["outcomes"],
                         {"REUSE": 4, "COMPOSE": 2, "ADAPT": 1, "NOVEL": 1})
        self.assertAlmostEqual(agg["calls_per_task"], 3 / 8)
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
        self.assertEqual(payload["capability_count"], 15)
        sig = payload["compression"]
        self.assertAlmostEqual(sig["tasks_per_capability"], 8 / 15)
        self.assertTrue(sig["warning"])

    def test_evidence_ledgers_track_trusted_outcomes(self):
        evidence = self.payloads["d"]["evidence"]
        # Composite members share composite evidence (§9 propagation):
        # cap-date-iso earns R-REU-01 directly plus R-CMP-01 as a
        # composite member.
        self.assertEqual(sorted(evidence["cap-date-iso"]["positive"]),
                         ["R-CMP-01", "R-REU-01"])
        self.assertEqual(evidence["cap-date-iso"]["negative"], [])
        self.assertIn("R-CMP-01",
                      evidence["cap-csv-parse"]["positive"])
        self.assertIn("R-REU-02",
                      evidence["cap-csv-parse"]["positive"])
        self.assertIn("R-CMP-02", evidence["cap-table-csv"]["positive"])
        self.assertEqual(
            evidence["map:cap-csv-parse[date]>cap-date-iso"]["positive"],
            ["R-CMP-01"])
        # Arm C records the misfires as negative evidence.
        c_evidence = self.payloads["c"]["evidence"]
        self.assertIn("R-CMP-01",
                      c_evidence["cap-csv-parse"]["negative"])
        self.assertIn("R-CMP-02", c_evidence["cap-flatten"]["negative"])

    def test_applicability_metrics(self):
        app = self.payloads["d"]["applicability"]
        self.assertEqual(app["executed_tasks"], 6)
        self.assertAlmostEqual(app["retrieval_precision"], 1.0)
        self.assertAlmostEqual(app["harmful_reuse_rate"], 0.0)
        self.assertEqual(app["false_positive_tasks"], 0)
        # The trap + novelty fallbacks are genuine: no same-category
        # capability would have passed those checks counterfactually.
        self.assertEqual(app["false_negative_checks"], 0)
        c_app = self.payloads["c"]["applicability"]
        self.assertAlmostEqual(c_app["retrieval_precision"], 4 / 6)
        self.assertEqual(c_app["false_positive_tasks"], 2)

    def test_curves_track_run_order(self):
        curves = self.payloads["d"]["curves"]
        self.assertEqual(curves["n"], list(range(1, 9)))
        # Final prefix equals the aggregate means.
        agg = self.payloads["d"]["aggregate"]
        self.assertAlmostEqual(curves["calls_per_task"][-1],
                               agg["calls_per_task"])
        self.assertAlmostEqual(curves["success_rate"][-1],
                               agg["success_rate"])

    def test_promotion_rule(self):
        evidence = self.payloads["d"]["evidence"]
        arm_rate = self.payloads["d"]["aggregate"]["success_rate"]
        base_rate = self.payloads["a"]["aggregate"]["success_rate"]
        decisions = run_abcd.assess_promotion(evidence, arm_rate,
                                               base_rate)
        # Two independent reuse tasks -> stable.
        self.assertEqual(decisions["cap-date-iso"]["status"], "stable")
        # Single reuse task -> provisional, never stable on one hit.
        self.assertEqual(decisions["cap-clf-parse"]["status"],
                         "provisional")
        # Misfires quarantine in arm C.
        c_evidence = self.payloads["c"]["evidence"]
        c_rate = self.payloads["c"]["aggregate"]["success_rate"]
        c_decisions = run_abcd.assess_promotion(c_evidence, c_rate,
                                                 base_rate)
        self.assertEqual(c_decisions["cap-csv-parse"]["status"],
                         "quarantined")

    def test_thesis_verdict_supported_offline(self):
        verdict = run_abcd.thesis_verdict(self.payloads)
        self.assertTrue(verdict["checks"]["success_held"])
        self.assertTrue(verdict["checks"]["calls_fell"])
        self.assertTrue(verdict["checks"]["tokens_fell"])
        self.assertTrue(verdict["supported"])
        # Growth is still one-for-one here, so §15 stays failed:
        # the verdict must report it, not hide it.
        self.assertFalse(verdict["checks"]["growth_sublinear"])
        self.assertTrue(verdict["failed_section_15"])


class MultiOrderTest(unittest.TestCase):
    def test_three_orders_all_supported(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        code = run_abcd.main(
            ["--adapter", "stub", "--arms", "ad",
             "--artifacts", tmp.name, "--orders", "3", "--seed", "7"])
        self.assertEqual(code, 0)
        with open(Path(tmp.name) / "comparison.json",
                  encoding="utf-8") as fh:
            comparison = json.load(fh)
        self.assertEqual(comparison["orders"], 3)
        self.assertEqual(len(comparison["verdicts"]), 3)
        for verdict in comparison["verdicts"]:
            self.assertTrue(verdict["supported"])
        self.assertTrue(comparison["verdict_mean"]["supported"])


if __name__ == "__main__":
    unittest.main()
