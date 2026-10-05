"""Tests for pipeline event wiring (instrument.py). Stdlib only."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import events
import instrument


def _result_dict(**overrides):
    result = {
        "text": "(candidate (:target f) (:parent 1))",
        "finish_reason": "stop",
        "model": "qwen-3.8-27b",
        "latency_ms": 812.5,
        "input_tokens": 120,
        "output_tokens": 45,
        "request_id": "req-abc-123",
        "cost_usd": 0.000186,
        "task_id": "task-1",
        "generation": 2,
        "candidate_id": "cand-1",
    }
    result.update(overrides)
    return result


class InstrumentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "ledger.db")
        self.ledger = events.EventLedger(self.db_path)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def _event_count(self):
        return len(self.ledger.get_recent_events(limit=10000))

    # -- log_model_call ------------------------------------------------
    def test_log_model_call_appends_one_event_and_call_row(self):
        stored = instrument.log_model_call(self.ledger, _result_dict())
        self.assertEqual(self._event_count(), 1)
        self.assertEqual(stored["event_type"], "model called")
        self.assertEqual(stored["task_id"], "task-1")
        self.assertEqual(stored["generation"], 2)
        self.assertEqual(stored["candidate_id"], "cand-1")
        payload = json.loads(stored["payload"])
        self.assertEqual(payload["request_id"], "req-abc-123")
        self.assertEqual(payload["model"], "qwen-3.8-27b")
        self.assertEqual(payload["latency_ms"], 812.5)
        self.assertEqual(payload["input_tokens"], 120)
        self.assertEqual(payload["output_tokens"], 45)
        self.assertEqual(payload["cost"], 0.000186)
        calls = self.ledger.list_model_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["provider"], "cerebras")
        self.assertEqual(calls[0]["model"], "qwen-3.8-27b")
        self.assertEqual(payload["call_id"], calls[0]["call_id"])
        self.assertTrue(self.ledger.verify_chain())

    def test_log_model_call_missing_model_names_key(self):
        result = _result_dict()
        del result["model"]
        with self.assertRaises(ValueError) as ctx:
            instrument.log_model_call(self.ledger, result)
        self.assertIn("model", str(ctx.exception))
        self.assertEqual(self._event_count(), 0)

    def test_log_model_call_non_dict_rejected(self):
        with self.assertRaises(TypeError):
            instrument.log_model_call(self.ledger, ["not", "a", "dict"])
        self.assertEqual(self._event_count(), 0)

    # -- log_candidate --------------------------------------------------
    def test_log_candidate_appends_one_event(self):
        stored = instrument.log_candidate(
            self.ledger,
            {"candidate_id": "cand-9", "target": "parse-csv",
             "generation": 4},
            "task-7",
        )
        self.assertEqual(self._event_count(), 1)
        self.assertEqual(stored["event_type"], "candidate generated")
        self.assertEqual(stored["task_id"], "task-7")
        self.assertEqual(stored["candidate_id"], "cand-9")
        self.assertEqual(stored["generation"], 4)
        self.assertIn("parse-csv", stored["payload"])
        self.assertTrue(self.ledger.verify_chain())

    def test_log_candidate_missing_key_names_key(self):
        with self.assertRaises(ValueError) as ctx:
            instrument.log_candidate(self.ledger, {"target": "f"}, "task-7")
        self.assertIn("candidate_id", str(ctx.exception))
        self.assertEqual(self._event_count(), 0)

    # -- log_compile_test ----------------------------------------------
    def test_log_compile_test_pass_and_fail(self):
        passed = instrument.log_compile_test(
            self.ledger,
            {"passed": True, "task_id": "t", "tests": 12},
            "cand-1",
        )
        self.assertEqual(passed["event_type"], "candidate passed")
        failed = instrument.log_compile_test(
            self.ledger,
            {"passed": False, "task_id": "t", "error": "compile"},
            "cand-2",
        )
        self.assertEqual(failed["event_type"], "candidate failed")
        self.assertEqual(failed["candidate_id"], "cand-2")
        self.assertIn("compile", failed["payload"])
        self.assertEqual(self._event_count(), 2)
        self.assertTrue(self.ledger.verify_chain())

    def test_log_compile_test_missing_key_names_key(self):
        with self.assertRaises(ValueError) as ctx:
            instrument.log_compile_test(self.ledger, {"task_id": "t"},
                                        "cand-1")
        self.assertIn("passed", str(ctx.exception))
        self.assertEqual(self._event_count(), 0)

    # -- log_promotion --------------------------------------------------
    def test_log_promotion_and_rejection(self):
        promoted = instrument.log_promotion(
            self.ledger,
            {"candidate_id": "cand-1", "approved": True,
             "capability_id": "parse-csv", "capability_version": 3,
             "task_id": "t"},
        )
        self.assertEqual(promoted["event_type"], "candidate promoted")
        self.assertEqual(promoted["capability_id"], "parse-csv")
        self.assertEqual(promoted["capability_version"], 3)
        rejected = instrument.log_promotion(
            self.ledger,
            {"candidate_id": "cand-2", "approved": False,
             "reason": "regression", "task_id": "t"},
        )
        self.assertEqual(rejected["event_type"], "candidate rejected")
        self.assertIn("regression", rejected["payload"])
        self.assertEqual(self._event_count(), 2)
        self.assertTrue(self.ledger.verify_chain())

    def test_log_promotion_missing_keys_name_key(self):
        for missing in ("candidate_id", "approved"):
            decision = {"candidate_id": "c", "approved": True}
            del decision[missing]
            with self.assertRaises(ValueError) as ctx:
                instrument.log_promotion(self.ledger, decision)
            self.assertIn(missing, str(ctx.exception))
        self.assertEqual(self._event_count(), 0)

    # -- log_invocation --------------------------------------------------
    def test_log_invocation_ok_and_failure(self):
        ok = instrument.log_invocation(self.ledger, "parse-csv", 3, True,
                                       12.5)
        self.assertEqual(ok["event_type"], "capability invoked")
        self.assertEqual(ok["capability_id"], "parse-csv")
        self.assertEqual(ok["capability_version"], 3)
        failed = instrument.log_invocation(self.ledger, "parse-csv", 3,
                                           False, 9000.0)
        self.assertEqual(failed["event_type"], "capability failed")
        payload = json.loads(failed["payload"])
        self.assertEqual(payload, {"ok": False, "latency_ms": 9000.0})
        self.assertEqual(self._event_count(), 2)
        self.assertTrue(self.ledger.verify_chain())

    def test_log_invocation_requires_capability_id(self):
        with self.assertRaises(ValueError) as ctx:
            instrument.log_invocation(self.ledger, "", 3, True, 1.0)
        self.assertIn("capability_id", str(ctx.exception))
        self.assertEqual(self._event_count(), 0)

    # -- log_rollback ----------------------------------------------------
    def test_log_rollback_appends_one_event(self):
        stored = instrument.log_rollback(self.ledger, "parse-csv", 4, 3,
                                         "regression in prod")
        self.assertEqual(self._event_count(), 1)
        self.assertEqual(stored["event_type"], "candidate rolled back")
        self.assertEqual(stored["capability_id"], "parse-csv")
        self.assertEqual(stored["capability_version"], 3)
        payload = json.loads(stored["payload"])
        self.assertEqual(payload["from_version"], 4)
        self.assertEqual(payload["to_version"], 3)
        self.assertIn("regression", payload["reason"])
        self.assertTrue(self.ledger.verify_chain())

    # -- log_task_outcome ------------------------------------------------
    def test_log_task_outcome_appends_one_event(self):
        stored = instrument.log_task_outcome(self.ledger, "task-1", True,
                                             "solved via parse-csv v3")
        self.assertEqual(self._event_count(), 1)
        self.assertEqual(stored["event_type"], "task completed")
        self.assertEqual(stored["task_id"], "task-1")
        payload = json.loads(stored["payload"])
        self.assertEqual(payload,
                         {"success": True,
                          "summary": "solved via parse-csv v3"})
        self.assertTrue(self.ledger.verify_chain())

    # -- mixed pipeline sequence ----------------------------------------
    def test_mixed_sequence_chain_verifies(self):
        instrument.log_model_call(self.ledger, _result_dict(task_id="t"))
        instrument.log_candidate(self.ledger, {"candidate_id": "c1"}, "t")
        instrument.log_compile_test(
            self.ledger, {"passed": True, "task_id": "t"}, "c1")
        instrument.log_promotion(
            self.ledger, {"candidate_id": "c1", "approved": True,
                          "capability_id": "cap", "capability_version": 1,
                          "task_id": "t"})
        instrument.log_invocation(self.ledger, "cap", 1, True, 3.0)
        instrument.log_rollback(self.ledger, "cap", 2, 1, "bad deploy")
        instrument.log_task_outcome(self.ledger, "t", True, "done")
        self.assertEqual(self._event_count(), 7)
        self.assertTrue(self.ledger.verify_chain())

    # -- append-only: no update/delete API -------------------------------
    def test_ledger_has_no_update_or_delete_api(self):
        forbidden = (
            "update_event", "update", "delete_event", "delete",
            "remove_event", "remove", "edit_event", "edit",
            "modify_event", "set_event", "rewrite_event",
            "update_model_call", "delete_model_call",
        )
        for name in forbidden:
            self.assertFalse(hasattr(self.ledger, name),
                             "ledger must not expose %r" % name)
            self.assertFalse(hasattr(events.EventLedger, name),
                             "EventLedger must not expose %r" % name)

    # -- world-model projection ------------------------------------------
    def test_recent_failures_projection(self):
        instrument.log_compile_test(
            self.ledger, {"passed": True, "task_id": "t"}, "c0")
        instrument.log_compile_test(
            self.ledger, {"passed": False, "task_id": "t",
                          "error": "compile"}, "c1")
        # Unlinked noise: must not leak into the task projection.
        instrument.log_invocation(self.ledger, "cap", 1, False, 5.0)
        instrument.log_compile_test(
            self.ledger, {"passed": False, "task_id": "t",
                          "error": "timeout"}, "c2")
        instrument.log_task_outcome(self.ledger, "t", True, "ok")
        instrument.log_task_outcome(self.ledger, "t", False, "gave up")
        failures = instrument.recent_failures(self.ledger, "t")
        self.assertEqual(len(failures), 3)
        # Most recent first.
        self.assertEqual(failures[0]["event_type"], "task completed")
        self.assertEqual(failures[1]["candidate_id"], "c2")
        self.assertEqual(failures[2]["candidate_id"], "c1")
        for event in failures:
            self.assertEqual(event["task_id"], "t")
        limited = instrument.recent_failures(self.ledger, "t", limit=1)
        self.assertEqual(len(limited), 1)
        self.assertEqual(limited[0]["event_id"],
                         failures[0]["event_id"])
        self.assertEqual(instrument.recent_failures(self.ledger, "other"),
                         [])

    def test_recent_failures_ignores_success_only_task(self):
        instrument.log_task_outcome(self.ledger, "t", True, "ok")
        self.assertEqual(instrument.recent_failures(self.ledger, "t"), [])


if __name__ == "__main__":
    unittest.main()
