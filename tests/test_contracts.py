"""Tests for task success contracts + green-stop gate (contracts.py). Stdlib only."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contracts
from contracts import PostSuccessTracker, TaskContract, check_contract, halt_on_green


def _pipeline_verdict(passed=True, evidence=None):
    if evidence is None:
        evidence = {"tests": "12/12", "prop": "500/500"}
    return {
        "passed": passed,
        "evidence": evidence,
        "failures": [] if passed else [{"property": "roundtrip"}],
    }


def _evaluator_verdict(verdict="pass"):
    return {
        "verdict": verdict,
        "evidence": {"summary": {"candidate_id": "c1", "passed_cases": 3}},
    }


def _contract():
    return TaskContract(
        name="roundtrip-csv",
        predicates={
            "pipeline_passed": lambda v: bool(v.get("passed"))
            or v.get("verdict") == "pass",
            "no_failures": lambda v: not v.get("failures"),
        },
        required_evidence=("tests",),
    )


class CheckContractTest(unittest.TestCase):
    def test_satisfied_pipeline_verdict(self):
        result = check_contract(_contract(), _pipeline_verdict(passed=True))
        self.assertEqual(set(result), {"satisfied", "missing", "evidence"})
        self.assertTrue(result["satisfied"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["evidence"], {"tests": "12/12"})

    def test_unsatisfied_lists_failing_predicates(self):
        result = check_contract(_contract(), _pipeline_verdict(passed=False))
        self.assertFalse(result["satisfied"])
        self.assertIn("pipeline_passed", result["missing"])
        self.assertIn("no_failures", result["missing"])

    def test_missing_evidence_blocks_satisfaction(self):
        verdict = _pipeline_verdict(passed=True, evidence={"other": 1})
        result = check_contract(_contract(), verdict)
        self.assertFalse(result["satisfied"])
        self.assertIn("evidence:tests", result["missing"])
        self.assertEqual(result["evidence"], {})

    def test_works_with_evaluator_verdict_shape(self):
        contract = TaskContract(
            name="eval-gate",
            predicates={"evaluator_pass": lambda v: v.get("verdict") == "pass"},
            required_evidence=("summary",),
        )
        result = check_contract(contract, _evaluator_verdict("pass"))
        self.assertTrue(result["satisfied"])
        self.assertIn("summary", result["evidence"])

    def test_raising_predicate_counts_as_unmet(self):
        def boom(verdict):
            raise RuntimeError("broken check")

        contract = TaskContract(name="broken", predicates={"boom": boom})
        result = check_contract(contract, _pipeline_verdict())
        self.assertFalse(result["satisfied"])
        self.assertIn("boom", result["missing"])

    def test_rejects_non_mapping_verdict(self):
        with self.assertRaises(TypeError):
            check_contract(_contract(), "pass")


class HaltOnGreenTest(unittest.TestCase):
    def test_halts_exactly_when_satisfied(self):
        contract = _contract()
        self.assertTrue(halt_on_green(contract, _pipeline_verdict(True)))
        self.assertFalse(halt_on_green(contract, _pipeline_verdict(False)))
        no_evidence = _pipeline_verdict(True, evidence={})
        self.assertFalse(halt_on_green(contract, no_evidence))


class PostSuccessTrackerTest(unittest.TestCase):
    def test_pre_green_actions_not_flagged(self):
        tracker = PostSuccessTracker()
        self.assertIsNone(tracker.record_action("model_call"))
        self.assertIsNone(tracker.record_action("refactor"))
        self.assertEqual(tracker.action_count, 2)
        self.assertEqual(tracker.violation_count, 0)
        self.assertEqual(tracker.violations, ())

    def test_post_green_action_is_flagged(self):
        tracker = PostSuccessTracker()
        tracker.record_action("model_call")
        tracker.mark_green("roundtrip-csv")
        self.assertTrue(tracker.green)
        violation = tracker.record_action("optional_refactor",
                                           detail={"fn": "escape-field"})
        self.assertIsNotNone(violation)
        self.assertEqual(violation["type"], "post_success_action")
        self.assertEqual(violation["action"], "optional_refactor")
        self.assertEqual(violation["contract"], "roundtrip-csv")
        self.assertEqual(tracker.violation_count, 1)
        self.assertEqual(len(tracker.violations), 1)

    def test_every_post_green_action_flagged_in_order(self):
        tracker = PostSuccessTracker()
        tracker.mark_green()
        first = tracker.record_action("model_call")
        second = tracker.record_action("pipeline_rerun")
        self.assertEqual((first["seq"], second["seq"]), (1, 2))
        self.assertEqual(tracker.violation_count, 2)
        self.assertEqual(tracker.action_count, 2)

    def test_rejects_empty_action(self):
        tracker = PostSuccessTracker()
        with self.assertRaises(ValueError):
            tracker.record_action("")


if __name__ == "__main__":
    unittest.main()
