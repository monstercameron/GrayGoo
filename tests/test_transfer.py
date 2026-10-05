"""Tests for transfer-based promotion (transfer.py). Stdlib only."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transfer
from transfer import TransferTracker, promote_or_hold


def good_outcome(task_id, tokens=100, latency=50.0, **kw):
    row = {"patch_id": "p1", "task_id": task_id, "helped": True,
           "tokens_saved": tokens, "latency_saved_ms": latency}
    row.update(kw)
    return row


class PromoteGateTest(unittest.TestCase):
    def test_promote_after_three_good_reuses(self):
        outcomes = [good_outcome("t1"), good_outcome("t2"),
                    good_outcome("t3")]
        result = promote_or_hold("p1", outcomes)
        self.assertEqual(result["decision"], "promote")
        self.assertTrue(result["reasons"])

    def test_hold_with_insufficient_evidence(self):
        result = promote_or_hold("p1", [good_outcome("t1"),
                                       good_outcome("t2")])
        self.assertEqual(result["decision"], "hold")
        self.assertTrue(any("independent" in r.lower()
                            for r in result["reasons"]))

    def test_independent_reuses_count_distinct_tasks(self):
        # Three rows on the same task count as one independent reuse.
        outcomes = [good_outcome("t1"), good_outcome("t1"),
                    good_outcome("t1")]
        result = promote_or_hold("p1", outcomes)
        self.assertEqual(result["decision"], "hold")

    def test_retire_on_severe_negative_transfer(self):
        outcomes = [good_outcome("t1"), good_outcome("t2"),
                    good_outcome("t3", severe=True)]
        result = promote_or_hold("p1", outcomes)
        self.assertEqual(result["decision"], "retire")
        self.assertTrue(any("severe" in r.lower()
                            for r in result["reasons"]))

    def test_hold_when_held_out_below_baseline(self):
        outcomes = [good_outcome("t1", helped=True, held_out=True),
                    good_outcome("t2", helped=False, held_out=True),
                    good_outcome("t3", helped=False, held_out=True),
                    good_outcome("t4", helped=False, held_out=True)]
        result = promote_or_hold("p1", outcomes, baseline_success=0.9)
        self.assertEqual(result["decision"], "hold")

    def test_hold_on_resource_regression(self):
        outcomes = [good_outcome("t1", tokens=-5000),
                    good_outcome("t2", tokens=-5000),
                    good_outcome("t3", tokens=-5000)]
        result = promote_or_hold("p1", outcomes, max_token_regression=100.0)
        self.assertEqual(result["decision"], "hold")


class DeltaMathTest(unittest.TestCase):
    def test_success_token_latency_deltas(self):
        outcomes = [good_outcome("t1", tokens=100, latency=10.0),
                    good_outcome("t2", tokens=200, latency=20.0),
                    {"patch_id": "p1", "task_id": "t3", "helped": False,
                     "tokens_saved": -50, "latency_saved_ms": -5.0}]
        metrics = transfer.summarize_outcomes(outcomes, baseline_success=0.5)
        self.assertEqual(metrics["reuse_count"], 3)
        self.assertEqual(metrics["independent_reuses"], 3)
        self.assertAlmostEqual(metrics["success_rate"], 2.0 / 3.0)
        self.assertAlmostEqual(metrics["success_delta"], 2.0 / 3.0 - 0.5)
        self.assertEqual(metrics["tokens_saved_total"], 250)
        self.assertEqual(metrics["token_delta"], 250)
        self.assertAlmostEqual(metrics["latency_saved_ms_total"], 25.0)
        self.assertEqual(metrics["negative_transfer_count"], 1)
        self.assertEqual(metrics["severe_regressions"], 0)

    def test_negative_transfer_counts(self):
        outcomes = [good_outcome("t1"),
                    good_outcome("t2", helped=False),
                    good_outcome("t3", helped=False, severe=True)]
        metrics = transfer.summarize_outcomes(outcomes)
        self.assertEqual(metrics["negative_transfer_count"], 2)
        self.assertEqual(metrics["severe_regressions"], 1)

    def test_empty_outcomes_hold(self):
        result = promote_or_hold("p1", [])
        self.assertEqual(result["decision"], "hold")


class TrackerTest(unittest.TestCase):
    def test_tracker_records_and_evaluates(self):
        tracker = TransferTracker()
        tracker.record_outcome("p1", "t1", helped=True, tokens_saved=10,
                               latency_saved_ms=5.0)
        tracker.record_outcome("p1", "t2", helped=True, tokens_saved=10,
                               latency_saved_ms=5.0)
        before = tracker.promote_or_hold("p1")
        self.assertEqual(before["decision"], "hold")
        tracker.record_outcome("p1", "t3", helped=True, tokens_saved=10,
                               latency_saved_ms=5.0)
        after = tracker.promote_or_hold("p1")
        self.assertEqual(after["decision"], "promote")
        self.assertEqual(after["metrics"]["independent_reuses"], 3)
        self.assertEqual(len(tracker.get_outcomes("p1")), 3)

    def test_tracker_persists_when_given_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = TransferTracker(directory=tmp)
            tracker.record_outcome("p1", "t1", helped=True)
            reopened = TransferTracker(directory=tmp)
            self.assertEqual(len(reopened.get_outcomes("p1")), 1)

    def test_tracker_retires_on_severe(self):
        tracker = TransferTracker()
        tracker.record_outcome("p1", "t1", helped=True)
        tracker.record_outcome("p1", "t2", helped=True)
        tracker.record_outcome("p1", "t3", helped=False, severe=True)
        result = tracker.promote_or_hold("p1")
        self.assertEqual(result["decision"], "retire")


if __name__ == "__main__":
    unittest.main()
