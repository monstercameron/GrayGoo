"""Tests for lesson mining (L3) and replay validation (L4). Stdlib only."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import events
import mine
import replay
import trajectories


def _steps_with(*failure_classes):
    steps = []
    for i, cls in enumerate(failure_classes):
        steps.append({"candidate": "cand-%d" % i,
                      "failure": ({"class": cls} if cls else None),
                      "repair": None, "success": cls is None})
    return steps


class ClusteringTest(unittest.TestCase):
    def setUp(self):
        self.ledger = events.EventLedger(":memory:")
        trajectories.record_trajectory(
            self.ledger, "a", _steps_with("wrong-output", "wrong-output"),
            family="csv")
        trajectories.record_trajectory(
            self.ledger, "b", _steps_with("wrong-output"), family="csv")
        trajectories.record_trajectory(
            self.ledger, "c", _steps_with("timeout"), family="pagination")
        trajectories.record_trajectory(
            self.ledger, "d", _steps_with(None), family="csv")
        step_events = self.ledger.get_events_by_type("trajectory step")
        self.clusters = mine.cluster_failures(step_events)

    def tearDown(self):
        self.ledger.close()

    def test_same_class_and_family_grouped(self):
        by_key = {(c["failure_class"], c["family"]): c
                  for c in self.clusters}
        self.assertIn(("wrong-output", "csv"), by_key)
        self.assertIn(("timeout", "pagination"), by_key)
        csv_cluster = by_key[("wrong-output", "csv")]
        self.assertEqual(csv_cluster["count"], 3)
        self.assertEqual(csv_cluster["task_ids"], ["a", "b"])
        self.assertEqual(len(csv_cluster["event_ids"]), 3)

    def test_success_only_steps_skipped(self):
        task_ids = [t for c in self.clusters for t in c["task_ids"]]
        self.assertNotIn("d", task_ids)

    def test_clusters_sorted_by_count_desc(self):
        counts = [c["count"] for c in self.clusters]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_cluster_repairs(self):
        trajectories.record_repair_outcome(
            self.ledger, "a", "cand-0", "wrong-output", "model-repair",
            True)
        trajectories.record_repair_outcome(
            self.ledger, "b", "cand-0", "wrong-output", "model-repair",
            False)
        clusters = mine.cluster_repairs(
            self.ledger.get_events_by_type("repair outcome"))
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["repair_kind"], "model-repair")
        self.assertEqual(clusters[0]["successes"], 1)
        self.assertEqual(clusters[0]["failures"], 1)


class ProposeCandidateTest(unittest.TestCase):
    def test_candidate_carries_evidence(self):
        cluster = {"failure_class": "syntax", "family": "csv",
                   "events": [{}, {}], "event_ids": [3, 7],
                   "task_ids": ["a", "b"], "count": 2}
        candidate = mine.propose_lesson_candidate(cluster)
        self.assertIn("syntax", candidate["statement"])
        self.assertIn("csv", candidate["statement"])
        self.assertEqual(candidate["evidence"]["event_ids"], [3, 7])
        self.assertEqual(candidate["evidence"]["task_ids"], ["a", "b"])
        self.assertEqual(candidate["evidence"]["count"], 2)
        self.assertTrue(candidate["applies_when"]["when"])
        self.assertTrue(candidate["applies_when"]["when_not"])
        self.assertEqual(candidate["status"], "candidate")

    def test_statement_override_hook(self):
        cluster = {"failure_class": "timeout", "family": "net",
                   "events": [{}], "event_ids": [1],
                   "task_ids": ["t"], "count": 1}
        candidate = mine.propose_lesson_candidate(
            cluster, statement="Custom phrasing.")
        self.assertEqual(candidate["statement"], "Custom phrasing.")


class DedupTest(unittest.TestCase):
    def test_near_identical_merged(self):
        candidates = [
            {"statement": "Inspect field escaping before row splitting.",
             "evidence": {"event_ids": [1], "task_ids": ["a"], "count": 1}},
            {"statement": "inspect field escaping before row splitting!",
             "evidence": {"event_ids": [2], "task_ids": ["b"], "count": 1}},
            {"statement": "Detect repeated cursors in pagination loops.",
             "evidence": {"event_ids": [3], "task_ids": ["c"], "count": 1}},
        ]
        deduped = mine.deduplicate(candidates)
        self.assertEqual(len(deduped), 2)
        merged = [c for c in deduped
                  if "escaping" in c["statement"].lower()][0]
        self.assertEqual(sorted(merged["evidence"]["event_ids"]), [1, 2])
        self.assertEqual(sorted(merged["evidence"]["task_ids"]), ["a", "b"])
        self.assertEqual(merged["evidence"]["count"], 2)
        # Inputs untouched.
        self.assertEqual(candidates[0]["evidence"]["event_ids"], [1])

    def test_empty_and_distinct(self):
        self.assertEqual(mine.deduplicate([]), [])
        distinct = [
            {"statement": "Alpha beta gamma delta.", "evidence": []},
            {"statement": "One two three four five six.", "evidence": []},
        ]
        self.assertEqual(len(mine.deduplicate(distinct)), 2)


def _helpful_fn(task, context_extras):
    if context_extras.get("lessons"):
        return {"first_pass": True, "repairs": 0, "tokens": 600,
                "time_ms": 300.0, "regressions": 0}
    return {"first_pass": task["base_pass"], "repairs": 2,
            "tokens": 1000, "time_ms": 500.0, "regressions": 0}


def _harmful_fn(task, context_extras):
    if context_extras.get("lessons"):
        return {"first_pass": False, "repairs": 3, "tokens": 1500,
                "time_ms": 900.0, "regressions": 1}
    return {"first_pass": True, "repairs": 0, "tokens": 800,
            "time_ms": 400.0, "regressions": 0}


def _neutral_fn(task, context_extras):
    return {"first_pass": True, "repairs": 1, "tokens": 500,
            "time_ms": 250.0, "regressions": 0}


class ReplayDeltasTest(unittest.TestCase):
    def test_helpful_lesson_deltas_and_promote(self):
        tasks = [{"base_pass": True}, {"base_pass": False},
                 {"base_pass": True}, {"base_pass": False}]
        report = replay.ab_replay(_helpful_fn, tasks, lesson_text="L1")
        self.assertEqual(report["n_tasks"], 4)
        self.assertAlmostEqual(report["control"]["first_pass_rate"], 0.5)
        self.assertAlmostEqual(report["lesson"]["first_pass_rate"], 1.0)
        deltas = report["deltas"]
        self.assertAlmostEqual(deltas["first_pass_delta"], 0.5)
        self.assertAlmostEqual(deltas["repair_delta"], -2.0)
        self.assertAlmostEqual(deltas["token_delta"], -400.0)
        self.assertAlmostEqual(deltas["time_delta"], -200.0)
        self.assertEqual(deltas["regression_delta"], 0)
        self.assertEqual(len(report["per_task"]), 4)
        self.assertEqual(report["recommendation"], "promote")

    def test_harmful_lesson_rejected(self):
        report = replay.ab_replay(_harmful_fn, [{"id": 1}, {"id": 2}],
                                  lesson_text="bad lesson")
        self.assertLess(report["deltas"]["first_pass_delta"], 0)
        self.assertGreater(report["deltas"]["regression_delta"], 0)
        self.assertEqual(report["recommendation"], "reject")

    def test_neutral_lesson_rejected(self):
        report = replay.ab_replay(_neutral_fn, [{"id": 1}], lesson_text="L")
        self.assertEqual(report["deltas"]["first_pass_delta"], 0)
        self.assertEqual(report["recommendation"], "reject")

    def test_strict_threshold_blocks_promotion(self):
        tasks = [{"base_pass": True}, {"base_pass": False}]
        report = replay.ab_replay(
            _helpful_fn, tasks, lesson_text="L1",
            thresholds={"min_first_pass_delta": 0.9})
        # Helpful (+0.5) but below the strict bar -> reject.
        self.assertEqual(report["recommendation"], "reject")

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(ValueError):
            replay.ab_replay(_neutral_fn, [], lesson_text="L")
        with self.assertRaises(ValueError):
            replay.ab_replay(_neutral_fn, [{"id": 1}], lesson_text="")
        with self.assertRaises(TypeError):
            replay.ab_replay("not-a-fn", [{"id": 1}], lesson_text="L")


def _fake_cluster():
    return {"failure_class": "edge-case", "family": "csv",
            "count": 4, "task_ids": ["A-1", "A-2"],
            "event_ids": ["e1", "e2", "e3", "e4"],
            "repairs": ["escape fields"]}


class ModelPhrasingTest(unittest.TestCase):
    def test_prompt_carries_cluster_facts(self):
        seen = {}

        def fake(prompt):
            seen["prompt"] = prompt
            return "Escape CSV fields before splitting rows."

        candidate = mine.phrase_with_model(_fake_cluster(), fake)
        self.assertIn("edge-case", seen["prompt"])
        self.assertIn("csv", seen["prompt"])
        self.assertIn("4", seen["prompt"])
        self.assertEqual(candidate["statement"],
                         "Escape CSV fields before splitting rows.")
        self.assertEqual(candidate["evidence"]["task_ids"], ["A-1", "A-2"])
        self.assertEqual(candidate["evidence"]["count"], 4)

    def test_empty_model_output_rejected(self):
        with self.assertRaises(ValueError):
            mine.phrase_with_model(_fake_cluster(),
                                   lambda prompt: "  ")
        with self.assertRaises(ValueError):
            mine.phrase_with_model(_fake_cluster(),
                                   lambda prompt: None)

    def test_non_callable_rejected(self):
        with self.assertRaises(TypeError):
            mine.phrase_with_model(_fake_cluster(), "not-a-fn")

    def test_whitespace_normalized(self):
        candidate = mine.phrase_with_model(
            _fake_cluster(), lambda prompt: "  Check\n  edges.  ")
        self.assertEqual(candidate["statement"], "Check edges.")


if __name__ == "__main__":
    unittest.main()
