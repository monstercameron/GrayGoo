"""QA seam tests: promotion.py fail-closed paths.

Proves QA-05 (_safe_name collisions merge capability histories),
QA-06 (ledger outage raises instead of deciding), QA-09 (corrupt
version state silently resets). Guards lock the fail-closed gates
that already hold (R6, stale generation, missing outputs,
unreachable evaluator). Full-promote tests reuse the offline
evaluator subprocess with corpus-echoed outputs (no network).
"""

import json
import os
import tempfile
import unittest

import promotion
import risk
from evaluator import service as _evaluator_service


def _passing_outputs():
    outputs = {}
    for case in _evaluator_service.load_hidden_cases():
        for index, check in enumerate(case["checks"]):
            outputs["%s:%d" % (case["id"], index)] = check["expected"]
    return outputs


def _evidence(capability_id, **over):
    evidence = {"source_generation": 1, "capability_id": capability_id,
                "risk_level": "R0",
                "gates_passed": list(risk.GATES["R0"]),
                "outputs": _passing_outputs()}
    evidence.update(over)
    return evidence


class _BrokenLedger:
    def append_event(self, *args, **kwargs):
        raise RuntimeError("ledger disk on fire")


class PromotionFailClosedTest(unittest.TestCase):
    def test_distinct_capability_ids_keep_distinct_histories(self):
        """QA-05: "a/b" and "a_b" share one versions file.

        _safe_name maps both to "a_b", so the second promote absorbs
        the first capability's version 1 under the wrong id. Suggested
        fix: collision-proof naming (e.g. hash-qualified filenames) or
        reject ids that do not survive the mapping round-trip.
        """
        tmpdir = tempfile.mkdtemp(prefix="qa-promo-")
        first = promotion.evaluate_promotion(
            "cand-1", _evidence("a/b"), generation=1, versions_dir=tmpdir)
        self.assertEqual(first["decision"], "promote")
        second = promotion.evaluate_promotion(
            "cand-2", _evidence("a_b"), generation=1, versions_dir=tmpdir)
        self.assertEqual(second["decision"], "promote")
        doc = promotion._load_capability_doc(tmpdir, "a/b")
        self.assertEqual(doc["capability_id"], "a/b")
        self.assertEqual(doc["current_version"], 1)

    def test_ledger_outage_still_returns_a_decision(self):
        """QA-06: broken ledger must not escape as an exception.

        Currently a ledger.append_event failure propagates out of
        evaluate_promotion, so the caller gets no decision at all.
        Suggested fix: wrap ledger appends; the reject path must
        always return its reject dict (fail closed, audibly).
        """
        result = promotion.evaluate_promotion(
            "", {"source_generation": 1}, generation=1,
            ledger=_BrokenLedger())
        self.assertEqual(result["decision"], "reject")

    def test_corrupt_version_state_does_not_silently_reset(self):
        """QA-09: corrupt versions file promotes as v1, epoch restarts.

        Currently garbage in versions/<cap>.json is treated as "never
        promoted" and the run reports "all gates passed"; likewise a
        corrupt epochs.json restarts the epoch at 1 (non-monotonic).
        Suggested fix: reject on unreadable state, or promote only
        with an explicit audited reset reason.
        """
        tmpdir = tempfile.mkdtemp(prefix="qa-promo-")
        with open(os.path.join(tmpdir, "cap.json"), "w",
                  encoding="utf-8") as handle:
            handle.write("{corrupt json!!!")
        result = promotion.evaluate_promotion(
            "cand-9", _evidence("cap"), generation=1, versions_dir=tmpdir)
        self.assertEqual(result["decision"], "reject")

    def test_r6_rejects_without_touching_evaluator(self):
        """Guard: R6 short-circuits before gate (c) (no subprocess)."""
        tmpdir = tempfile.mkdtemp(prefix="qa-promo-")
        result = promotion.evaluate_promotion(
            "cand-x", _evidence("cap", risk_level="R6"), generation=1,
            versions_dir=tmpdir,
            service_path="definitely-not-a-service.py")
        self.assertEqual(result["decision"], "reject")
        self.assertTrue(any("R6" in reason for reason in result["reasons"]))

    def test_stale_generation_rejects(self):
        """Guard: wrong source generation fails closed."""
        tmpdir = tempfile.mkdtemp(prefix="qa-promo-")
        result = promotion.evaluate_promotion(
            "cand-x", _evidence("cap", source_generation=0), generation=1,
            versions_dir=tmpdir)
        self.assertEqual(result["decision"], "reject")

    def test_missing_outputs_rejects(self):
        """Guard: no evaluator outputs means no hidden verdict."""
        tmpdir = tempfile.mkdtemp(prefix="qa-promo-")
        evidence = _evidence("cap")
        del evidence["outputs"]
        result = promotion.evaluate_promotion(
            "cand-x", evidence, generation=1, versions_dir=tmpdir)
        self.assertEqual(result["decision"], "reject")

    def test_unreachable_evaluator_rejects(self):
        """Guard: evaluator crash/timeout/misbehavior blocks promotion."""
        tmpdir = tempfile.mkdtemp(prefix="qa-promo-")
        result = promotion.evaluate_promotion(
            "cand-x", _evidence("cap"), generation=1, versions_dir=tmpdir,
            service_path="definitely-not-a-service.py")
        self.assertEqual(result["decision"], "reject")
        self.assertTrue(any("evaluator" in reason.lower()
                            for reason in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
