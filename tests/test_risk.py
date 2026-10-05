"""Offline tests for risk.py (mutation-risk classifier, plan.md section 7).

Stdlib unittest only. Candidates are built with s_expr.parse_candidate so the
tests exercise the real parser -> classifier path. No live API calls.
"""

import unittest

import risk
from s_expr import parse_candidate


def classify_text(text, context=None):
    return risk.classify(parse_candidate(text), context)


def levels_in(reasons):
    return {reason.split(":", 1)[0] for reason in reasons}


class TestLevels(unittest.TestCase):
    """One passing case per risk level R0-R6."""

    def test_r0_pure_computation(self):
        result = classify_text(
            "(candidate (:target rank-items) (:parent 0) "
            "(:definition (lambda (items) (mapcar score-item items))))")
        self.assertEqual(result["level"], "R0")
        self.assertEqual(result["gates"], [
            "isolated-compile", "unit-tests", "property-tests",
            "performance-checks"])
        self.assertTrue(result["reasons"])

    def test_r1_local_state(self):
        result = classify_text(
            "(candidate (:target cache-put) (:parent 3) "
            "(:definition (lambda (table key value) "
            "(setf (gethash key table) value))))")
        self.assertEqual(result["level"], "R1")
        for gate in ("state-diff-validation", "replay-testing",
                     "isolated-compile", "unit-tests"):
            self.assertIn(gate, result["gates"])

    def test_r2_shared_state(self):
        result = classify_text(
            "(candidate (:target update-user-record) (:parent 7) "
            "(:definition (lambda (id record) "
            "(setf (gethash id *user-records*) record))))")
        self.assertEqual(result["level"], "R2")
        for gate in ("forked-state", "transaction-simulation",
                     "invariant-checking"):
            self.assertIn(gate, result["gates"])

    def test_r3_schema_change(self):
        result = classify_text(
            "(candidate (:target customer-record-schema) (:parent 2) "
            '(:definition (defstruct customer-record (name "") (id 0))))')
        self.assertEqual(result["level"], "R3")
        self.assertIn("R3", levels_in(result["reasons"]))
        for gate in ("migration", "backwards-compatibility",
                     "contract-replay", "previous-version-coexistence"):
            self.assertIn(gate, result["gates"])

    def test_r3_runtime_redefinition(self):
        result = classify_text(
            "(candidate (:target hot-patch) (:parent 2) "
            "(:definition (lambda (name code) "
            "(setf (symbol-function name) code))))")
        self.assertEqual(result["level"], "R3")

    def test_r4_network_effect(self):
        result = classify_text(
            "(candidate (:target export-report) (:parent 5) "
            '(:definition (lambda (rows) (http-post '
            '"https://api.example.invalid/x" rows))))')
        self.assertEqual(result["level"], "R4")
        for gate in ("effect-virtualization", "approval-policy",
                     "shadow-or-dry-run-evaluation"):
            self.assertIn(gate, result["gates"])

    def test_r4_filesystem_effect(self):
        result = classify_text(
            "(candidate (:target export-report) (:parent 5) "
            "(:definition (lambda (rows path) (with-open-file "
            "(out path :direction :output) (write-rows rows out)))))")
        self.assertEqual(result["level"], "R4")

    def test_r5_policy_target(self):
        result = classify_text(
            "(candidate (:target retrieval-ranker) (:parent 9) "
            "(:definition (lambda (items) (reverse items))))")
        self.assertEqual(result["level"], "R5")
        for gate in ("offline-benchmark", "control-group",
                     "paired-evaluation", "hidden-test-suite",
                     "statistically-meaningful-result"):
            self.assertIn(gate, result["gates"])

    def test_r5_prompt_target(self):
        result = classify_text(
            "(candidate (:target prompt) (:parent 9) "
            "(:definition (lambda (items) (reverse items))))")
        self.assertEqual(result["level"], "R5")

    def test_r6_evaluator_target(self):
        result = classify_text(
            "(candidate (:target evaluator-harness) (:parent 1) "
            "(:definition (lambda (x) (+ x 1))))")
        self.assertEqual(result["level"], "R6")
        self.assertEqual(result["gates"],
                         ["promotion-forbidden", "external-release-only"])


class TestKernelMutationAttempt(unittest.TestCase):
    def test_promotion_authority_is_forbidden(self):
        result = classify_text(
            "(candidate (:target promotion-authority) (:parent 1) "
            "(:definition (lambda (candidate) (promote candidate))))")
        self.assertEqual(result["level"], "R6")
        self.assertEqual(result["gates"],
                         ["promotion-forbidden", "external-release-only"])
        self.assertTrue(any("promot" in reason
                            for reason in result["reasons"]))

    def test_credential_broker_reference_is_forbidden(self):
        result = classify_text(
            "(candidate (:target rotate-keys) (:parent 1) "
            "(:definition (lambda () (credential-broker-reset))))")
        self.assertEqual(result["level"], "R6")


class TestMultiSignal(unittest.TestCase):
    def test_highest_level_wins_local_plus_network(self):
        result = classify_text(
            "(candidate (:target notify-cache) (:parent 4) "
            "(:definition (lambda (table key message) (progn "
            "(setf (gethash key table) message) "
            '(send-email "ops@example.invalid" message)))))')
        self.assertEqual(result["level"], "R4")
        cited = levels_in(result["reasons"])
        self.assertIn("R1", cited)
        self.assertIn("R4", cited)

    def test_highest_level_wins_effect_plus_policy(self):
        result = classify_text(
            "(candidate (:target context-compiler-notify) (:parent 4) "
            '(:definition (lambda (message) (send-email '
            '"ops@example.invalid" message))))')
        self.assertEqual(result["level"], "R5")
        cited = levels_in(result["reasons"])
        self.assertIn("R4", cited)
        self.assertIn("R5", cited)


class TestUnknownFormConservatism(unittest.TestCase):
    """Unknown forms resolve to at least R4; garbage input is refused.

    Policy (documented in risk.py): dynamic/introspective heads, malformed
    heads, foreign node types, and unknown declared-effect names are R4
    with an explicit "unclassifiable" reason. Structurally invalid input
    raises instead of returning a level.
    """

    def test_dynamic_dispatch_is_r4(self):
        result = classify_text(
            "(candidate (:target apply-helper) (:parent 0) "
            "(:definition (lambda (f x) (funcall f x))))")
        self.assertEqual(result["level"], "R4")
        self.assertTrue(any("unclassifiable" in reason
                            for reason in result["reasons"]))

    def test_eval_is_r4(self):
        result = classify_text(
            "(candidate (:target run-snippet) (:parent 0) "
            "(:definition (lambda (code) (eval code))))")
        self.assertGreaterEqual(risk.LEVELS.index(result["level"]),
                                risk.LEVELS.index("R4"))

    def test_unknown_declared_effect_is_r4(self):
        candidate = parse_candidate(
            "(candidate (:target rank-items) (:parent 0) "
            "(:definition (lambda (items) (mapcar score-item items))))")
        result = risk.classify(candidate, {"effects": ["teleport"]})
        self.assertEqual(result["level"], "R4")
        self.assertTrue(any("teleport" in reason and "unclassifiable" in reason
                            for reason in result["reasons"]))

    def test_malformed_form_head_is_r4(self):
        result = risk.classify({"target": "weird", "definition": [[1, 2]]})
        self.assertEqual(result["level"], "R4")
        self.assertTrue(any("unclassifiable" in reason
                            for reason in result["reasons"]))

    def test_garbage_input_is_refused(self):
        with self.assertRaises(TypeError):
            risk.classify("not-a-candidate")
        with self.assertRaises(ValueError):
            risk.classify({})
        with self.assertRaises(ValueError):
            risk.classify({"target": "x", "definition": []})
        with self.assertRaises(ValueError):
            risk.classify({"target": "x", "definition": "oops"})
        with self.assertRaises(ValueError):
            risk.classify({"target": 42, "definition": ["nil"]})


class TestDeclaredEffects(unittest.TestCase):
    PURE = ("(candidate (:target rank-items) (:parent 0) "
            "(:definition (lambda (items) (mapcar score-item items))))")

    def test_extra_effects_network(self):
        candidate = parse_candidate(
            "(candidate (:target rank-items) (:parent 0) "
            "(:definition (lambda (items) items)) "
            "(:effects (:network-write)))")
        self.assertEqual(risk.classify(candidate)["level"], "R4")

    def test_extra_effects_state(self):
        candidate = parse_candidate(
            "(candidate (:target rank-items) (:parent 0) "
            "(:definition (lambda (items) items)) "
            "(:effects (:state-write)))")
        self.assertEqual(risk.classify(candidate)["level"], "R2")

    def test_context_effects(self):
        pure = parse_candidate(self.PURE)
        self.assertEqual(
            risk.classify(pure, {"effects": ["email"]})["level"], "R4")
        self.assertEqual(
            risk.classify(pure, {"effects": ["pure"]})["level"], "R0")
        self.assertEqual(
            risk.classify(pure, {"effects": ["clock"]})["level"], "R1")
        self.assertEqual(
            risk.classify(pure, {"effects": ["queue-write"]})["level"], "R2")


class TestHygiene(unittest.TestCase):
    def test_string_contents_not_scanned(self):
        result = classify_text(
            "(candidate (:target label-rows) (:parent 0) "
            '(:definition (lambda (row) (concatenate (quote string) row '
            '"network email http kernel evaluator"))))')
        self.assertEqual(result["level"], "R0")

    def test_quoted_code_not_scanned(self):
        result = classify_text(
            "(candidate (:target default-rules) (:parent 0) "
            '(:definition (lambda () (quote (eval (delete-file "x"))))))')
        self.assertEqual(result["level"], "R0")

    def test_console_prompt_is_not_policy(self):
        result = classify_text(
            "(candidate (:target greet) (:parent 0) "
            "(:definition (lambda (name) (prompt-user name))))")
        self.assertEqual(result["level"], "R1")
        named = classify_text(
            "(candidate (:target prompt-user) (:parent 0) "
            "(:definition (lambda (name) (prompt-user name))))")
        self.assertEqual(named["level"], "R1")

    def test_user_interface_is_display_code(self):
        result = classify_text(
            "(candidate (:target render-user-interface) (:parent 0) "
            "(:definition (lambda (model) (render-user-interface model))))")
        self.assertEqual(result["level"], "R0")

    def test_process_verb_is_not_process_effect(self):
        result = classify_text(
            "(candidate (:target process-rows) (:parent 0) "
            "(:definition (lambda (rows) (mapcar score-item rows))))")
        self.assertEqual(result["level"], "R0")

    def test_os_process_signals_are_r4(self):
        result = classify_text(
            "(candidate (:target run-worker) (:parent 0) "
            "(:definition (lambda (cmd) (sb-ext:run-program cmd))))")
        self.assertEqual(result["level"], "R4")

    def test_deterministic_and_isolated_results(self):
        text = ("(candidate (:target notify-cache) (:parent 4) "
                "(:definition (lambda (table key message) (progn "
                "(setf (gethash key table) message) "
                '(send-email "ops@example.invalid" message)))))')
        first = classify_text(text)
        second = classify_text(text)
        self.assertEqual(first, second)
        first["gates"].append("mutated")
        first["reasons"].append("mutated")
        third = classify_text(text)
        self.assertEqual(second, third)

    def test_result_shape(self):
        result = classify_text(
            "(candidate (:target rank-items) (:parent 0) "
            "(:definition (lambda (items) items)))")
        self.assertEqual(set(result), {"level", "reasons", "gates"})
        self.assertIn(result["level"], risk.LEVELS)
        self.assertTrue(result["reasons"])
        self.assertTrue(result["gates"])
        self.assertTrue(all(isinstance(reason, str)
                            for reason in result["reasons"]))

    def test_gates_cumulative_through_r5(self):
        chain = [risk.GATES[level]
                 for level in ("R0", "R1", "R2", "R3", "R4", "R5")]
        for lower, higher in zip(chain, chain[1:]):
            self.assertLess(set(lower), set(higher))
        self.assertEqual(risk.GATES["R6"],
                         ["promotion-forbidden", "external-release-only"])


if __name__ == "__main__":
    unittest.main()
