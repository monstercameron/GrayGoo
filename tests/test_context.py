"""Tests for the minimal context compiler (context.py). Stdlib only."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import context
from context import compile_context, estimate_tokens


def make_goal(**overrides):
    goal = {
        "text": "Fix CSV export for embedded newlines.",
        "task": "Return one candidate replacement.",
        "contracts": ["Output must parse to same row/column values."],
        "callers": ["EXPORT-CUSTOMERS", "EXPORT-ORDERS"],
        "constraints": ["Do not alter public WRITE-CSV interface."],
    }
    goal.update(overrides)
    return goal


def make_retrieved():
    return [
        {"id": "WRITE-CSV", "version": 12,
         "intent": "write records as CSV text",
         "input_types": ["record"], "output_types": ["csv-text"],
         "effects": ["filesystem-write"]},
        {"id": "ESCAPE-CSV-FIELD", "version": 4,
         "intent": "escape embedded newlines in CSV fields",
         "input_types": ["csv-text"], "output_types": ["csv-text"]},
    ]


class CompileContextTest(unittest.TestCase):
    def test_full_context_contains_plan_sections(self):
        result = compile_context(
            make_goal(),
            make_retrieved(),
            failures=["Embedded LF produces invalid row boundary."],
            effects=["filesystem-write:/exports"],
            budgets={"max_tokens": 2000},
        )
        text = result["text"]
        for section in ("GOAL", "RELEVANT CAPABILITY", "CONTRACT",
                        "CURRENT FAILURE", "CALLERS", "ALLOWED EFFECTS",
                        "CONSTRAINT", "TASK"):
            self.assertIn(section, text)
        self.assertEqual(result["dropped"], [])
        self.assertLessEqual(result["tokens"], 2000)
        self.assertEqual(result["tokens"], estimate_tokens(text))

    def test_budget_drops_lowest_priority_first(self):
        full = compile_context(
            make_goal(), make_retrieved(),
            failures=["Embedded LF produces invalid row boundary."],
            effects=["filesystem-write:/exports"],
            budgets={"max_tokens": 2000})
        tiny_budget = 30
        self.assertGreater(full["tokens"], tiny_budget)
        result = compile_context(
            make_goal(), make_retrieved(),
            failures=["Embedded LF produces invalid row boundary."],
            effects=["filesystem-write:/exports"],
            budgets={"max_tokens": tiny_budget})
        # Hard cap holds.
        self.assertLessEqual(result["tokens"], tiny_budget)
        # Lowest-priority sections dropped before higher ones.
        self.assertIn("CALLERS", result["dropped"])
        # GOAL and TASK are pinned and survive.
        self.assertIn("GOAL", result["sections"])
        self.assertIn("TASK", result["sections"])
        self.assertIn("GOAL", result["text"])
        self.assertIn("TASK", result["text"])

    def test_capabilities_trimmed_before_whole_sections_drop(self):
        many = [{"id": "CAP-%d" % i, "version": 1,
                 "intent": "capability number %d for testing budgets" % i,
                 "input_types": ["a"], "output_types": ["b"]}
                for i in range(10)]
        loose = compile_context(
            make_goal(), many, failures=[], effects=["none"],
            budgets={"max_tokens": 2000})
        # Tighten just below the loose cost: capabilities should shrink
        # while every other section survives.
        tight = compile_context(
            make_goal(), many, failures=[], effects=["none"],
            budgets={"max_tokens": loose["tokens"] - 5})
        self.assertLessEqual(tight["tokens"], loose["tokens"] - 5)
        self.assertEqual(tight["dropped"], [])
        self.assertIn("RELEVANT CAPABILITY", tight["sections"])
        kept = [line for line in tight["text"].split("\n")
                if line.startswith("CAP-")]
        self.assertLess(len(kept), 10)
        self.assertGreater(len(kept), 0)

    def test_never_includes_full_history(self):
        failures = ["failure one details here",
                    "failure two details here",
                    "current failure three details here"]
        result = compile_context(
            make_goal(), [], failures=failures, effects=[],
            budgets={"max_tokens": 2000})
        self.assertIn("current failure three", result["text"])
        self.assertNotIn("failure one", result["text"])
        self.assertNotIn("failure two", result["text"])

    def test_empty_inputs_yield_minimal_context(self):
        result = compile_context(
            {"text": "Do something.", "task": "Do it."},
            [], [], [], budgets={"max_tokens": 2000})
        self.assertIn("GOAL", result["text"])
        self.assertIn("TASK", result["text"])
        self.assertNotIn("RELEVANT CAPABILITY", result["text"])
        self.assertEqual(result["dropped"], [])

    def test_max_capabilities_cap(self):
        result = compile_context(
            make_goal(), make_retrieved(), failures=[], effects=[],
            budgets={"max_tokens": 2000, "max_capabilities": 1})
        kept = [line for line in result["text"].split("\n")
                if "@" in line and ("WRITE-CSV" in line or "ESCAPE" in line)]
        self.assertEqual(len(kept), 1)

    def test_estimate_tokens_is_word_based(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("   "), 0)
        self.assertEqual(estimate_tokens("GOAL\nFix CSV export."), 4)


if __name__ == "__main__":
    unittest.main()
