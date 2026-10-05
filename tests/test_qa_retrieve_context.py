"""QA seam tests: retrieve.py / context.py budget edge cases.

Proves the max_depth=0 direct-hit inconsistency (QA report low).
Guards pin empty/zero/negative/huge budget behavior: k edges, empty
goals, token-budget validation, hard-cap guarantee, capability trim,
last-failure-only rendering.
"""

import unittest

import context as ctx
import retrieve
from retrieve import Capability, CapabilityIndex


def _index():
    return CapabilityIndex([
        Capability("csv-parse", intent="parse csv rows into records",
                   input_types=["csv-text"], output_types=["record"],
                   effects=[], family="parsing"),
        Capability("json-parse", intent="parse json text into records",
                   input_types=["json-text"], output_types=["record"],
                   effects=[], family="parsing"),
    ])


class RetrieveBudgetEdgeTest(unittest.TestCase):
    @unittest.expectedFailure  # KNOWN-FAIL (low)
    def test_compose_plan_max_depth_zero_returns_none(self):
        """max_depth=0 still returns a direct (length-1) hit.

        The BFS honors max_depth but the direct-hit shortcut above it
        does not, so "no steps allowed" returns a one-step chain.
        Suggested fix: skip the direct hit when max_depth < 1.
        """
        plan = retrieve.compose_plan(
            {"input_types": ["csv-text"], "output_types": ["record"]},
            _index(), max_depth=0)
        self.assertIsNone(plan)

    def test_k_zero_and_negative_yield_empty(self):
        """Guard: non-positive k returns [] instead of erroring."""
        self.assertEqual(_index().find_capabilities("parse csv", k=0), [])
        self.assertEqual(_index().find_capabilities("parse csv", k=-5), [])

    def test_huge_k_returns_all_matches(self):
        """Guard: oversized k is clamped by the match count."""
        found = _index().find_capabilities("parse into records", k=10 ** 6)
        self.assertTrue(0 < len(found) <= 2)

    def test_none_goal_ranks_pure_caps_by_effect_bonus(self):
        """Guard: pure capabilities match even an empty goal (by design).

        With no goal signals, pure caps still score the fits-anywhere
        effect bonus; hard filters (family/effects) still exclude.
        """
        found = _index().find_capabilities(None, k=8)
        self.assertEqual(len(found), 2)
        strict = CapabilityIndex([
            Capability("net", intent="fetch remote bytes",
                       input_types=["url"], output_types=["bytes"],
                       effects=["network-write"], family="net")])
        self.assertEqual(strict.find_capabilities(
            None, constraints={"family": "nope"}), [])


class ContextBudgetEdgeTest(unittest.TestCase):
    def test_zero_and_negative_budget_rejected(self):
        """Guard: max_tokens must be positive."""
        with self.assertRaises(ValueError):
            ctx.compile_context("goal", [], None, None, {"max_tokens": 0})
        with self.assertRaises(ValueError):
            ctx.compile_context("goal", [], None, None, {"max_tokens": -3})

    def test_empty_goal_renders_empty(self):
        """Guard: no goal/task content yields empty text, 0 tokens."""
        out = ctx.compile_context({}, [], None, None)
        self.assertEqual(out["text"], "")
        self.assertEqual(out["tokens"], 0)

    def test_hard_cap_holds_at_tiny_budget(self):
        """Guard: tokens never exceed max_tokens, however small."""
        goal = {"text": "word " * 500, "task": "task " * 500,
                "contracts": ["c"] * 50, "callers": ["x"] * 50,
                "constraints": ["y"] * 50}
        out = ctx.compile_context(goal, [{"id": "c", "intent": "i"}],
                                  ["fail"], ["e"], {"max_tokens": 7})
        self.assertLessEqual(out["tokens"], 7)
        self.assertEqual(out["budget"], 7)

    def test_only_last_failure_rendered(self):
        """Guard: full failure history is never included."""
        out = ctx.compile_context(
            "goal", [], ["first failure", "second failure"], None)
        self.assertIn("second failure", out["text"])
        self.assertNotIn("first failure", out["text"])

    def test_max_capabilities_trims_lines(self):
        """Guard: max_capabilities caps lines even when budget allows."""
        caps = [{"id": "c%d" % i, "intent": "does thing %d" % i}
                for i in range(10)]
        out = ctx.compile_context("goal", caps, None, None,
                                  {"max_tokens": 100000,
                                   "max_capabilities": 2})
        body = out["text"].split("RELEVANT CAPABILITY\n")[1].split("\n\n")[0]
        self.assertEqual(len(body.split("\n")), 2)


if __name__ == "__main__":
    unittest.main()
