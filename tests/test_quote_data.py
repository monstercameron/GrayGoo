"""Quoting of unquoted list data in model-written calls, and call validation.

Covers the four test calls from a live run that SBCL rejected as
"illegal function call", the forms that must stay untouched, the worked
examples inside plan step specs, and the validate_build call checks.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import s_expr  # noqa: E402


class LiveRunCallsTests(unittest.TestCase):
    def test_the_four_live_run_calls_are_quoted(self):
        cases = [
            ('(db-insert (("users" ())) "users" \'(1 "alice" "pw"))',
             '(db-insert \'(("users" ())) "users" \'(1 "alice" "pw"))'),
            ('(db-insert (("users" (\'(1 "alice" "pw")))) "users" \'(2 "bob" "pw2"))',
             '(db-insert \'(("users" ((1 "alice" "pw")))) "users" \'(2 "bob" "pw2"))'),
            ('(db-update-table (("users" ()) ("posts" \'(1))) "users" (\'(1 "a")))',
             '(db-update-table \'(("users" ()) ("posts" (1))) "users" \'((1 "a")))'),
            ('(db-select (("users" ((1 "a" "p")))) "users")',
             '(db-select \'(("users" ((1 "a" "p")))) "users")'),
        ]
        for before, after in cases:
            self.assertEqual(ag.quote_literals(before), after, before)
            s_expr.parse(ag.quote_literals(before))   # still one readable form

    def test_string_led_and_list_led_arguments_are_data(self):
        q = ag.quote_literals
        self.assertEqual(q('(f ("a" 1))'), "(f '(\"a\" 1))")
        self.assertEqual(q("(f ((1 2) (3 4)))"), "(f '((1 2) (3 4)))")
        self.assertEqual(q('(f (("a" (1 2))))'), "(f '((\"a\" (1 2))))")

    def test_strings_with_parentheses_are_not_corrupted(self):
        q = ag.quote_literals
        self.assertEqual(q('(f ("(1 2)" 3))'), "(f '(\"(1 2)\" 3))")
        self.assertEqual(q('(f "a (1 2" (x 1))'), '(f "a (1 2" (x 1))')


class MustNotBreakTests(unittest.TestCase):
    def test_already_quoted_nil_and_empty_lists_are_unchanged(self):
        q = ag.quote_literals
        for text in ("(f '(1 2))", "(f '((\"a\" 1)))", "(f (quote (1 2)))",
                     "(f nil)", "(f ())", "(f (nil))", "(f #'(lambda (x) x))"):
            self.assertEqual(q(text), text, text)

    def test_lambda_lists_and_code_are_unchanged(self):
        q = ag.quote_literals
        for text in ("(lambda (x) (* x x))", "(mapcar (lambda (x) (+ x 1)) xs)",
                     "(defun f (x) (+ x 1))", "(square (square 3))",
                     "(+ 1 2)", "(f (g (h x)))", "(f (1+ x))"):
            self.assertEqual(q(text), text, text)

    def test_let_and_do_binding_forms_are_unchanged(self):
        q = ag.quote_literals
        for text in ("(let ((a 1)) (+ a 1))", "(let* ((a 1) (b (f a))) b)",
                     "(do ((i 0 (1+ i))) ((> i 3)) (print i))",
                     "(flet ((g (x) (* x 2))) (g 3))",
                     "(destructuring-bind (a (b c)) (f) (+ a b c))",
                     "(multiple-value-bind (q r) (floor 7 2) (list q r))"):
            self.assertEqual(q(text), text, text)

    def test_cond_and_case_clauses_are_unchanged(self):
        q = ag.quote_literals
        for text in ('(cond ((> x 1) "big") (t "small"))',
                     '(case x ((1 2) "low") (t "other"))',
                     "(case x (1 \"one\") (2 \"two\"))"):
            self.assertEqual(q(text), text, text)

    def test_numeric_list_data_still_quoted_inside_non_binding_forms(self):
        q = ag.quote_literals
        self.assertEqual(q("(dolist (x (1 2 3)) (print x))"),
                         "(dolist (x '(1 2 3)) (print x))")
        self.assertEqual(q("(loop for x in (1 2) collect x)"),
                         "(loop for x in '(1 2) collect x)")
        self.assertEqual(q("(let ((a (1 2))) a)"), "(let ((a '(1 2))) a)")

    def test_character_literals_do_not_upset_depth(self):
        self.assertEqual(ag.quote_literals("(f #\\( (1 2))"), "(f #\\( '(1 2))")


class SpecExampleTests(unittest.TestCase):
    def test_example_call_in_a_step_spec_is_quoted_and_rest_kept(self):
        spec = ('insert-user (db, name, pw): add a row, e.g. '
                '(db-insert (("users" ())) "users" (1 "alice" "pw")) => T; '
                "returns the id")
        plan = {"steps": [{"name": "insert-user", "spec": spec}]}
        ag.normalize_plan(plan)
        self.assertEqual(
            plan["steps"][0]["spec"],
            'insert-user (db, name, pw): add a row, e.g. '
            '(db-insert \'(("users" ())) "users" \'(1 "alice" "pw")) => T; '
            "returns the id")

    def test_specs_without_a_quotable_example_are_unchanged(self):
        for spec in ("sum (xs): e.g. (sum (1 2)) is 3",          # no "=>"
                     "add (a b): e.g. (+ 1 2) => 3",               # code, not data
                     "name (x): returns a string"):
            plan = {"steps": [{"name": "s", "spec": spec}]}
            ag.normalize_plan(plan)
            self.assertEqual(plan["steps"][0]["spec"], spec, spec)


class ValidateBuildCallTests(unittest.TestCase):
    def _plan(self, name, tests):
        return {"name": name, "definition": "(defun %s (x) x)" % name,
                "tests": tests, "call": "(%s 1)" % name}

    def test_prose_after_a_call_is_rejected(self):
        plan = self._plan("db-init-table", [
            {"call": "(db-init-table) then insert", "expect": "T"}])
        reason = ag.validate_build(plan)
        self.assertIsNotNone(reason)
        self.assertIn("exactly one Lisp call", reason)

    def test_bare_symbol_or_empty_call_is_rejected(self):
        for call in ("db-init-table", "()"):
            plan = self._plan("db-init-table", [{"call": call, "expect": "T"}])
            self.assertIn("exactly one Lisp call", ag.validate_build(plan), call)

    def test_test_call_must_mention_the_tool_name(self):
        plan = self._plan("sq", [{"call": "(db-init)", "expect": "T"}])
        reason = ag.validate_build(plan)
        self.assertIn("does not call the tool sq", reason)

    def test_name_must_match_as_a_whole_symbol(self):
        plan = self._plan("sq", [{"call": "(sq-helper 2)", "expect": "4"}])
        self.assertIn("does not call the tool sq", ag.validate_build(plan))

    def test_legitimate_exact_and_property_tests_still_pass(self):
        plan = {"name": "sq", "definition": "(defun sq (x) (* x x))",
                "call": "(sq 3)",
                "tests": [{"call": "(sq 3)", "expect": "9"},
                          {"call": "(= (sq 2) (sq 2))", "expect": "T"},
                          {"call": "(funcall #'sq 4)", "expect": "16"},
                          {"call": "(sq '(1 2))", "expect": "T"}]}
        self.assertIsNone(ag.validate_build(plan))


if __name__ == "__main__":
    unittest.main()
