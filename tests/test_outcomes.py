"""Tests for outcomes.py: REUSE/COMPOSE/ADAPT/NOVEL taxonomy."""

import unittest

import outcomes


class ClassifyTest(unittest.TestCase):
    def test_reuse_single_capability_zero_calls(self):
        self.assertEqual(outcomes.classify(0, 1, False), outcomes.REUSE)

    def test_compose_multi_capability_zero_calls(self):
        self.assertEqual(outcomes.classify(0, 2, False), outcomes.COMPOSE)
        self.assertEqual(outcomes.classify(0, 5, True), outcomes.COMPOSE)

    def test_adapt_retrieved_but_model_needed(self):
        self.assertEqual(outcomes.classify(2, 0, True), outcomes.ADAPT)
        self.assertEqual(outcomes.classify(1, 1, True), outcomes.ADAPT)

    def test_novel_no_capability_no_retrieval(self):
        self.assertEqual(outcomes.classify(2, 0, False), outcomes.NOVEL)

    def test_degenerate_zero_calls_zero_exec(self):
        # Nothing executed and nothing retrieved: NOVEL, not free REUSE.
        self.assertEqual(outcomes.classify(0, 0, False), outcomes.NOVEL)
        self.assertEqual(outcomes.classify(0, 0, True), outcomes.ADAPT)


class AggregateTest(unittest.TestCase):
    def test_counts_and_means(self):
        records = [
            {"outcome": "REUSE", "calls": 0, "tokens": 0, "passed": True},
            {"outcome": "COMPOSE", "calls": 0, "tokens": 0, "passed": True},
            {"outcome": "ADAPT", "calls": 2, "tokens": 400, "passed": True},
            {"outcome": "NOVEL", "calls": 2, "tokens": 300, "passed": False},
        ]
        agg = outcomes.aggregate(records)
        self.assertEqual(agg["n"], 4)
        self.assertEqual(agg["outcomes"],
                         {"REUSE": 1, "COMPOSE": 1, "ADAPT": 1, "NOVEL": 1})
        self.assertAlmostEqual(agg["reuse_rate"], 0.5)
        self.assertAlmostEqual(agg["calls_per_task"], 1.0)
        self.assertAlmostEqual(agg["tokens_per_task"], 175.0)
        self.assertAlmostEqual(agg["success_rate"], 0.75)

    def test_empty_records(self):
        agg = outcomes.aggregate([])
        self.assertEqual(agg["n"], 0)
        self.assertEqual(agg["reuse_rate"], 0.0)

    def test_unknown_outcome_falls_back_to_novel(self):
        agg = outcomes.aggregate(
            [{"outcome": "BOGUS", "calls": 1, "tokens": 1, "passed": True}])
        self.assertEqual(agg["outcomes"]["NOVEL"], 1)


class CompressionTest(unittest.TestCase):
    def test_healthy_compression(self):
        sig = outcomes.compression(12, 4)
        self.assertAlmostEqual(sig["tasks_per_capability"], 3.0)
        self.assertFalse(sig["warning"])

    def test_memorization_warning(self):
        # One function per task with a growing library warns.
        sig = outcomes.compression(6, 6)
        self.assertTrue(sig["warning"])

    def test_empty_library(self):
        sig = outcomes.compression(0, 0)
        self.assertIsNone(sig["tasks_per_capability"])
        self.assertFalse(sig["warning"])


if __name__ == "__main__":
    unittest.main()
