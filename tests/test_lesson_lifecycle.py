"""Tests for lesson lifecycle: decay, counterexamples, effect (L2/L4)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lessons
import replay


def _lesson(**overrides):
    base = {
        "id": "lifecycle-1",
        "class": "design",
        "statement": "Prefer composition of existing capabilities.",
        "applies_when": {"when": "task decomposes into covered steps",
                         "when_not": "latency-critical hot paths"},
        "confidence": 0.8,
    }
    base.update(overrides)
    return lessons.validate_lesson(base)


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


class DecayTest(unittest.TestCase):
    def test_no_decay_when_versions_match(self):
        lesson = _lesson()
        lessons.decay_confidence(lesson, runtime_version="sbcl-2.5",
                                 model_version="qwen-1")
        before = lesson["confidence"]
        self.assertFalse(lesson["needs_revalidation"])
        lessons.decay_confidence(lesson, runtime_version="sbcl-2.5",
                                 model_version="qwen-1")
        self.assertEqual(lesson["confidence"], before)
        self.assertFalse(lesson["needs_revalidation"])

    def test_decay_on_runtime_change(self):
        lesson = _lesson()
        lessons.decay_confidence(lesson, runtime_version="sbcl-2.5",
                                 model_version="qwen-1")
        before = lesson["confidence"]
        lessons.decay_confidence(lesson, runtime_version="sbcl-2.6",
                                 model_version="qwen-1")
        self.assertAlmostEqual(
            lesson["confidence"], round(before * lessons.DECAY_FACTOR, 4))
        self.assertTrue(lesson["needs_revalidation"])
        self.assertEqual(lesson["runtime_version"], "sbcl-2.6")

    def test_decay_on_model_change(self):
        lesson = _lesson()
        lessons.decay_confidence(lesson, runtime_version="sbcl-2.5",
                                 model_version="qwen-1")
        before = lesson["confidence"]
        lessons.decay_confidence(lesson, runtime_version="sbcl-2.5",
                                 model_version="qwen-2")
        self.assertLess(lesson["confidence"], before)
        self.assertTrue(lesson["needs_revalidation"])

    def test_no_double_decay_without_further_change(self):
        lesson = _lesson()
        lessons.decay_confidence(lesson, runtime_version="a",
                                 model_version="b")
        lessons.decay_confidence(lesson, runtime_version="a2",
                                 model_version="b")
        decayed = lesson["confidence"]
        lessons.decay_confidence(lesson, runtime_version="a2",
                                 model_version="b")
        self.assertEqual(lesson["confidence"], decayed)
        self.assertTrue(lesson["needs_revalidation"])

    def test_bad_versions_rejected(self):
        with self.assertRaises(ValueError):
            lessons.decay_confidence(_lesson(), runtime_version="",
                                     model_version="m")
        with self.assertRaises(ValueError):
            lessons.decay_confidence(_lesson(), runtime_version="r",
                                     model_version="")


class CounterexampleTest(unittest.TestCase):
    def test_accumulate_then_refine_then_deprecate(self):
        store = lessons.LessonStore(lessons=[_lesson()])
        lesson_id = "lifecycle-1"
        orig_when = store.get(lesson_id)["applies_when"]["when"]
        orig_not = store.get(lesson_id)["applies_when"]["when_not"]
        deprecate_at = lessons.COUNTEREXAMPLE_DEPRECATE_THRESHOLD
        for i in range(deprecate_at):
            store.record_counterexample(lesson_id, "task-%d" % i,
                                        "failure mode %d" % i)
            lesson = store.get(lesson_id)
            self.assertEqual(len(lesson["counterexamples"]), i + 1)
            lessons.refine_or_deprecate(lesson)
            if i + 1 < deprecate_at:
                self.assertNotEqual(lesson["status"], "deprecated")
                if i + 1 >= lessons.COUNTEREXAMPLE_REFINE_THRESHOLD:
                    narrowed = lesson["applies_when"]
                    self.assertTrue(
                        len(narrowed["when"]) > len(orig_when)
                        or len(narrowed["when_not"]) > len(orig_not))
            else:
                self.assertEqual(lesson["status"], "deprecated")

    def test_module_record_on_dict(self):
        lesson = _lesson()
        items = lessons.record_counterexample(lesson, "t-1", "hot path lag")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["task_id"], "t-1")
        lessons.refine_or_deprecate(lesson)
        self.assertIn("hot path lag", lesson["applies_when"]["when_not"])

    def test_module_record_with_id_returns_record(self):
        record = lessons.record_counterexample("lid", "t", "note")
        self.assertEqual(record["lesson_id"], "lid")

    def test_refine_idempotent(self):
        lesson = _lesson()
        lessons.record_counterexample(lesson, "t", "note")
        lessons.refine_or_deprecate(lesson)
        snapshot = dict(lesson["applies_when"])
        lessons.refine_or_deprecate(lesson)
        self.assertEqual(lesson["applies_when"], snapshot)

    def test_invalid_counterexamples_rejected(self):
        store = lessons.LessonStore(lessons=[_lesson()])
        with self.assertRaises(KeyError):
            store.record_counterexample("missing", "t", "n")
        with self.assertRaises(ValueError):
            store.record_counterexample("lifecycle-1", "", "n")
        with self.assertRaises(ValueError):
            store.record_counterexample("lifecycle-1", "t", "")

    def test_lifecycle_fields_persist_via_tempfile(self):
        lesson = _lesson()
        lessons.decay_confidence(lesson, runtime_version="r1",
                                 model_version="m1")
        lessons.record_counterexample(lesson, "t", "n")
        store = lessons.LessonStore(lessons=[lesson])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lifecycle.json")
            store.save(path)
            reopened = lessons.LessonStore.load(path)
        kept = reopened.get("lifecycle-1")
        self.assertEqual(kept["runtime_version"], "r1")
        self.assertEqual(len(kept["counterexamples"]), 1)


class MeasureEffectTest(unittest.TestCase):
    def test_helpful_promotes_end_to_end(self):
        tasks = [{"base_pass": True}, {"base_pass": False}]
        report = replay.ab_replay(_helpful_fn, tasks, lesson_text="help")
        effect = replay.measure_lesson_effect("help", report)
        self.assertEqual(effect["recommendation"], "promote")
        self.assertGreater(effect["impact"]["first_pass_success"], 0)
        self.assertLess(effect["impact"]["tokens"], 0)
        self.assertEqual(effect["lesson_text"], "help")

    def test_harmful_rejects_end_to_end(self):
        report = replay.ab_replay(_harmful_fn, [{"id": 1}, {"id": 2}],
                                  lesson_text="harm")
        effect = replay.measure_lesson_effect("harm", report)
        self.assertEqual(effect["recommendation"], "reject")
        self.assertTrue(
            effect["impact"]["first_pass_success"] < 0
            or effect["impact"]["regressions"] > 0)

    def test_neutral_holds(self):
        report = replay.ab_replay(_neutral_fn, [{"id": 1}],
                                  lesson_text="neutral")
        self.assertEqual(report["recommendation"], "reject")
        effect = replay.measure_lesson_effect("neutral", report)
        self.assertEqual(effect["recommendation"], "hold")

    def test_invalid_inputs_rejected(self):
        report = replay.ab_replay(_neutral_fn, [{"id": 1}],
                                  lesson_text="L")
        with self.assertRaises(ValueError):
            replay.measure_lesson_effect("", report)
        with self.assertRaises((TypeError, ValueError)):
            replay.measure_lesson_effect("L", {})
        with self.assertRaises((TypeError, ValueError)):
            replay.measure_lesson_effect("L", "nope")


if __name__ == "__main__":
    unittest.main()
