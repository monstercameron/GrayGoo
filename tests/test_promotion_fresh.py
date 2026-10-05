"""Tests for fresh-case promotion wiring (todos.md: execute candidates on
fresh inputs in the promotion path). Stdlib only, offline.

Covers: protocol ``fresh_inputs`` action (+ backward compat for legacy
requests), inputs-only disclosure (no expected answers leak),
determinism, the REAL subprocess round trip, full-green promotion with
fresh outputs (kwarg + evidence paths), fail-closed rejection on
missing fresh outputs and malformed specs, and fetch_fresh_inputs.

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
from evaluator import protocol
from evaluator import service as evaluator_service
from events import EventLedger

GENERATION = 7
FRESH = {"seed": 3, "per_case": 1}


def correct_outputs():
    outputs = {}
    for case in evaluator_service.load_hidden_cases():
        for i, check in enumerate(case["checks"]):
            outputs["%s:%d" % (case["id"], i)] = check["expected"]
    return outputs


def correct_fresh_outputs(seed, per_case):
    outputs = {}
    fresh = evaluator_service.generate_fresh_cases(
        evaluator_service.load_hidden_cases(), seed=seed, per_case=per_case)
    for case in fresh:
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


class FreshProtocolTest(unittest.TestCase):
    def test_legacy_request_defaults_to_evaluate(self):
        request = protocol.make_request("c", {"a:0": "x"})
        normalized = protocol.validate_request(request)
        self.assertEqual(normalized["action"], "evaluate")

    def test_fresh_inputs_request_validates(self):
        request = protocol.make_fresh_inputs_request("c", seed=3, per_case=1)
        normalized = protocol.validate_request(request)
        self.assertEqual(normalized["action"], "fresh_inputs")
        self.assertEqual(normalized["fresh"], {"seed": 3, "per_case": 1})

    def test_fresh_inputs_needs_no_outputs(self):
        request = protocol.make_fresh_inputs_request("c")
        self.assertNotIn("outputs", request)
        protocol.validate_request(request)  # must not raise

    def test_unknown_action_rejected(self):
        request = protocol.make_request("c", {"a:0": "x"})
        request["action"] = "delete_corpus"
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(request)

    def test_evaluate_still_requires_outputs(self):
        request = protocol.make_request("c", {"a:0": "x"})
        del request["outputs"]
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(request)


class FreshInputsDisclosureTest(unittest.TestCase):
    def test_inputs_only_and_deterministic(self):
        first = evaluator_service.fresh_case_inputs(seed=3, per_case=1)
        second = evaluator_service.fresh_case_inputs(seed=3, per_case=1)
        self.assertTrue(first)
        self.assertEqual(first, second)
        for entry in first:
            self.assertEqual(set(entry), {"key", "case_id", "index", "input"})
        self.assertNotIn("expected", json.dumps(first))

    def test_inputs_match_scored_fresh_keys(self):
        inputs = evaluator_service.fresh_case_inputs(
            seed=FRESH["seed"], per_case=FRESH["per_case"])
        fresh = evaluator_service.generate_fresh_cases(
            evaluator_service.load_hidden_cases(),
            seed=FRESH["seed"], per_case=FRESH["per_case"])
        scored = {"%s:%d" % (c["id"], i)
                  for c in fresh for i in range(len(c["checks"]))}
        self.assertEqual({e["key"] for e in inputs}, scored)

    def test_subprocess_round_trip_discloses_no_answers(self):
        request = protocol.make_fresh_inputs_request(
            "cand-1", seed=3, per_case=1)
        envelope = evaluator_service.evaluate_in_subprocess(request,
                                                            timeout=60)
        self.assertTrue(envelope.get("ok"))
        self.assertEqual(envelope.get("action"), "fresh_inputs")
        self.assertTrue(envelope.get("inputs"))
        self.assertNotIn("expected", json.dumps(envelope))
        self.assertNotIn("verdict", envelope)


class FreshPromotionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.versions = os.path.join(self.tmp.name, "versions")
        self.ledger = EventLedger(":memory:")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.ledger.close)

    def test_promote_with_fresh_outputs_kwarg(self):
        outputs = correct_outputs()
        outputs.update(correct_fresh_outputs(FRESH["seed"],
                                             FRESH["per_case"]))
        evidence = green_evidence(outputs=outputs)
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions, fresh=FRESH)
        self.assertEqual(result["decision"], "promote")
        with open(os.path.join(self.versions, "demo-cap.json"),
                  encoding="utf-8") as handle:
            doc = json.load(handle)
        record = doc["versions"]["1"]
        self.assertEqual(record["fresh"]["seed"], FRESH["seed"])
        self.assertGreater(record["fresh"]["generated"], 0)

    def test_promote_with_fresh_in_evidence(self):
        outputs = correct_outputs()
        outputs.update(correct_fresh_outputs(FRESH["seed"],
                                             FRESH["per_case"]))
        evidence = green_evidence(outputs=outputs, fresh=dict(FRESH))
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions)
        self.assertEqual(result["decision"], "promote")

    def test_missing_fresh_outputs_reject(self):
        evidence = green_evidence()  # static outputs only
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions, fresh=FRESH)
        self.assertEqual(result["decision"], "reject")
        self.assertFalse(os.path.exists(self.versions))

    def test_malformed_fresh_spec_rejects_closed(self):
        evidence = green_evidence()
        result = promotion.evaluate_promotion(
            "cand-1", evidence, generation=GENERATION,
            ledger=self.ledger, versions_dir=self.versions,
            fresh={"seed": -1, "per_case": 1})
        self.assertEqual(result["decision"], "reject")
        self.assertTrue(any("fresh" in r.lower()
                            for r in result["reasons"]))
        self.assertFalse(os.path.exists(self.versions))

    def test_fetch_fresh_inputs_helper(self):
        fetched = promotion.fetch_fresh_inputs(dict(FRESH), timeout=60)
        direct = evaluator_service.fresh_case_inputs(
            seed=FRESH["seed"], per_case=FRESH["per_case"])
        self.assertEqual(fetched["inputs"], direct)
        self.assertEqual(fetched["generated"], len(direct))
        with self.assertRaises(ValueError):
            promotion.fetch_fresh_inputs({"seed": "x"})


if __name__ == "__main__":
    unittest.main()
