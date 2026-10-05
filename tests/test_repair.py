"""Tests for bounded repair mode + failure summaries (repair.py). Stdlib only."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import repair
from repair import (detect_oscillation, failure_signature, is_green,
                    repair_loop, summarize_failure)


def _fail(property="roundtrip(csv(rows)) == rows", counterexample=None,
          expected="2 columns / 1 row", actual="row split after x",
          callsite="escape-field", **extra):
    failure = {
        "property": property,
        "counterexample": counterexample if counterexample is not None
        else [["a", "x\ny"]],
        "expected": expected,
        "actual": actual,
        "callsite": callsite,
    }
    failure.update(extra)
    return {"passed": False, "evidence": {}, "failures": [failure]}


_PASS = {"passed": True, "evidence": {"tests": "12/12"}, "failures": []}


class SummarizeFailureTest(unittest.TestCase):
    def test_minimal_counterexample_shape(self):
        summary = summarize_failure(_fail())
        self.assertEqual(set(summary),
                         {"property", "counterexample", "expected", "actual",
                          "callsite"})
        self.assertEqual(summary["property"], "roundtrip(csv(rows)) == rows")
        self.assertEqual(summary["counterexample"], [["a", "x\ny"]])
        self.assertEqual(summary["expected"], "2 columns / 1 row")
        self.assertEqual(summary["actual"], "row split after x")
        self.assertEqual(summary["callsite"], "escape-field")

    def test_never_includes_full_logs(self):
        verdict = _fail(logs="x" * 100000, stdout="...", traceback="...",
                        transcript=["turn"] * 500)
        verdict["evidence"] = {"raw_log": "huge"}
        summary = summarize_failure(verdict)
        blob = repr(summary)
        self.assertNotIn("logs", blob)
        self.assertNotIn("stdout", blob)
        self.assertNotIn("traceback", blob)
        self.assertNotIn("transcript", blob)
        self.assertNotIn("raw_log", blob)

    def test_unknown_fields_become_none(self):
        summary = summarize_failure({"passed": False, "failures": [{}]})
        self.assertEqual(summary, {"property": None, "counterexample": None,
                                   "expected": None, "actual": None,
                                   "callsite": None})

    def test_tolerates_evaluator_verdict_shape(self):
        verdict = {
            "verdict": "fail",
            "evidence": {
                "cases": [
                    {"id": "csv-1", "passed": False,
                     "checks": [{"key": "csv-1:0", "passed": False,
                                 "error": "mismatch"}]},
                ],
            },
        }
        summary = summarize_failure(verdict)
        self.assertEqual(summary["property"], "csv-1")
        self.assertEqual(summary["actual"], "mismatch")


class IsGreenTest(unittest.TestCase):
    def test_accepts_pipeline_and_evaluator_shapes(self):
        self.assertTrue(is_green({"passed": True}))
        self.assertTrue(is_green({"verdict": "pass"}))
        self.assertTrue(is_green({"status": "green"}))
        self.assertFalse(is_green({"passed": False}))
        self.assertFalse(is_green({"verdict": "fail"}))
        self.assertFalse(is_green({}))
        self.assertFalse(is_green("pass"))


class RepairLoopTest(unittest.TestCase):
    def test_first_try_success_uses_no_repairs(self):
        calls = []

        def pipeline_fn(candidate, tests):
            calls.append(candidate)
            return dict(_PASS)

        generate_calls = []

        def generate_fn(candidate, summary):
            generate_calls.append(candidate)
            return candidate

        result = repair_loop("(v1)", ["t"], pipeline_fn=pipeline_fn,
                             generate_fn=generate_fn)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["candidate"], "(v1)")
        self.assertEqual(result["repairs_used"], 0)
        self.assertEqual(result["history"], [])
        self.assertEqual(calls, ["(v1)"])
        self.assertEqual(generate_calls, [])

    def test_succeeds_on_second_attempt_via_fakes(self):
        rehearsals = []

        def pipeline_fn(candidate, tests):
            rehearsals.append(candidate)
            self.assertEqual(tests, ["roundtrip"])
            if len(rehearsals) == 1:
                return _fail()
            return dict(_PASS)

        seen = {}

        def generate_fn(candidate, summary):
            seen["candidate"] = candidate
            seen["summary"] = summary
            self.assertEqual(summary["callsite"], "escape-field")
            return "(v2-fixed)"

        result = repair_loop("(v1)", ["roundtrip"], pipeline_fn=pipeline_fn,
                             generate_fn=generate_fn)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["candidate"], "(v2-fixed)")
        self.assertEqual(result["repairs_used"], 1)
        # Re-rehearsed from scratch: the pipeline saw both candidates.
        self.assertEqual(rehearsals, ["(v1)", "(v2-fixed)"])
        self.assertEqual(seen["candidate"], "(v1)")
        self.assertEqual(len(result["history"]), 1)

    def test_repair_budget_respected(self):
        counts = {"pipeline": 0, "generate": 0}

        def pipeline_fn(candidate, tests):
            counts["pipeline"] += 1
            # Distinct failures so oscillation never triggers.
            return _fail(property="prop-%d" % counts["pipeline"])

        def generate_fn(candidate, summary):
            counts["generate"] += 1
            return candidate + "-r"

        result = repair_loop("(v1)", [], pipeline_fn=pipeline_fn,
                             generate_fn=generate_fn, max_repairs=1)
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(result["reason"], "repair_budget_exhausted")
        self.assertEqual(result["repairs_used"], 1)
        self.assertEqual(counts["generate"], 1)
        self.assertEqual(counts["pipeline"], 2)

    def test_zero_budget_escalates_without_repair(self):
        def generate_fn(candidate, summary):  # pragma: no cover
            self.fail("must not attempt any repair")

        result = repair_loop("(v1)", [], pipeline_fn=lambda c, t: _fail(),
                             generate_fn=generate_fn, max_repairs=0)
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(result["reason"], "repair_budget_exhausted")
        self.assertEqual(result["repairs_used"], 0)

    def test_oscillation_freezes_instead_of_looping(self):
        counts = {"pipeline": 0, "generate": 0}

        def pipeline_fn(candidate, tests):
            counts["pipeline"] += 1
            return _fail()  # identical failure every time

        def generate_fn(candidate, summary):
            counts["generate"] += 1
            return candidate + "-r"

        result = repair_loop("(v1)", [], pipeline_fn=pipeline_fn,
                             generate_fn=generate_fn, max_repairs=5)
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(result["reason"], "oscillation")
        self.assertTrue(result["frozen"])
        # One repair attempted, then the repeat froze the loop — no
        # further repairs despite remaining budget.
        self.assertEqual(counts["generate"], 1)
        self.assertEqual(counts["pipeline"], 2)
        self.assertEqual(len(result["history"]), 1)

    def test_escalation_record_shape(self):
        result = repair_loop("(v1)", [], pipeline_fn=lambda c, t: _fail(),
                             generate_fn=lambda c, s: c + "-r",
                             max_repairs=1)
        self.assertEqual(set(result),
                         {"status", "reason", "repairs_used", "history",
                          "failure", "frozen"})
        self.assertEqual(result["status"], "escalated")
        self.assertTrue(result["frozen"])
        self.assertEqual(set(result["failure"]),
                         {"property", "counterexample", "expected", "actual",
                          "callsite"})
        for entry in result["history"]:
            self.assertEqual(set(entry), {"signature", "summary"})

    def test_rejects_negative_budget(self):
        with self.assertRaises(ValueError):
            repair_loop("(v1)", [], pipeline_fn=lambda c, t: _PASS,
                        generate_fn=lambda c, s: c, max_repairs=-1)


class OscillationHelperTest(unittest.TestCase):
    def test_signature_stable_and_detects_repeats(self):
        first = failure_signature(_fail())
        self.assertEqual(first, failure_signature(_fail()))
        self.assertNotEqual(first, failure_signature(_fail(actual="other")))
        history = [{"signature": first, "summary": {}}]
        self.assertTrue(detect_oscillation(history, first))
        self.assertFalse(detect_oscillation(history, "different"))
        self.assertFalse(detect_oscillation([], first))


if __name__ == "__main__":
    unittest.main()
