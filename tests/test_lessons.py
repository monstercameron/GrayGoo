"""Tests for trajectory capture (L1) and manual lessons (L2). Stdlib only."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import events
import lessons
import trajectories


def _sample_steps():
    return [
        {"candidate": "cand-1",
         "failure": {"class": "syntax", "detail": "unbalanced paren"},
         "repair": None, "success": False},
        {"candidate": "cand-2",
         "failure": {"class": "wrong-output", "detail": "off by one"},
         "repair": {"kind": "model-repair", "detail": "fix predicate"},
         "success": False},
        {"candidate": "cand-3", "failure": None,
         "repair": {"kind": "model-repair"}, "success": True},
    ]


class TrajectoryRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.ledger = events.EventLedger(":memory:")

    def tearDown(self):
        self.ledger.close()

    def test_record_and_query_round_trip(self):
        summary = trajectories.record_trajectory(
            self.ledger, "task-1", _sample_steps(), family="csv",
            goal="fix parser")
        self.assertEqual(summary["task_id"], "task-1")
        self.assertEqual(len(summary["event_ids"]), 4)  # 3 steps + summary
        self.assertTrue(self.ledger.verify_chain())

        found = trajectories.query_trajectories(self.ledger, task_id="task-1")
        self.assertEqual(len(found), 1)
        traj = found[0]
        self.assertEqual(traj["family"], "csv")
        self.assertEqual(traj["goal"], "fix parser")
        self.assertEqual(len(traj["steps"]), 3)
        self.assertEqual(traj["steps"][0]["failure"]["class"], "syntax")
        self.assertEqual(traj["steps"][1]["repair"]["kind"], "model-repair")
        self.assertTrue(traj["steps"][2]["success"])
        self.assertIsNone(traj["steps"][2]["failure"])

    def test_query_filters_by_family(self):
        trajectories.record_trajectory(self.ledger, "a", _sample_steps(),
                                       family="csv")
        trajectories.record_trajectory(self.ledger, "b", _sample_steps(),
                                       family="pagination")
        self.assertEqual(len(trajectories.query_trajectories(self.ledger)), 2)
        csv_only = trajectories.query_trajectories(self.ledger, family="csv")
        self.assertEqual([t["task_id"] for t in csv_only], ["a"])
        self.assertEqual(
            trajectories.query_trajectories(self.ledger, task_id="missing"),
            [])

    def test_repair_and_strategy_outcomes(self):
        trajectories.record_trajectory(self.ledger, "t", _sample_steps())
        trajectories.record_repair_outcome(
            self.ledger, "t", "cand-2", "wrong-output", "model-repair",
            True, detail="predicate fix")
        trajectories.record_strategy_outcome(
            self.ledger, "t", "differential-first", True)
        trajectories.record_strategy_outcome(
            self.ledger, "t", "synthesize-monolith", False)
        repairs = trajectories.list_repair_outcomes(self.ledger, task_id="t")
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["failure_class"], "wrong-output")
        self.assertTrue(repairs[0]["success"])
        strategies = trajectories.list_strategy_outcomes(self.ledger)
        self.assertEqual(
            {s["strategy"]: s["success"] for s in strategies},
            {"differential-first": True, "synthesize-monolith": False})


class TaxonomyValidityTest(unittest.TestCase):
    def setUp(self):
        self.ledger = events.EventLedger(":memory:")

    def tearDown(self):
        self.ledger.close()

    def test_sixteen_canonical_classes(self):
        self.assertEqual(len(trajectories.FAILURE_CLASSES), 16)
        self.assertEqual(len(set(trajectories.FAILURE_CLASSES)), 16)
        for expected in ("syntax", "compile", "type/contract",
                         "wrong-output", "edge-case", "state-corruption",
                         "effect-violation", "performance", "timeout",
                         "memory", "stale-generation", "over-refactor",
                         "negative-transfer", "test-overfit", "tool-misuse",
                         "context-missing"):
            self.assertIn(expected, trajectories.FAILURE_CLASS_SET)

    def test_normalize_accepts_aliases(self):
        self.assertEqual(
            trajectories.normalize_failure_class("contract"),
            "type/contract")
        self.assertEqual(
            trajectories.normalize_failure_class("wrong_output"),
            "wrong-output")
        self.assertEqual(
            trajectories.normalize_failure_class("Timeout"), "timeout")

    def test_unknown_class_rejected(self):
        with self.assertRaises(ValueError):
            trajectories.normalize_failure_class("vibes")
        with self.assertRaises(ValueError):
            trajectories.record_trajectory(
                self.ledger, "t",
                [{"candidate": "c", "failure": "not-a-class",
                  "repair": None, "success": False}])
        with self.assertRaises(ValueError):
            trajectories.record_repair_outcome(
                self.ledger, "t", "c", "bogus", "model-repair", True)


class LessonSchemaTest(unittest.TestCase):
    def _valid(self):
        return {
            "id": "t1", "class": "failure",
            "statement": "Check X before Y.",
            "applies_when": {"when": "doing X", "when_not": "doing Z"},
            "evidence": [], "counterexamples": [], "confidence": 0.7,
            "impact": {}, "created_generation": 1, "last_validated": 2,
            "usage": {}, "status": "candidate",
        }

    def test_valid_lesson_passes_with_defaults(self):
        lesson = lessons.validate_lesson(self._valid())
        self.assertEqual(lesson["usage"],
                         {"retrievals": 0, "successes": 0, "failures": 0})
        self.assertEqual(lesson["kind"], "heuristic")
        self.assertEqual(lesson["tags"], [])

    def test_bad_class_rejected(self):
        bad = self._valid()
        bad["class"] = "vibes"
        with self.assertRaises(ValueError):
            lessons.validate_lesson(bad)

    def test_bad_confidence_rejected(self):
        for confidence in (-0.1, 1.5, "high", None, True):
            bad = self._valid()
            bad["confidence"] = confidence
            with self.assertRaises(ValueError, msg=repr(confidence)):
                lessons.validate_lesson(bad)

    def test_missing_applicability_rejected(self):
        for applies in (None, {}, {"when": "x"}, {"when_not": "y"},
                        {"when": "", "when_not": "y"}):
            bad = self._valid()
            bad["applies_when"] = applies
            with self.assertRaises(ValueError, msg=repr(applies)):
                lessons.validate_lesson(bad)

    def test_bad_status_and_kind_rejected(self):
        bad = self._valid()
        bad["status"] = "eternal"
        with self.assertRaises(ValueError):
            lessons.validate_lesson(bad)
        bad = self._valid()
        bad["kind"] = "rumor"
        with self.assertRaises(ValueError):
            lessons.validate_lesson(bad)


class SeedLessonsTest(unittest.TestCase):
    def test_seed_count_and_validity(self):
        seeds = lessons.seed_10_20()
        self.assertGreaterEqual(len(seeds), 10)
        ids = set()
        for seed in seeds:
            validated = lessons.validate_lesson(seed)
            self.assertEqual(validated, seed)
            ids.add(seed["id"])
        self.assertEqual(len(ids), len(seeds))  # unique ids

    def test_seeds_cover_all_five_classes(self):
        classes = {s["class"] for s in lessons.seed_10_20()}
        self.assertEqual(classes, set(lessons.LESSON_CLASSES))

    def test_seeds_have_narrow_applicability(self):
        for seed in lessons.seed_10_20():
            when = seed["applies_when"]["when"]
            when_not = seed["applies_when"]["when_not"]
            self.assertTrue(when.strip())
            self.assertTrue(when_not.strip())
            self.assertNotEqual(when, when_not)

    def test_seeds_separate_facts_and_heuristics(self):
        kinds = {s["kind"] for s in lessons.seed_10_20()}
        self.assertIn("fact", kinds)
        self.assertIn("heuristic", kinds)


class LessonStoreTest(unittest.TestCase):
    def test_json_persistence_round_trip(self):
        store = lessons.LessonStore(lessons=lessons.seed_10_20())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lessons.json")
            store.save(path)
            reopened = lessons.LessonStore.load(path)
        self.assertEqual(len(reopened), len(store))
        original = store.get("cursor-cycle-detection")
        self.assertEqual(reopened.get("cursor-cycle-detection"), original)

    def test_usage_and_counterexamples(self):
        store = lessons.LessonStore(lessons=lessons.seed_10_20()[:2])
        store.record_usage("csv-field-escaping-first", success=True)
        store.record_usage("csv-field-escaping-first", success=False)
        usage = store.get("csv-field-escaping-first")["usage"]
        self.assertEqual(usage["retrievals"], 2)
        self.assertEqual(usage["successes"], 1)
        self.assertEqual(usage["failures"], 1)
        store.add_counterexample("csv-field-escaping-first",
                                 {"task_id": "x", "note": "escaped pipe"})
        self.assertEqual(
            len(store.get("csv-field-escaping-first")["counterexamples"]), 1)
        with self.assertRaises(KeyError):
            store.record_usage("missing", success=True)

    def test_decay_confidence(self):
        store = lessons.LessonStore(lessons=lessons.seed_10_20()[:2])
        before = [l["confidence"] for l in store]
        store.decay_confidence(0.5)
        after = [l["confidence"] for l in store]
        for old, new in zip(before, after):
            self.assertAlmostEqual(new, round(old * 0.5, 4))
        with self.assertRaises(ValueError):
            store.decay_confidence(1.5)


class RetrievalTest(unittest.TestCase):
    def test_failure_class_match_ranks_first(self):
        registry = lessons.LessonRegistry(lessons=[
            lessons.make_lesson(
                "tag-match", "design", "Tag overlapping lesson.",
                when="tagged work", when_not="other work",
                confidence=0.9, status="active", tags=["csv", "parsing"],
                failure_classes=["timeout"]),
            lessons.make_lesson(
                "class-match", "failure", "Failure class lesson.",
                when="syntax work", when_not="other work",
                confidence=0.5, status="active", tags=["unrelated"],
                failure_classes=["syntax"]),
        ])
        ranked = registry.retrieve_for_task(["csv", "parsing"],
                                            failure_class="syntax", k=5)
        self.assertEqual([l["id"] for l in ranked],
                         ["class-match", "tag-match"])

    def test_only_active_and_k_respected(self):
        seed_lessons = lessons.seed_10_20()
        seed_lessons[0]["status"] = "candidate"
        registry = lessons.LessonRegistry(lessons=seed_lessons)
        ranked = registry.retrieve_for_task(["csv"], k=5)
        self.assertLessEqual(len(ranked), 5)
        self.assertTrue(all(l["status"] == "active" for l in ranked))
        self.assertNotIn("csv-field-escaping-first",
                         [l["id"] for l in ranked])
        self.assertEqual(registry.retrieve_for_task(["csv"], k=0), [])
        # Hard cap holds even for large k.
        self.assertLessEqual(
            len(registry.retrieve_for_task([], k=50)),
            lessons.MAX_LESSONS_PER_CALL)


class RegistryOutcomeTest(unittest.TestCase):
    """Registry usage/counterexample recording + precision (issues #89/#97)."""

    def _registry(self):
        return lessons.LessonRegistry(
            lessons=lessons.seed_10_20()[:2])

    def test_registry_record_usage_mirrors_store(self):
        registry = self._registry()
        registry.record_usage("csv-field-escaping-first", success=True)
        registry.record_usage("csv-field-escaping-first", success=False)
        usage = registry.get("csv-field-escaping-first")["usage"]
        self.assertEqual(
            usage, {"retrievals": 2, "successes": 1, "failures": 1})
        with self.assertRaises(KeyError):
            registry.record_usage("missing", success=True)

    def test_registry_record_counterexample(self):
        registry = self._registry()
        out = registry.record_counterexample(
            "csv-field-escaping-first", "A-TRN-01", "injected, still failed")
        self.assertEqual(out, [{"task_id": "A-TRN-01",
                                "note": "injected, still failed"}])
        with self.assertRaises(KeyError):
            registry.record_counterexample("missing", "t", "n")

    def test_precision_aggregates_usage(self):
        registry = self._registry()
        self.assertIsNone(lessons.precision(registry)["precision"])
        registry.record_usage("csv-field-escaping-first", success=True)
        registry.record_usage("csv-field-escaping-first", success=False)
        got = lessons.precision(registry)
        self.assertEqual(got["retrievals"], 2)
        self.assertEqual(got["successes"], 1)
        self.assertEqual(got["failures"], 1)
        self.assertEqual(got["precision"], 0.5)
        self.assertEqual(
            got["per_lesson"]["csv-field-escaping-first"]["precision"], 0.5)

    def test_precision_accepts_lesson_dicts(self):
        got = lessons.precision([
            {"id": "a", "usage": {"retrievals": 4, "successes": 3,
                                 "failures": 1}},
            {"id": "b"},  # no usage: never retrieved, still listed
        ])
        self.assertEqual(got["precision"], 0.75)
        self.assertIsNone(got["per_lesson"]["b"]["precision"])


if __name__ == "__main__":
    unittest.main()
