"""Offline tests for s_expr.py (and the cerebras_client validation gate).

Stdlib unittest only. The cerebras_client tests use mocks or offline
validation — no live API calls here.
"""

import unittest
from unittest import mock

import s_expr
from s_expr import (
    MAX_INPUT_CHARS,
    SExprSchemaError,
    SExprSyntaxError,
    parse,
    parse_candidate,
)

try:
    import cerebras_client
except ImportError:  # pragma: no cover - offline env without client deps
    cerebras_client = None

VALID_FULL = """\
(candidate
  (:target write-csv)
  (:parent 12)
  (:reason embedded-newline-handling)
  (:claims
    (preserves-interface t)
    (preserves-roundtrip t))
  (:definition
    (lambda (rows stream)
      (write rows :stream stream))))\
"""

VALID_MINIMAL = "(candidate (:target foo) (:parent 0) (:definition (nil)))"


class TestParseValid(unittest.TestCase):
    def test_plan_grammar_example(self):
        cand = parse_candidate(VALID_FULL)
        self.assertEqual(cand["target"], "write-csv")
        self.assertEqual(cand["parent"], 12)
        self.assertEqual(cand["reason"], "embedded-newline-handling")
        self.assertEqual(
            cand["claims"],
            [["preserves-interface", "t"], ["preserves-roundtrip", "t"]],
        )
        self.assertEqual(cand["definition"][0], "lambda")
        self.assertEqual(cand["extra"], {})

    def test_minimal_candidate(self):
        cand = parse_candidate(VALID_MINIMAL)
        self.assertEqual(cand["target"], "foo")
        self.assertEqual(cand["parent"], 0)
        self.assertEqual(cand["definition"], ["nil"])
        self.assertIsNone(cand["reason"])
        self.assertEqual(cand["claims"], [])

    def test_strings_and_comments_ignored(self):
        text = """\
; leading comment with ( unbalanced parens
(candidate ; trailing comment
  (:target write-csv) ; ( ) "quoted?"
  (:parent 12)
  (:definition (format nil "a)b(c;not-a-comment\\"q\\""))) ; tail
"""
        cand = parse_candidate(text)
        self.assertEqual(cand["target"], "write-csv")
        self.assertEqual(cand["parent"], 12)
        self.assertEqual(
            cand["definition"], ["format", "nil", 'a)b(c;not-a-comment"q"']
        )

    def test_string_is_not_a_symbol(self):
        got = parse('("write-csv" write-csv)')
        self.assertIsInstance(got[0], s_expr.SString)
        self.assertIs(type(got[1]), str)

    def test_numbers_and_negative_parent(self):
        cand = parse_candidate(
            "(candidate (:target f) (:parent -3) "
            "(:definition (list 1 -2 3.5)))"
        )
        self.assertEqual(cand["parent"], -3)
        self.assertEqual(cand["definition"], ["list", 1, -2, 3.5])

    def test_quote_sugar(self):
        self.assertEqual(parse("'a"), ["quote", "a"])
        self.assertEqual(parse("(f 'a)"), ["f", ["quote", "a"]])

    def test_unknown_keys_pass_through_as_extra(self):
        cand = parse_candidate(
            "(candidate (:target f) (:parent 1) "
            "(:definition (f)) (:risk R0))"
        )
        self.assertEqual(cand["extra"], {":risk": ["R0"]})


class TestParseRejected(unittest.TestCase):
    def test_unbalanced_open(self):
        with self.assertRaises(SExprSyntaxError):
            parse("(candidate (:target f)")

    def test_unbalanced_close(self):
        with self.assertRaises(SExprSyntaxError):
            parse("(candidate (:target f)))")

    def test_truncated(self):
        with self.assertRaises(SExprSyntaxError):
            parse_candidate("(candidate (:target write-csv) (:parent 12)")

    def test_garbage(self):
        for bad in ("hello world", "(((", ")))", "(a b", "a b)", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(s_expr.SExprError):
                    parse(bad)

    def test_whitespace_only(self):
        with self.assertRaises(SExprSyntaxError):
            parse("  \n\t  ")

    def test_trailing_content_rejected(self):
        with self.assertRaises(SExprSyntaxError):
            parse(VALID_MINIMAL + " " + VALID_MINIMAL)
        with self.assertRaises(SExprSyntaxError):
            parse(VALID_MINIMAL + " trailing-prose")

    def test_unterminated_string(self):
        with self.assertRaises(SExprSyntaxError):
            parse('(candidate (:target "oops)')

    def test_dangling_quote(self):
        with self.assertRaises(SExprSyntaxError):
            parse("(f ')")


class TestSchemaRejected(unittest.TestCase):
    def test_missing_target(self):
        with self.assertRaises(SExprSchemaError):
            parse_candidate("(candidate (:parent 1) (:definition (f)))")

    def test_missing_parent(self):
        with self.assertRaises(SExprSchemaError):
            parse_candidate("(candidate (:target f) (:definition (f)))")

    def test_missing_definition(self):
        with self.assertRaises(SExprSchemaError):
            parse_candidate("(candidate (:target f) (:parent 1))")

    def test_wrong_head(self):
        with self.assertRaises(SExprSchemaError):
            parse_candidate(
                "(not-a-candidate (:target f) (:parent 1) (:definition (f)))"
            )

    def test_top_level_must_be_list(self):
        with self.assertRaises(SExprSchemaError):
            parse_candidate("candidate")

    def test_target_must_be_symbol(self):
        for bad_target in ('"f"', "42", ":f", "(f)"):
            with self.subTest(bad_target=bad_target):
                with self.assertRaises(SExprSchemaError):
                    parse_candidate(
                        "(candidate (:target " + bad_target + ") "
                        "(:parent 1) (:definition (f)))"
                    )

    def test_parent_must_be_int(self):
        for bad_parent in ('"1"', "1.5", ":x", "(1)", "sym"):
            with self.subTest(bad_parent=bad_parent):
                with self.assertRaises(SExprSchemaError):
                    parse_candidate(
                        "(candidate (:target f) (:parent " + bad_parent + ") "
                        "(:definition (f)))"
                    )

    def test_definition_must_be_nonempty_list(self):
        for bad_defn in ("f", '"f"', "42", "()"):
            with self.subTest(bad_defn=bad_defn):
                with self.assertRaises(SExprSchemaError):
                    parse_candidate(
                        "(candidate (:target f) (:parent 1) "
                        "(:definition " + bad_defn + "))"
                    )

    def test_entry_must_be_keyword_list(self):
        with self.assertRaises(SExprSchemaError):
            parse_candidate(
                "(candidate (:target f) (:parent 1) "
                "(:definition (f)) (oops 1))"
            )

    def test_duplicate_key(self):
        with self.assertRaises(SExprSchemaError):
            parse_candidate(
                "(candidate (:target f) (:target g) (:parent 1) "
                "(:definition (f)))"
            )

    def test_schema_error_names_key(self):
        with self.assertRaises(SExprSchemaError) as ctx:
            parse_candidate("(candidate (:target f) (:parent 1))")
        self.assertEqual(ctx.exception.key, ":definition")


class TestAdversarial(unittest.TestCase):
    def test_oversized_input_rejected(self):
        big = "(" + "a " * (MAX_INPUT_CHARS // 2 + 100) + ")"
        self.assertGreater(len(big), MAX_INPUT_CHARS)
        with self.assertRaises(SExprSyntaxError):
            parse(big)

    def test_deep_nesting_rejected(self):
        deep = "(" * (s_expr.MAX_DEPTH + 10) + ")" * (s_expr.MAX_DEPTH + 10)
        with self.assertRaises(SExprSyntaxError):
            parse(deep)

    def test_many_parens_but_balanced_ok(self):
        wide = "(candidate (:target f) (:parent 1) (:definition (f " + "x " * 5000 + ")))"
        cand = parse_candidate(wide)
        self.assertEqual(len(cand["definition"]), 5001)


@unittest.skipUnless(
    cerebras_client is not None, "cerebras_client deps not installed"
)
class TestClientValidationGate(unittest.TestCase):
    def test_validate_accepts_valid_candidate(self):
        cand = cerebras_client.validate_model_output(VALID_FULL)
        self.assertEqual(cand["target"], "write-csv")
        self.assertEqual(cand["parent"], 12)

    def test_validate_rejects_malformed_with_structured_failure(self):
        with self.assertRaises(
            cerebras_client.MalformedCandidateError
        ) as ctx:
            cerebras_client.validate_model_output("(candidate (:target f)")
        failure = ctx.exception.failure
        self.assertFalse(failure["ok"])
        self.assertIn("error_type", failure)
        self.assertIn("message", failure)
        self.assertIn("raw_excerpt", failure)
        self.assertIsInstance(failure["raw_excerpt"], str)

    def test_validate_rejects_schema_violation(self):
        with self.assertRaises(
            cerebras_client.MalformedCandidateError
        ) as ctx:
            cerebras_client.validate_model_output(
                "(candidate (:target f) (:parent 1))"
            )
        self.assertEqual(ctx.exception.failure["key"], ":definition")
        self.assertEqual(
            ctx.exception.failure["error_type"], "SExprSchemaError"
        )

    def test_error_is_s_expr_error(self):
        self.assertTrue(
            issubclass(
                cerebras_client.MalformedCandidateError, s_expr.SExprError
            )
        )

    def _fake_result(self, text):
        return {
            "text": text,
            "finish_reason": "stop",
            "model": "test-model",
            "latency_ms": 1.0,
            "input_tokens": 10,
            "output_tokens": 20,
        }

    def test_generate_candidate_ok_path_offline(self):
        with mock.patch.object(
            cerebras_client,
            "generate",
            return_value=self._fake_result(VALID_MINIMAL),
        ) as gen:
            out = cerebras_client.generate_candidate("make foo v1")
        gen.assert_called_once()
        self.assertTrue(out["ok"])
        self.assertIsNone(out["error"])
        self.assertEqual(out["candidate"]["target"], "foo")
        self.assertEqual(out["raw"], VALID_MINIMAL)
        self.assertEqual(out["model"], "test-model")
        # Prompt must instruct single S-expression output.
        _, kwargs = gen.call_args
        self.assertIn("S-expression", kwargs["system"])
        self.assertIn("(candidate", kwargs["system"])

    def test_generate_candidate_rejects_malformed_offline(self):
        with mock.patch.object(
            cerebras_client,
            "generate",
            return_value=self._fake_result("Sure! Here is some prose..."),
        ):
            out = cerebras_client.generate_candidate("make foo v1")
        self.assertFalse(out["ok"])
        self.assertIsNone(out["candidate"])
        self.assertIn("error_type", out["error"])
        self.assertEqual(out["raw"], "Sure! Here is some prose...")

    def test_existing_generate_api_untouched(self):
        import inspect

        sig = inspect.signature(cerebras_client.generate)
        self.assertEqual(
            list(sig.parameters),
            [
                "prompt",
                "model",
                "system",
                "max_tokens",
                "temperature",
                "reasoning_effort",
                "timeout",
            ],
        )


if __name__ == "__main__":
    unittest.main()
