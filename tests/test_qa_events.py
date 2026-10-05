"""QA seam tests: events.py chain-tamper variants.

Guards prove the tampers verify_chain() catches (payload/type/timestamp
mutation, middle-row delete, link tamper, fixed-hash rewrite); one
KNOWN-FAIL documents the fundamental gap (tail truncation needs an
external anchor — see QA report coverage gaps).
"""

import unittest

import events


def _ledger_with(*kinds):
    ledger = events.EventLedger(":memory:")
    for kind in kinds:
        ledger.append_event(kind, payload={"n": kind})
    return ledger


class EventChainTamperTest(unittest.TestCase):
    @unittest.expectedFailure  # KNOWN-FAIL (fundamental limitation)
    def test_tail_truncation_is_detected(self):
        """Tail-row DELETE leaves a valid chain (no external anchor).

        verify_chain() recomputes links it can see; a removed tail row
        is invisible. Fixing this needs an out-of-band anchor (expected
        event count / tip hash persisted by the caller), i.e. an API
        addition, not a one-line fix — hence tracked as a limitation.
        """
        ledger = _ledger_with("a", "b", "c")
        ledger.conn.execute("DELETE FROM events WHERE event_id = 3")
        ledger.conn.commit()
        self.assertFalse(ledger.verify_chain())

    def test_payload_tamper_detected(self):
        ledger = _ledger_with("a", "b")
        ledger.conn.execute(
            "UPDATE events SET payload = ? WHERE event_id = 1", ('{"evil": 1}',))
        ledger.conn.commit()
        self.assertFalse(ledger.verify_chain())

    def test_event_type_tamper_detected(self):
        ledger = _ledger_with("a", "b")
        ledger.conn.execute(
            "UPDATE events SET event_type = 'EVIL' WHERE event_id = 2")
        ledger.conn.commit()
        self.assertFalse(ledger.verify_chain())

    def test_timestamp_tamper_detected(self):
        ledger = _ledger_with("a", "b")
        ledger.conn.execute(
            "UPDATE events SET timestamp = timestamp + 100 WHERE event_id = 1")
        ledger.conn.commit()
        self.assertFalse(ledger.verify_chain())

    def test_middle_row_delete_detected(self):
        ledger = _ledger_with("a", "b", "c")
        ledger.conn.execute("DELETE FROM events WHERE event_id = 2")
        ledger.conn.commit()
        self.assertFalse(ledger.verify_chain())

    def test_previous_hash_tamper_detected(self):
        ledger = _ledger_with("a", "b")
        ledger.conn.execute(
            "UPDATE events SET previous_hash = '00' WHERE event_id = 2")
        ledger.conn.commit()
        self.assertFalse(ledger.verify_chain())

    def test_rewritten_hash_without_relinking_detected(self):
        """Attacker fixes row 1's hash but cannot fix row 2's link."""
        ledger = _ledger_with("a", "b")
        row = ledger.get_event(1)
        fixed = events.compute_event_hash(
            row["previous_hash"], row["timestamp"], "EVIL",
            row["generation"], row["task_id"], row["candidate_id"],
            row["capability_id"], row["capability_version"],
            row["payload"])
        ledger.conn.execute(
            "UPDATE events SET event_type = 'EVIL', hash = ? "
            "WHERE event_id = 1", (fixed,))
        ledger.conn.commit()
        self.assertFalse(ledger.verify_chain())

    def test_untampered_chain_verifies(self):
        self.assertTrue(_ledger_with("a", "b", "c").verify_chain())

    def test_empty_ledger_verifies_vacuously(self):
        self.assertTrue(events.EventLedger(":memory:").verify_chain())


if __name__ == "__main__":
    unittest.main()
