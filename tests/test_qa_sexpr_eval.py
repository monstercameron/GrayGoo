"""QA seam tests: s_expr.py adversarial inputs + evaluator edges.

All guards (current behavior verified): nesting/size caps at the exact
boundary, quote-sugar edges, schema rejections, unicode/odd atoms
(incl. number-lexer quirks documented for the QA report), evaluator
NaN-threshold rejection and non-string-output tolerance.
"""

import unittest

import s_expr
from evaluator import protocol, service


class SExprAdversarialTest(unittest.TestCase):
    def test_nesting_at_cap_parses(self):
        """Guard: exactly MAX_DEPTH (200) levels is accepted."""
        s_expr.parse("(" * 200 + "x" + ")" * 200)

    def test_nesting_over_cap_rejected(self):
        """Guard: 201 levels raises, naming the cap."""
        with self.assertRaises(s_expr.SExprSyntaxError) as caught:
            s_expr.parse("(" * 201 + "x" + ")" * 201)
        self.assertIn("200", str(caught.exception))

    def test_input_at_size_cap_checked(self):
        """Guard: exactly 100000 chars passes the size gate."""
        try:
            s_expr.parse(";" + "x" * 99999)
        except s_expr.SExprSyntaxError as exc:
            # Comment-only input holds no form; the size gate itself
            # must not be what rejects it.
            self.assertNotIn("too large", str(exc))
        else:
            self.fail("comment-only input unexpectedly parsed a form")

    def test_input_over_size_cap_rejected(self):
        """Guard: 100001 chars raises before any lexing."""
        with self.assertRaises(s_expr.SExprSyntaxError) as caught:
            s_expr.tokenize("x" * 100001)
        self.assertIn("too large", str(caught.exception))

    def test_unterminated_string_rejected(self):
        with self.assertRaises(s_expr.SExprSyntaxError):
            s_expr.parse('(candidate (:target t) (:parent 0) "abc')

    def test_dangling_quote_rejected(self):
        with self.assertRaises(s_expr.SExprSyntaxError):
            s_expr.parse("(candidate (:target t) (:parent 0) '")

    def test_quote_without_form_rejected(self):
        with self.assertRaises(s_expr.SExprSyntaxError):
            s_expr.parse("(')")

    def test_nested_quote_sugar_parses(self):
        self.assertEqual(s_expr.parse("''x"), ["quote", ["quote", "x"]])

    def test_trailing_content_rejected(self):
        with self.assertRaises(s_expr.SExprSyntaxError):
            s_expr.parse("(a) (b)")

    def test_non_string_input_rejected(self):
        with self.assertRaises(s_expr.SExprSyntaxError):
            s_expr.tokenize(123)
        with self.assertRaises(s_expr.SExprSyntaxError):
            s_expr.parse(None)

    def test_duplicate_candidate_key_rejected(self):
        with self.assertRaises(s_expr.SExprSchemaError):
            s_expr.parse_candidate(
                "(candidate (:target t) (:parent 0) (:parent 1) "
                "(:definition (f)))")

    def test_bool_parent_rejected(self):
        """Guard: :parent t/nil or bools are not integers."""
        with self.assertRaises(s_expr.SExprSchemaError):
            s_expr.parse_candidate(
                "(candidate (:target t) (:parent t) (:definition (f)))")

    def test_unicode_symbol_round_trip(self):
        """Guard: emoji/CJK/RTL symbols survive as opaque atoms."""
        form = s_expr.parse_candidate(
            "(candidate (:target t) (:parent 0) "
            '(:definition (greet "\U0001F389\u4e2d\u05e9\u200d")) '
            "(:reason \U0001F389))")
        self.assertEqual(form["reason"], "\U0001F389")

    def test_number_lexer_accepts_python_only_spellings(self):
        """Guard (quirk): lexer uses int()/float(), CL disagrees.

        "1_000", Arabic-Indic digits, and "1e999"->inf all parse as
        numbers though the Common Lisp reader would not read them as
        such. Documented here; tracked as a QA-report low/info item.
        """
        self.assertEqual(s_expr.tokenize("1_000")[0].value, 1000)
        self.assertEqual(s_expr.tokenize("\u0661\u0662\u0663")[0].value, 123)
        self.assertEqual(s_expr.tokenize("1e999")[0].value, float("inf"))


class EvaluatorEdgeTest(unittest.TestCase):
    def test_nan_threshold_rejected(self):
        """Guard: NaN (valid JSON number) fails the range check."""
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(
                {"request_id": "r", "candidate_id": "c", "outputs": {},
                 "thresholds": {"min_case_pass_rate": float("nan")}})

    def test_non_string_outputs_fail_check_without_crash(self):
        """Guard: wrong-typed outputs fail loudly, never raise."""
        result = service.evaluate(
            "c", {"H:0": 123, "zzz": None},
            cases=[{"id": "H", "checks": [{"input": "i", "expected": "e",
                                           "compare": "exact"}]}])
        self.assertEqual(result["verdict"], "fail")
        checks = result["evidence"]["cases"][0]["checks"]
        self.assertEqual(checks[0]["error"], "non_string_output")
        self.assertEqual(result["evidence"]["unexpected_outputs"], ["zzz"])

    def test_default_corpus_loads_non_empty(self):
        """Guard: the shipped hidden corpus is a real gate."""
        self.assertTrue(service.load_hidden_cases())


if __name__ == "__main__":
    unittest.main()
