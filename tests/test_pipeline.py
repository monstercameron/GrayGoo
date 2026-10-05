"""Tests for the mutation-pipeline orchestrator (pipeline.py). Stdlib only.

All sibling dependencies (workers, risk classifier) are injected fakes:
pipeline.py must never import workers.py or risk.py.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline

GOOD_CANDIDATE = (
    "(candidate (:target fast-double) (:parent 3) "
    "(:definition (defun fast-double (x) (+ x x))))"
)

REQUIRED_KEYS = {"ok", "verdict", "failures", "evidence", "risk", "stages"}


def ok_envelope(return_value=None, elapsed_ms=2):
    return {"ok": True, "stdout": "", "return_value": return_value,
            "error": None, "timed_out": False, "elapsed_ms": elapsed_ms}


def fail_envelope(error="boom"):
    return {"ok": False, "stdout": "", "return_value": None,
            "error": error, "timed_out": False, "elapsed_ms": 1}


class CountingWorker:
    """Fake worker_fn recording every code string it receives."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, code):
        self.calls.append(code)
        return self.handler(code)


def risk_r0(parsed):
    assert isinstance(parsed, dict) and "definition" in parsed
    return {"level": "R0", "reasons": [], "gates": []}


class RunCandidateTest(unittest.TestCase):
    def test_malformed_candidate_fails_without_calling_worker(self):
        worker = CountingWorker(lambda code: ok_envelope())
        risk_calls = []
        for bad in ("(candidate (:target foo)",
                    "not an s-expression ((( ",
                    "(candidate (:target foo) (:parent 0))"):
            result = pipeline.run_candidate(
                bad, tests={"direct": ["(run-tests)"]},
                worker_fn=worker,
                risk_fn=lambda parsed: risk_calls.append(parsed))
            self.assertFalse(result["ok"], bad)
            self.assertEqual(result["verdict"]["failed_stage"], "parse")
        self.assertEqual(worker.calls, [])
        self.assertEqual(risk_calls, [])

    def test_all_pass_run_yields_pass_verdict(self):
        worker = CountingWorker(lambda code: ok_envelope(return_value=4))
        result = pipeline.run_candidate(
            GOOD_CANDIDATE,
            tests={
                "direct": ["(t1)", {"code": "(t2)", "expect": 4}],
                "regression": ["(reg-1)"],
                "property": ["(prop-1)"],
                "differential": {"cases": [{"old": 1, "new": 1}]},
                "performance": {"budget_ms": 100, "samples": [2, 3]},
            },
            worker_fn=worker,
            risk_fn=risk_r0,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["verdict"]["pass"])
        self.assertIsNone(result["verdict"]["failed_stage"])
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["risk"]["level"], "R0")
        statuses = {s["name"]: s["status"] for s in result["stages"]}
        for name in ("parse", "risk", "direct", "regression", "property",
                     "differential", "performance"):
            self.assertEqual(statuses[name], "pass", name)

    def test_direct_failure_short_circuits_later_stages(self):
        def handler(code):
            if "REG" in code or "PROP" in code or "DIFF" in code:
                raise AssertionError("short-circuited stage executed: %r"
                                     % (code,))
            if "BAD" in code:
                return fail_envelope("direct assertion failed")
            return ok_envelope()

        worker = CountingWorker(handler)
        result = pipeline.run_candidate(
            GOOD_CANDIDATE,
            tests={
                "direct": ["(GOOD-1)", "(BAD)", "(GOOD-2)"],
                "regression": ["(REG-1)"],
                "property": ["(PROP-1)"],
                "differential": {"cases": [
                    {"old": {"code": "(DIFF-old)"},
                     "new": {"code": "(DIFF-new)"}}]},
                "performance": {"budget_ms": 100, "cases": ["(PERF-1)"]},
            },
            worker_fn=worker,
            risk_fn=risk_r0,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["verdict"]["failed_stage"], "direct")
        statuses = {s["name"]: s["status"] for s in result["stages"]}
        self.assertEqual(statuses["direct"], "fail")
        for name in ("regression", "property", "differential",
                     "performance"):
            self.assertEqual(statuses[name], "skipped", name)
        # Only direct items reached the worker.
        self.assertEqual(worker.calls, ["(GOOD-1)", "(BAD)", "(GOOD-2)"])
        self.assertIn("direct", result["evidence"])
        self.assertNotIn("regression", result["evidence"])

    def test_differential_stage_flags_old_new_divergence(self):
        worker = CountingWorker(lambda code: ok_envelope(return_value=99))
        result = pipeline.run_candidate(
            GOOD_CANDIDATE,
            tests={
                "direct": ["(t1)"],
                "differential": {"cases": [
                    {"old": 1, "new": 1},
                    {"old": 2, "new": 3},
                    {"old": {"code": "(old-impl)"}, "new": 7},
                ]},
            },
            worker_fn=worker,
            risk_fn=risk_r0,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["verdict"]["failed_stage"], "differential")
        evidence = result["evidence"]["differential"]
        self.assertEqual(evidence["divergences"], 2)
        diverged = [c for c in evidence["cases"] if not c["pass"]]
        self.assertEqual(len(diverged), 2)
        self.assertEqual(diverged[0]["old_value"], 2)
        self.assertEqual(diverged[0]["new_value"], 3)

    def test_verdict_contains_every_required_key(self):
        failing = pipeline.run_candidate(
            "(broken", tests={"direct": ["(t)"]},
            worker_fn=CountingWorker(lambda code: ok_envelope()))
        passing = pipeline.run_candidate(
            GOOD_CANDIDATE, tests={"direct": ["(t)"]},
            worker_fn=CountingWorker(lambda code: ok_envelope()),
            risk_fn=risk_r0)
        for result in (failing, passing):
            self.assertTrue(REQUIRED_KEYS <= set(result),
                            "missing keys: %s"
                            % (REQUIRED_KEYS - set(result)))
            verdict = result["verdict"]
            self.assertTrue({"pass", "reason", "failed_stage"} <= set(verdict))
            self.assertIsInstance(result["failures"], list)
            self.assertIsInstance(result["evidence"], dict)
            self.assertIsInstance(result["risk"], dict)
            self.assertIsInstance(result["stages"], list)
            self.assertEqual(result["ok"], verdict["pass"])
        self.assertFalse(failing["ok"])
        self.assertTrue(passing["ok"])

    def test_risk_none_fails_closed_without_worker_use(self):
        # Issue 71: no classifier must fail closed, never execute.
        worker = CountingWorker(lambda code: ok_envelope())
        result = pipeline.run_candidate(
            GOOD_CANDIDATE, tests={"direct": ["(t)"]},
            worker_fn=worker)
        self.assertFalse(result["ok"])
        self.assertEqual(result["risk"]["level"], "unclassified")
        self.assertEqual(result["verdict"]["failed_stage"], "risk")
        self.assertEqual(worker.calls, [])

    def test_r6_risk_blocks_before_any_worker_use(self):
        worker = CountingWorker(lambda code: ok_envelope())
        result = pipeline.run_candidate(
            GOOD_CANDIDATE, tests={"direct": ["(t)"]},
            worker_fn=worker,
            risk_fn=lambda parsed: {"level": "R6",
                                    "reasons": ["touches evaluator"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["verdict"]["failed_stage"], "risk")
        self.assertEqual(worker.calls, [])

    def test_performance_budget_violation_fails(self):
        result = pipeline.run_candidate(
            GOOD_CANDIDATE,
            tests={"performance": {"budget_ms": 10, "samples": [4, 25]}},
            worker_fn=CountingWorker(lambda code: ok_envelope()),
            risk_fn=risk_r0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["verdict"]["failed_stage"], "performance")
        self.assertEqual(result["evidence"]["performance"]["violations"],
                         [25])

    def test_performance_without_budget_is_measured_not_passed(self):
        # Issue 78: measurement without a threshold is not verification.
        result = pipeline.run_candidate(
            GOOD_CANDIDATE,
            tests={"performance": {"samples": [4, 5]}},
            worker_fn=CountingWorker(lambda code: ok_envelope()),
            risk_fn=risk_r0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["verdict"]["failed_stage"], "performance")
        self.assertTrue(result["evidence"]["performance"]["measured"])


if __name__ == "__main__":
    unittest.main()
