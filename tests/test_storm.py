"""Tests for mutation-storm controls (storm.py). Stdlib only."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storm


class ConsecutiveFreezeTest(unittest.TestCase):
    def test_freeze_at_threshold_not_before(self):
        tracker = storm.StormTracker(max_consecutive_failures=3)
        for attempt in range(1, 3):
            status = tracker.record_failure("cap-a", "task-1",
                                            "sig-%d" % attempt)
            self.assertEqual(status["consecutive"], attempt)
            self.assertIsNone(status["frozen"])
            self.assertFalse(tracker.is_frozen("cap-a"))
        status = tracker.record_failure("cap-a", "task-1", "sig-3")
        self.assertEqual(status["consecutive"], 3)
        self.assertIsNotNone(status["frozen"])
        self.assertEqual(status["frozen"]["reason"], "consecutive_failures")
        self.assertTrue(tracker.is_frozen("cap-a"))

    def test_success_resets_counter(self):
        tracker = storm.StormTracker(max_consecutive_failures=2)
        tracker.record_failure("cap-a", "task-1", "sig-1")
        tracker.record_success("cap-a")
        status = tracker.record_failure("cap-a", "task-1", "sig-2")
        self.assertEqual(status["consecutive"], 1)
        self.assertFalse(tracker.is_frozen("cap-a"))

    def test_freeze_is_sticky_and_check_raises(self):
        tracker = storm.StormTracker(max_consecutive_failures=1)
        tracker.record_failure("cap-a", "task-1", "sig-1")
        tracker.record_success("cap-a")
        self.assertTrue(tracker.is_frozen("cap-a"))
        with self.assertRaises(storm.TargetFrozenError) as ctx:
            tracker.check("cap-a")
        self.assertEqual(ctx.exception.target, "cap-a")
        self.assertIsNone(tracker.check("cap-other"))

    def test_unfreeze_needs_reason_and_clears(self):
        tracker = storm.StormTracker(max_consecutive_failures=1)
        tracker.record_failure("cap-a", "task-1", "sig-1")
        with self.assertRaises(ValueError):
            tracker.unfreeze("cap-a", "")
        with self.assertRaises(KeyError):
            tracker.unfreeze("cap-other", "reason")
        record = tracker.unfreeze("cap-a", "diagnosed: stale fixture")
        self.assertEqual(record["reason"], "unfrozen")
        self.assertFalse(tracker.is_frozen("cap-a"))
        status = tracker.record_failure("cap-a", "task-2", "sig-9")
        self.assertEqual(status["consecutive"], 1)

    def test_targets_are_independent(self):
        tracker = storm.StormTracker(max_consecutive_failures=2)
        tracker.record_failure("cap-a", "task-1", "sig-1")
        tracker.record_failure("cap-a", "task-1", "sig-1")
        self.assertTrue(tracker.is_frozen("cap-a"))
        self.assertFalse(tracker.is_frozen("cap-b"))


class CrossTaskCycleTest(unittest.TestCase):
    def test_cycle_across_distinct_tasks(self):
        tracker = storm.StormTracker(cross_task_threshold=3)
        first = tracker.record_failure("cap-a", "task-1", "same-sig")
        second = tracker.record_failure("cap-b", "task-2", "same-sig")
        self.assertIsNone(first["cycle"])
        self.assertIsNone(second["cycle"])
        third = tracker.record_failure("cap-c", "task-3", "same-sig")
        self.assertIsNotNone(third["cycle"])
        self.assertEqual(third["cycle"]["reason"], "cross_task_cycle")
        self.assertEqual(third["cycle"]["evidence"]["task_count"], 3)
        # The reporting target freezes too.
        self.assertTrue(tracker.is_frozen("cap-c"))

    def test_same_task_repeats_are_not_a_cycle(self):
        tracker = storm.StormTracker(cross_task_threshold=2,
                                     max_consecutive_failures=99)
        for _ in range(5):
            status = tracker.record_failure("cap-a", "only-task", "same-sig")
        self.assertIsNone(status["cycle"])

    def test_distinct_signatures_are_not_a_cycle(self):
        tracker = storm.StormTracker(cross_task_threshold=2,
                                     max_consecutive_failures=99)
        tracker.record_failure("cap-a", "task-1", "sig-1")
        status = tracker.record_failure("cap-b", "task-2", "sig-2")
        self.assertIsNone(status["cycle"])

    def test_cycle_escalates_once_per_signature(self):
        tracker = storm.StormTracker(cross_task_threshold=2,
                                     max_consecutive_failures=99)
        tracker.record_failure("cap-a", "task-1", "same-sig")
        tracker.record_failure("cap-b", "task-2", "same-sig")
        later = tracker.record_failure("cap-d", "task-4", "same-sig")
        self.assertIsNone(later["cycle"])
        cycles = [r for r in tracker.escalations
                  if r["reason"] == "cross_task_cycle"]
        self.assertEqual(len(cycles), 1)

    def test_repair_summary_dicts_canonicalize(self):
        tracker = storm.StormTracker(cross_task_threshold=2,
                                     max_consecutive_failures=99)
        summary = {"property": "p", "expected": 1, "actual": 2,
                   "callsite": "ignored", "counterexample": "ignored"}
        tracker.record_failure("cap-a", "task-1", dict(summary))
        status = tracker.record_failure("cap-b", "task-2", dict(summary))
        self.assertIsNotNone(status["cycle"])
        # Same canonical form as repair.failure_signature ("%r|%r|%r").
        self.assertEqual(status["cycle"]["evidence"]["signature"],
                         "'p'|1|2")


class EscalationRecordTest(unittest.TestCase):
    def test_record_shape_and_sink_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = os.path.join(tmp, "escalations.jsonl")
            tracker = storm.StormTracker(sink=sink)
            record = tracker.escalate("cap-a", "consecutive_failures",
                                      {"consecutive": 3})
            self.assertEqual(
                set(record),
                {"id", "timestamp", "target", "reason", "evidence",
                 "frozen"})
            self.assertTrue(record["frozen"])
            self.assertEqual(len(tracker.escalations), 1)
            back = storm.load_escalations(sink)
            self.assertEqual(back, tracker.escalations)

    def test_bad_inputs_rejected(self):
        tracker = storm.StormTracker()
        with self.assertRaises(ValueError):
            tracker.record_failure("", "task-1", "sig")
        with self.assertRaises(ValueError):
            tracker.record_failure("cap-a", "", "sig")
        with self.assertRaises(ValueError):
            tracker.record_failure("cap-a", "task-1", "")
        with self.assertRaises(TypeError):
            tracker.record_failure("cap-a", "task-1", 42)
        with self.assertRaises(ValueError):
            tracker.escalate("", "reason")
        with self.assertRaises(ValueError):
            tracker.escalate("cap-a", "")
        with self.assertRaises(ValueError):
            storm.StormTracker(max_consecutive_failures=0)
        with self.assertRaises(ValueError):
            storm.StormTracker(cross_task_threshold=1)


if __name__ == "__main__":
    unittest.main()
