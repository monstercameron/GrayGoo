"""Tests for the promotion authority (promotion.py). Stdlib only, offline.

Covers: stale-generation rejection, missing-risk-gate rejection,
mandatory hidden-evaluator verdict over the REAL subprocess service
(wrong output rejects, unreachable service fails closed), full-green
promotion with version record + monotonic epoch + ledger event,
version-record immutability, and back-only rollback.

All version state lives in tempfiles; the real versions/ directory is
never touched. No live API calls.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import promotion
import risk
import transfer
from evaluator import service as evaluator_service
from events import EventLedger

GENERATION = 7


def correct_outputs():
    outputs = {}
    for case in evaluator_service.load_hidden_cases():
        for i, check in enumerate(case["checks"]):
            outputs["%s:%d" % (case["id"], i)] = check["expected"]
    return outputs


def good_transfer_outcomes(n=3):
    return [{"patch_id": "cand-1", "task_id": "t%d" % i, "helped": True,
             "tokens_saved": 100.0, "latency_saved_ms": 50.0}
            for i in range(n)]


def green_evidence(**overrides):
    evidence = {
        "source_generation": GENERATION,
        "capability_id": "demo-cap",
        "version": 1,
        "risk_level": "R0",
        "gates_passed": list(risk.GATES["R0"]),
        "outputs": correct_outputs(),
        "transfer": {"outcomes": good_transfer_outcomes()},
    }
    evidence.update(overrides)
    return evidence


class PromotionGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.versions = os.path.join(self.tmp.name, "versions")
        self.ledger = EventLedger(":memory:")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.ledger.close)

    # -- gate (a): generation currency ------------------------------------

    def test_reject_on_stale_generation(self):
        evidence = green_evidence(source_generation=GENERATION - 1)
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions)
        self.assertEqual(result["decision"], "reject")
        self.assertTrue(any("stale" in r.lower() for r in result["reasons"]))
        self.assertIsNone(result["version"])
        self.assertFalse(os.path.exists(self.versions))

    def test_reject_on_missing_source_generation(self):
        evidence = green_evidence()
        del evidence["source_generation"]
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions)
        self.assertEqual(result["decision"], "reject")

    # -- gate (b): risk gates ----------------------------------------------

    def test_reject_on_missing_risk_gates(self):
        evidence = green_evidence(gates_passed=["unit-tests"])
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions)
        self.assertEqual(result["decision"], "reject")
        self.assertTrue(any("isolated-compile" in r for r in result["reasons"]))
        self.assertFalse(os.path.exists(self.versions))

    def test_reject_r6_always(self):
        evidence = green_evidence(
            risk_level="R6",
            gates_passed=list(risk.GATES["R6"]))
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions)
        self.assertEqual(result["decision"], "reject")
        self.assertTrue(any("R6" in r or "forbidden" in r.lower()
                            for r in result["reasons"]))

    def test_reject_on_failing_transfer_evidence(self):
        bad = good_transfer_outcomes()
        bad.append({"patch_id": "cand-1", "task_id": "t9", "helped": False,
                    "severe": True})
        evidence = green_evidence(transfer={"outcomes": bad})
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions)
        self.assertEqual(result["decision"], "reject")
        self.assertTrue(any("transfer" in r.lower()
                            for r in result["reasons"]))

    # -- gate (c): mandatory hidden-evaluator verdict -----------------------

    def test_reject_when_evaluator_verdict_fails(self):
        outputs = correct_outputs()
        first_key = sorted(outputs)[0]
        outputs[first_key] = "definitely wrong output"
        evidence = green_evidence(outputs=outputs)
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions)
        self.assertEqual(result["decision"], "reject")
        self.assertTrue(any("fail" in r.lower() for r in result["reasons"]))
        self.assertFalse(os.path.exists(self.versions))

    def test_evaluator_call_defaults_to_coarse_detail(self):
        # issues.md #68: promotion must not pull per-case hidden outcomes.
        envelope = promotion._call_evaluator("cand-1", correct_outputs())
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["verdict"], "pass")
        self.assertNotIn("cases", envelope["evidence"])
        self.assertEqual(envelope["evidence"]["detail"], "coarse")

    def test_evaluator_call_full_detail_opt_in(self):
        envelope = promotion._call_evaluator("cand-1", correct_outputs(),
                                             detail="full")
        self.assertTrue(envelope["ok"])
        self.assertIn("cases", envelope["evidence"])

    def test_reject_when_service_unreachable_fail_closed(self):
        evidence = green_evidence()
        bad_path = os.path.join(self.tmp.name, "no-such-service.py")
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions,
            service_path=bad_path)
        self.assertEqual(result["decision"], "reject")
        self.assertTrue(any("unreachable" in r.lower() or "no verdict" in r.lower()
                            for r in result["reasons"]))
        self.assertFalse(os.path.exists(self.versions))

    # -- full green promotion ------------------------------------------------

    def test_promote_on_full_green_with_real_verdict(self):
        result = promotion.evaluate_promotion(
            "cand-1", green_evidence(), generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions)
        self.assertEqual(result["decision"], "promote")
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["epoch"], 1)

        cap_path = os.path.join(self.versions, "demo-cap.json")
        self.assertTrue(os.path.exists(cap_path))
        with open(cap_path, encoding="utf-8") as handle:
            doc = json.load(handle)
        self.assertEqual(doc["current_version"], 1)
        self.assertIn("1", doc["versions"])
        self.assertEqual(doc["versions"]["1"]["evaluator_verdict"], "pass")
        self.assertEqual(doc["versions"]["1"]["epoch"], 1)

        with open(os.path.join(self.versions, "epochs.json"),
                  encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["epoch"], 1)

        events = self.ledger.get_events_by_type("candidate_promoted")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["candidate_id"], "cand-1")
        self.assertEqual(events[0]["capability_version"], 1)
        self.assertTrue(self.ledger.verify_chain())

    def test_second_promotion_advances_epoch(self):
        first = promotion.evaluate_promotion(
            "cand-1", green_evidence(), generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions)
        second = promotion.evaluate_promotion(
            "cand-2", green_evidence(version=2), generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions)
        self.assertEqual((first["epoch"], second["epoch"]), (1, 2))
        self.assertEqual(second["version"], 2)
        self.assertEqual(
            promotion.get_current_version("demo-cap", self.versions), 2)
        self.assertEqual(promotion.get_current_epoch(self.versions), 2)

    def test_version_record_immutable(self):
        first = promotion.evaluate_promotion(
            "cand-1", green_evidence(), generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions)
        self.assertEqual(first["decision"], "promote")
        again = promotion.evaluate_promotion(
            "cand-2", green_evidence(version=1), generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions)
        self.assertEqual(again["decision"], "reject")
        self.assertTrue(any("immutable" in r.lower()
                            for r in again["reasons"]))

    # -- transfer corroboration (issue 63) -----------------------------------

    def _recorded_tracker(self, rows):
        tracker = transfer.TransferTracker()
        for row in rows:
            tracker.record_outcome(dict(row, patch_id="cand-1"))
        return tracker

    def test_fabricated_transfer_rows_rejected_with_tracker(self):
        tracker = self._recorded_tracker(good_transfer_outcomes())
        forged = good_transfer_outcomes() + [
            {"patch_id": "cand-1", "task_id": "ghost-task",
             "helped": True, "tokens_saved": 999.0,
             "latency_saved_ms": 999.0}]
        evidence = green_evidence(transfer={"outcomes": forged})
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions,
            transfer_tracker=tracker)
        self.assertEqual(result["decision"], "reject")
        self.assertTrue(any("corroborat" in r.lower()
                            for r in result["reasons"]))
        self.assertFalse(os.path.exists(self.versions))

    def test_flipped_helped_bit_rejected_with_tracker(self):
        rows = good_transfer_outcomes()
        tracker = self._recorded_tracker(rows)
        flipped = [dict(rows[0], helped=False)] + rows[1:]
        evidence = green_evidence(transfer={"outcomes": flipped})
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions,
            transfer_tracker=tracker)
        self.assertEqual(result["decision"], "reject")
        self.assertTrue(any("corroborat" in r.lower()
                            for r in result["reasons"]))

    def test_recorded_rows_promote_with_tracker(self):
        rows = good_transfer_outcomes()
        tracker = self._recorded_tracker(rows)
        evidence = green_evidence(transfer={"outcomes": rows})
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions,
            transfer_tracker=tracker)
        self.assertEqual(result["decision"], "promote")

    def test_subset_of_recorded_rows_promote_with_tracker(self):
        rows = good_transfer_outcomes(4)
        tracker = self._recorded_tracker(rows)
        evidence = green_evidence(transfer={"outcomes": rows[:3]})
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions,
            transfer_tracker=tracker)
        self.assertEqual(result["decision"], "promote")

    # -- rollback ------------------------------------------------------------

    def test_rollback_moves_pointer_back_only(self):
        promotion.evaluate_promotion(
            "cand-1", green_evidence(), generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions)
        promotion.evaluate_promotion(
            "cand-2", green_evidence(version=2), generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions)
        moved = promotion.set_current_version(
            "demo-cap", 1, versions_dir=self.versions)
        self.assertEqual(moved["current_version"], 1)
        self.assertEqual(moved["previous_version"], 2)
        self.assertEqual(
            promotion.get_current_version("demo-cap", self.versions), 1)
        # Records survive the rollback untouched.
        with open(os.path.join(self.versions, "demo-cap.json"),
                  encoding="utf-8") as handle:
            doc = json.load(handle)
        self.assertIn("2", doc["versions"])
        # Forward (or no-op) moves are refused.
        with self.assertRaises(ValueError):
            promotion.set_current_version(
                "demo-cap", 2, versions_dir=self.versions)
        with self.assertRaises(ValueError):
            promotion.set_current_version(
                "demo-cap", 1, versions_dir=self.versions)
        with self.assertRaises(KeyError):
            promotion.set_current_version(
                "demo-cap", 99, versions_dir=self.versions)
        with self.assertRaises(KeyError):
            promotion.set_current_version(
                "unknown-cap", 1, versions_dir=self.versions)


if __name__ == "__main__":
    unittest.main()
