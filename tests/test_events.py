"""Tests for the append-only event ledger (events.py). Stdlib only."""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import events


class EventLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "ledger.db")
        self.ledger = events.EventLedger(self.db_path)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_append_and_retrieve_round_trip(self):
        stored = self.ledger.append_event(
            "candidate generated",
            generation=3,
            task_id="task-1",
            candidate_id="cand-7",
            capability_id="fetch-customer-history",
            capability_version=18,
            payload={"ast": "(+ 1 2)", "model": "qwen-test"},
        )
        self.assertIsNotNone(stored["event_id"])
        self.assertIsNotNone(stored["hash"])
        fetched = self.ledger.get_event(stored["event_id"])
        self.assertEqual(fetched, stored)
        self.assertEqual(fetched["event_type"], "candidate generated")
        self.assertEqual(fetched["task_id"], "task-1")
        self.assertIn("(+ 1 2)", fetched["payload"])

        by_task = self.ledger.get_events_by_task("task-1")
        self.assertEqual(len(by_task), 1)
        self.assertEqual(by_task[0]["event_id"], stored["event_id"])

        by_type = self.ledger.get_events_by_type("candidate generated")
        self.assertEqual(len(by_type), 1)

        recent = self.ledger.get_recent_events(limit=10)
        self.assertEqual(len(recent), 1)

    def test_chain_verification_passes(self):
        for i, kind in enumerate(["task started", "model called",
                                  "candidate generated", "candidate passed",
                                  "candidate promoted", "task completed"]):
            self.ledger.append_event(kind, generation=1, task_id="t",
                                     payload={"seq": i})
        self.assertTrue(self.ledger.verify_chain())
        recent = self.ledger.get_recent_events(limit=2)
        self.assertEqual(recent[0]["event_type"], "task completed")
        self.assertEqual(recent[1]["event_type"], "candidate promoted")

    def test_tampering_detected_by_verify_chain(self):
        self.ledger.append_event("task started", generation=1, task_id="t")
        self.ledger.append_event("model called", generation=1, task_id="t")
        self.assertTrue(self.ledger.verify_chain())

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE events SET payload = ? WHERE event_type = ?",
            ('{"evil": true}', "model called"))
        conn.commit()
        conn.close()

        self.assertFalse(self.ledger.verify_chain())

    def test_model_call_record_round_trip(self):
        stored = self.ledger.log_model_call(
            provider="cerebras",
            model="qwen-test",
            input_tokens=120,
            output_tokens=45,
            context_hash="abc123",
            prompt_version="v2",
            generation=4,
            candidate_id="cand-9",
            result="pass",
            cost=0.0012,
            latency_ms=321.5,
        )
        self.assertIsNotNone(stored["call_id"])
        fetched = self.ledger.get_model_call(stored["call_id"])
        self.assertEqual(fetched, stored)
        self.assertEqual(fetched["provider"], "cerebras")
        self.assertEqual(fetched["input_tokens"], 120)
        self.assertEqual(fetched["latency_ms"], 321.5)

        calls = self.ledger.list_model_calls()
        self.assertEqual(len(calls), 1)
        gen_calls = self.ledger.list_model_calls(generation=4)
        self.assertEqual(len(gen_calls), 1)
        self.assertEqual(self.ledger.list_model_calls(generation=999), [])

    def test_persistence_across_reconnects(self):
        first = self.ledger.append_event("task started", task_id="persist")
        call = self.ledger.log_model_call(provider="cerebras", model="m",
                                          result="pass")
        self.ledger.close()

        reopened = events.EventLedger(self.db_path)
        try:
            self.assertEqual(reopened.get_event(first["event_id"]), first)
            self.assertEqual(reopened.get_model_call(call["call_id"]), call)
            self.assertTrue(reopened.verify_chain())
            # Chain must extend correctly after reopen.
            reopened.append_event("task completed", task_id="persist")
            self.assertTrue(reopened.verify_chain())
            self.assertEqual(len(reopened.get_events_by_task("persist")), 2)
        finally:
            reopened.close()

    def test_no_update_or_delete_api(self):
        for name in ("update", "delete", "remove", "edit", "modify"):
            self.assertFalse(
                any(name in attr.lower()
                    for attr in dir(events.EventLedger)),
                "ledger must stay append-only (found %r-like attr)" % name)


if __name__ == "__main__":
    unittest.main()
