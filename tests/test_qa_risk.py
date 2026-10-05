"""QA seam tests: risk.py tricky candidates (incl. R5/R6 edges).

Proves QA-07 (dangerous primitives in value position read R0),
QA-08 (mutation target naming a core dynamic form reads R0), QA-11
(deep definition escapes the documented exception contract).
Guards pin the fail-safe edges: cl:quote over-flagging, prompt-user
exemption, package-qualified heads, R5/R6 target signals, deliberate
R0s (symbol-function reads, user-interface masking).
"""

import unittest

import risk
import s_expr


def _classify(definition_text, target="do-it"):
    return risk.classify({"definition": s_expr.parse(definition_text),
                          "target": target})


class RiskBlindSpotTest(unittest.TestCase):
    def test_dangerous_primitive_as_function_value_flags_r4(self):
        """QA-07: `(mapcar open xs)` opens files but classifies R0.

        Only head position is checked against the dangerous-head
        tables, so a file opener passed as a higher-order value is
        "pure computation". Suggested fix: scan value-position atoms
        against the opener/dynamic tables (at least open/load/exec).
        """
        self.assertEqual(_classify("(mapcar open xs)")["level"], "R4")

    def test_target_naming_core_dynamic_form_is_not_pure(self):
        """QA-08: redefining `eval` classifies R0.

        A candidate whose target IS eval/open/load redefines a core
        dynamic form, yet the target channel has no primitive table and
        reports "pure computation". Suggested fix: flag targets naming
        R4-dynamic heads (level R3/R4 TBD by spec).
        """
        self.assertNotEqual(_classify("(defun eval (x) x)",
                                      target="eval")["level"], "R0")

    def test_deep_definition_raises_documented_error(self):
        """QA-11: hostile nesting escapes as RecursionError.

        The contract promises TypeError/ValueError, but _walk recurses
        without a depth guard, so a programmatically built 3000-deep
        definition raises RecursionError. (Parse-built input is capped
        at depth 200 by s_expr; direct callers are not.)
        """
        deep = ["f"]
        for _ in range(3000):
            deep = ["f", deep]
        with self.assertRaises(ValueError):
            risk.classify({"definition": deep, "target": "t"})


class RiskEdgeGuardTest(unittest.TestCase):
    def test_cl_quote_overflags_fail_safe(self):
        """Guard: `(cl:quote (eval x))` flags R4 (fail-safe FP)."""
        got = _classify("(cl:quote (eval x))")
        self.assertEqual(got["level"], "R4")

    def test_prompt_user_target_stays_r1(self):
        """Guard: prompt_user target is console I/O, not R5 policy."""
        got = _classify("(foo 1)", target="prompt_user")
        self.assertEqual(got["level"], "R1")

    def test_bare_prompt_target_is_r5(self):
        """Guard: target naming the prompter is agent policy (R5)."""
        got = _classify("(foo 1)", target="prompt")
        self.assertEqual(got["level"], "R5")

    def test_package_qualified_external_head_flags_r4(self):
        """Guard: sb-ext:run-program head is an external effect."""
        self.assertEqual(_classify("(sb-ext:run-program x)")["level"], "R4")

    def test_ledger_target_is_r6(self):
        """Guard: ledger-entry target hits the trust root (R6)."""
        self.assertEqual(_classify("(foo 1)",
                                   target="ledger-entry")["level"], "R6")

    def test_hidden_test_symbol_is_r6(self):
        """Guard: hidden-test reference is trust-root (R6)."""
        self.assertEqual(
            _classify("(check hidden-test-results)")["level"], "R6")

    def test_retrieval_strategy_symbol_is_r5(self):
        """Guard: retrieval-strategy reference is agent policy (R5)."""
        self.assertEqual(
            _classify("(tune retrieval-strategy)")["level"], "R5")

    def test_user_interface_masking_avoids_r2_r3(self):
        """Guard: user-interface is display code, not records/contracts."""
        got = _classify("(render user-interface)")
        self.assertNotIn(got["level"], ("R2", "R3", "R4", "R5", "R6"))

    def test_symbol_function_read_stays_r0_by_design(self):
        """Guard: bare introspection reads are deliberately R0."""
        self.assertEqual(_classify("(symbol-function f)")["level"], "R0")

    def test_string_contents_never_scanned(self):
        """Guard: "eval"/"kernel" inside string literals stay R0."""
        got = _classify('(format nil "eval kernel ledger")')
        self.assertEqual(got["level"], "R1")  # format alone is console I/O


if __name__ == "__main__":
    unittest.main()
