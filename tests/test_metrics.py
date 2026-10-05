"""Tests for the learning-measurement harness (metrics.py). Stdlib only."""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import metrics
from metrics import (
    entropy_from_counts,
    held_out_success,
    negative_transfer_a_relative,
    negative_transfer_outcome,
    resources_per_task,
    reuse_rate,
    success_rate,
    time_to_verified_mutation,
    transfer_success,
    trend,
)


def task(task_id, passed, split="transfer", phase="transfer", **kw):
    record = {"id": task_id, "split": split, "phase": phase,
              "passed": passed, "calls": 2, "input_tokens": 100,
              "output_tokens": 50, "total_tokens": 150,
              "latency_ms": 500.0, "cost_usd": 0.0001,
              "text_hit": False, "patch_reuse": False,
              "text_sources": [], "patch_sources": [], "stored": 0}
    record.update(kw)
    return record


def check_envelope(test, result):
    test.assertIn("value", result)
    test.assertIn("n", result)
    test.assertIn("basis", result)
    test.assertIsInstance(result["basis"], str)
    test.assertTrue(result["basis"])


class TrendTest(unittest.TestCase):
    def test_increasing(self):
        result = trend([1.0, 2.0, 3.0, 4.0])
        check_envelope(self, result)
        self.assertEqual(result["value"]["direction"], "increasing")
        self.assertGreater(result["value"]["slope"], 0)
        self.assertEqual(result["n"], 4)

    def test_decreasing(self):
        result = trend([4.0, 3.0, 2.0, 1.0])
        self.assertEqual(result["value"]["direction"], "decreasing")
        self.assertLess(result["value"]["slope"], 0)

    def test_flat_constant(self):
        result = trend([2.0, 2.0, 2.0, 2.0])
        self.assertEqual(result["value"]["direction"], "flat")
        self.assertEqual(result["value"]["slope"], 0.0)

    def test_flat_within_tolerance(self):
        result = trend([100.0, 100.5, 99.8, 100.2])
        self.assertEqual(result["value"]["direction"], "flat")

    def test_small_change_detected_with_tight_tolerance(self):
        result = trend([100.0, 101.0, 102.0, 103.0], tolerance=0.001)
        self.assertEqual(result["value"]["direction"], "increasing")

    def test_single_timepoint_is_tbd(self):
        result = trend([1.0])
        check_envelope(self, result)
        self.assertIsNone(result["value"])
        self.assertEqual(result["n"], 0)

    def test_empty_series_is_tbd(self):
        result = trend([])
        self.assertIsNone(result["value"])
        self.assertEqual(result["n"], 0)

    def test_missing_values_are_tbd(self):
        result = trend([1.0, None, 3.0])
        self.assertIsNone(result["value"])
        self.assertEqual(result["n"], 0)


class EntropyTest(unittest.TestCase):
    def test_uniform_over_four_is_ln4(self):
        result = entropy_from_counts([5, 5, 5, 5])
        check_envelope(self, result)
        self.assertAlmostEqual(result["value"], math.log(4))
        self.assertEqual(result["n"], 20)

    def test_degenerate_distribution_is_zero(self):
        result = entropy_from_counts([12])
        self.assertAlmostEqual(result["value"], 0.0)

    def test_fair_coin_is_ln2(self):
        result = entropy_from_counts([1, 1])
        self.assertAlmostEqual(result["value"], math.log(2))

    def test_skewed_less_than_uniform(self):
        skewed = entropy_from_counts([9, 1])["value"]
        uniform = entropy_from_counts([5, 5])["value"]
        self.assertLess(skewed, uniform)

    def test_empty_counts_is_tbd(self):
        result = entropy_from_counts([])
        check_envelope(self, result)
        self.assertIsNone(result["value"])
        self.assertEqual(result["n"], 0)


class ARelativeTransferTest(unittest.TestCase):
    def setUp(self):
        self.base = [task("t1", True), task("t2", True),
                     task("t3", False), task("t4", False)]
        self.exp = [task("t1", False), task("t2", True),
                    task("t3", True), task("t4", False)]

    def test_counts_regressions_and_improvements(self):
        result = negative_transfer_a_relative(self.base, self.exp)
        check_envelope(self, result)
        value = result["value"]
        self.assertEqual(value["regressions"], 1)
        self.assertEqual(value["regressed_ids"], ["t1"])
        self.assertEqual(value["improvements"], 1)
        self.assertEqual(value["improved_ids"], ["t3"])
        self.assertEqual(result["n"], 4)

    def test_restricts_to_given_task_ids(self):
        result = negative_transfer_a_relative(self.base, self.exp,
                                              task_ids=["t1", "t2"])
        self.assertEqual(result["value"]["regressions"], 1)
        self.assertEqual(result["value"]["improvements"], 0)
        self.assertEqual(result["n"], 2)

    def test_no_comparable_tasks_is_tbd(self):
        result = negative_transfer_a_relative(self.base, self.exp,
                                              task_ids=["zzz"])
        check_envelope(self, result)
        self.assertIsNone(result["value"])
        self.assertEqual(result["n"], 0)

    def test_outcome_level_counts_bad_rows(self):
        rows = [{"patch_id": "p", "task_id": "t1", "helped": True},
                {"patch_id": "p", "task_id": "t2", "helped": False},
                {"patch_id": "p", "task_id": "t3", "helped": False,
                 "severe": True}]
        result = negative_transfer_outcome(rows)
        check_envelope(self, result)
        self.assertEqual(result["value"]["bad"], 2)
        self.assertEqual(result["value"]["severe"], 1)
        self.assertAlmostEqual(result["value"]["rate"], 2 / 3)


class GracefulTbdTest(unittest.TestCase):
    def test_success_rate_empty(self):
        result = success_rate([])
        self.assertIsNone(result["value"])
        self.assertEqual(result["n"], 0)

    def test_held_out_with_no_transfer_tasks(self):
        tasks = [task("e1", True, split="exposure", phase="exposure")]
        result = held_out_success(tasks)
        self.assertIsNone(result["value"])

    def test_transfer_success_without_held_out_rows(self):
        rows = [{"patch_id": "p", "task_id": "t1", "helped": True,
                 "held_out": False}]
        result = transfer_success(rows)
        self.assertIsNone(result["value"])

    def test_resources_empty(self):
        result = resources_per_task([])
        self.assertIsNone(result["value"])

    def test_time_to_verified_mutation_empty(self):
        result = time_to_verified_mutation([])
        check_envelope(self, result)
        self.assertIsNone(result["value"])
        self.assertEqual(result["n"], 0)

    def test_reuse_rate_without_phase2(self):
        tasks = [task("e1", True, split="exposure", phase="exposure")]
        result = reuse_rate(tasks)
        self.assertIsNone(result["value"])


class SuccessAndReuseTest(unittest.TestCase):
    def test_success_rate_values(self):
        tasks = [task("t1", True), task("t2", False)]
        result = success_rate(tasks)
        check_envelope(self, result)
        self.assertAlmostEqual(result["value"], 0.5)
        self.assertEqual(result["n"], 2)

    def test_reuse_rate_counts_either_memory(self):
        tasks = [task("t1", True, text_hit=True),
                 task("t2", True, patch_reuse=True),
                 task("t3", False)]
        result = reuse_rate(tasks)
        self.assertAlmostEqual(result["value"], 2 / 3)
        self.assertEqual(result["n"], 3)

    def test_time_to_verified_mutation_stats(self):
        samples = [{"total_ms": 100.0}, {"total_ms": 200.0},
                   {"total_ms": 300.0}]
        result = time_to_verified_mutation(samples)
        check_envelope(self, result)
        self.assertAlmostEqual(result["value"]["mean_ms"], 200.0)
        self.assertAlmostEqual(result["value"]["median_ms"], 200.0)
        self.assertEqual(result["value"]["min_ms"], 100.0)
        self.assertEqual(result["value"]["max_ms"], 300.0)


if __name__ == "__main__":
    unittest.main()
