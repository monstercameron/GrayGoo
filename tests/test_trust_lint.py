"""trust_problems: a tool may not change, shadow or reach around trusted functions."""
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lispstyle  # noqa: E402
import webkit  # noqa: E402

# Kit tools that legitimately trip a rule, by name, with the reason. Empty: none does.
KIT_EXEMPT = {}

ADD = '(defun add-two (n)\n  "Adds two to N."\n  (+ n 2))'


def trust(text, reserved=()):
    return lispstyle.trust_problems(text, reserved=reserved)


class ReservedNameTests(unittest.TestCase):
    def test_gg_name_is_reported(self):
        self.assertEqual(trust('(defun gg-check (x)\n  "Checks X."\n  x)'),
                         ["the name gg-check is reserved for the harness: pick another name"])

    def test_gg_name_after_a_comment_is_reported(self):
        self.assertEqual(trust('; a note\n(defun gg-near (x) "Near." x)'),
                         ["the name gg-near is reserved for the harness: pick another name"])

    def test_upper_case_gg_name_is_reported_in_lower_case(self):
        self.assertEqual(trust('(defun GG-CHECK (x) "Checks." x)'),
                         ["the name gg-check is reserved for the harness: pick another name"])

    def test_kit_or_saved_name_is_not_reported_here(self):
        text = '(defun html-escape (text)\n  "Escapes TEXT."\n  text)'
        self.assertEqual(trust(text, reserved=["html-escape"]), [])

    def test_gg_word_in_a_docstring_is_fine(self):
        self.assertEqual(trust('(defun add-gg (x) "gg-check is the checker." x)'), [])

    def test_calling_a_reserved_function_is_fine(self):
        self.assertEqual(trust('(defun add-gg (x) "Adds." (gg-check x))', reserved=["gg-check"]), [])


class NestedDefinitionTests(unittest.TestCase):
    def test_nested_defun_is_reported(self):
        text = '(defun outer (x)\n  "Outer."\n  (defun helper (y) y)\n  (helper x))'
        self.assertEqual(trust(text), ["a tool is one function: it may not define a function "
                                       "helper inside itself"])

    def test_nested_defmacro_is_reported(self):
        text = '(defun outer (x) "Outer." (defmacro twice (f) f) x)'
        self.assertEqual(trust(text), ["a tool is one function: it may not define a macro twice "
                                       "inside itself"])

    def test_nested_defmethod_and_defstruct_are_reported(self):
        self.assertIn("a method describe-it", trust('(defun f (x) "F." (defmethod describe-it (x) x) x)')[0])
        self.assertIn("a structure point", trust('(defun f (x) "F." (defstruct point x) x)')[0])

    def test_in_package_is_reported(self):
        self.assertEqual(trust('(defun f (x) "F." (in-package cl-user) x)'),
                         ["a tool is one function: it may not change the package inside itself"])

    def test_definition_text_in_a_string_is_fine(self):
        self.assertEqual(trust('(defun add-two (n) "(defun helper (y) y) is an example." (+ n 2))'), [])

    def test_definition_text_in_a_comment_is_fine(self):
        self.assertEqual(trust('(defun add-two (n)\n  ; (defun helper (y) y)\n  (+ n 2))'), [])

    def test_local_functions_are_fine(self):
        self.assertEqual(trust('(defun f (x) "F." (flet ((helper (y) y)) (helper x)))'), [])
        self.assertEqual(trust('(defun f (x) "F." (labels ((a () 1) (b () (a))) (b)))'), [])

    def test_the_tool_own_defun_is_fine(self):
        self.assertEqual(trust(ADD), [])


class RedefinitionTests(unittest.TestCase):
    def test_setf_symbol_function_is_reported(self):
        out = trust("(setf (symbol-function 'gg-check) (lambda (x) x))")
        self.assertIn("it may not redefine or remove functions (setf symbol-function)", out)

    def test_setf_fdefinition_is_reported(self):
        out = trust("(setf (fdefinition 'html-escape) (lambda (x) x))", reserved=["html-escape"])
        self.assertIn("it may not redefine or remove functions (setf fdefinition)", out)

    def test_setf_macro_and_compiler_macro_function_are_reported(self):
        self.assertEqual(trust("(setf (macro-function 'when) f)"),
                         ["it may not redefine or remove functions (setf macro-function)"])
        self.assertEqual(trust("(setf (compiler-macro-function 'car) f)"),
                         ["it may not redefine or remove functions (setf compiler-macro-function)"])

    def test_setf_symbol_value_and_get_are_reported(self):
        self.assertEqual(trust("(setf (symbol-value 'x) 1)"),
                         ["it may not redefine or remove functions (setf symbol-value)"])
        self.assertEqual(trust("(setf (get 'a 'b) 1)"),
                         ["it may not redefine or remove functions (setf get)"])

    def test_fmakunbound_makunbound_unintern_are_reported(self):
        self.assertEqual(trust("(fmakunbound 'html-escape)"),
                         ["it may not redefine or remove functions (fmakunbound)"])
        self.assertEqual(trust("(makunbound 'x)"),
                         ["it may not redefine or remove functions (makunbound)"])
        self.assertEqual(trust("(unintern 'gg-check)"),
                         ["it may not redefine or remove functions (unintern)"])

    def test_trace_is_reported(self):
        self.assertEqual(trust("(trace gg-check)"),
                         ["it may not redefine or remove functions (trace)"])

    def test_local_definition_of_a_reserved_name_is_reported(self):
        self.assertEqual(trust("(flet ((gg-check (x) x)) (gg-check 1))"),
                         ["it may not shadow the reserved function gg-check with a local definition"])

    def test_labels_of_a_kit_name_is_reported(self):
        self.assertEqual(trust('(defun f (s) "F." (labels ((html-escape (t2) t2)) (html-escape s)))',
                               reserved=["html-escape"]),
                         ["it may not shadow the reserved function html-escape with a local definition"])

    def test_local_names_that_are_not_reserved_are_fine(self):
        self.assertEqual(trust("(flet ((helper (x) x)) (helper 1))"), [])
        self.assertEqual(trust("(labels ((utf8 (c) c)) (utf8 1))"), [])

    def test_lambda_list_names_that_are_also_operators_are_fine(self):
        self.assertEqual(trust("(defun status (load) load)"), [])
        self.assertEqual(trust("(defun f (x trace) (list x trace))"), [])

    def test_reserved_name_bound_as_a_variable_is_fine(self):
        self.assertEqual(trust("(let ((gg-check 1)) gg-check)"), [])

    def test_guards_for_redefinition_words(self):
        self.assertEqual(trust("(setf (gethash key table) value)"), [])
        self.assertEqual(trust("(defun wipe (unintern-count) unintern-count)"), [])
        self.assertEqual(trust('(defun add (x) "(setf (symbol-function \'f) g) is text." x)'), [])
        self.assertEqual(trust("(defun add (x)\n  ; (fmakunbound 'x)\n  x)"), [])
        self.assertEqual(trust("(defun f (trace) trace)"), [])


class RunTimeCodeTests(unittest.TestCase):
    def test_direct_calls_are_reported(self):
        for call, op in (("(eval x)", "eval"), ("(compile nil x)", "compile"),
                         ("(load path)", "load"), ("(read s)", "read"),
                         ("(read-from-string s)", "read-from-string"),
                         ("(intern name)", "intern"), ("(find-symbol name)", "find-symbol"),
                         ("(symbol-function 'x)", "symbol-function"),
                         ("(fdefinition 'x)", "fdefinition"),
                         ("(macroexpand-1 x)", "macroexpand-1")):
            self.assertEqual(trust("(defun f (x) %s)" % call),
                             ["it may not build or look up code at run time (%s)" % op], call)

    def test_funcall_of_a_computed_function_is_reported(self):
        self.assertEqual(trust('(funcall (intern "X") 1)'),
                         ["it may not build or look up code at run time (funcall)"])
        self.assertEqual(trust("(apply (fdefinition 'x) args)"),
                         ["it may not build or look up code at run time (apply)"])

    def test_quoted_or_function_quoted_operator_is_reported(self):
        self.assertEqual(trust("(funcall 'eval '(+ 1 2))"),
                         ["it may not build or look up code at run time ('eval)"])
        self.assertEqual(trust("(mapcar #'eval forms)"),
                         ["it may not build or look up code at run time ('eval)"])
        self.assertEqual(trust("(apply (function compile) args)"),
                         ["it may not build or look up code at run time ('compile)"])

    def test_intern_into_the_keyword_package_is_fine(self):
        # The saved tool parse-command: keywords are data, not callable code.
        text = ("(defun parse-command (words)\n"
                '  "Parse a list of words into a command keyword and remaining args."\n'
                "  (if (null words)\n      '(:cmd :help :args ())\n"
                "      (let* ((cmd (intern (string-upcase (first words)) :keyword))\n"
                "             (args (rest words)))\n        (list :cmd cmd :args args))))")
        self.assertEqual(trust(text), [])
        self.assertEqual(trust("(find-symbol name :keyword)"), [])

    def test_intern_into_any_other_package_is_reported(self):
        for call in ('(intern name "CL-USER")', "(intern name :cl-user)",
                     "(intern name (find-package :cl-user))", "(find-symbol name pkg)",
                     "(intern name)"):
            op = "find-symbol" if call.startswith("(find") else "intern"
            self.assertEqual(trust("(defun f (name pkg) %s)" % call),
                             ["it may not build or look up code at run time (%s)" % op], call)

    def test_coerce_of_a_quoted_lambda_is_reported(self):
        self.assertEqual(trust("(coerce '(lambda (x) x) 'function)"),
                         ["it may not build or look up code at run time (coerce of a quoted lambda)"])

    def test_eval_hidden_behind_a_character_literal_is_reported(self):
        # #\" is a character; a checker that read it as a string start would hide the eval.
        text = '(defun f (x) (list #\\" (eval x) "y"))'
        self.assertEqual(trust(text), ["it may not build or look up code at run time (eval)"])

    def test_eval_after_a_quote_inside_a_comment_is_reported(self):
        # The quote is in a comment, so it opens no string and the eval is code.
        text = '(defun f (x)\n  (list 1 ; he said "\n   (eval x) "y"))'
        self.assertEqual(trust(text), ["it may not build or look up code at run time (eval)"])

    def test_guards_for_run_time_words(self):
        self.assertEqual(trust("(funcall fn x)"), [])
        self.assertEqual(trust("(funcall #'car x)"), [])
        self.assertEqual(trust("(apply #'concatenate 'string parts)"), [])
        self.assertEqual(trust("(mapcar (lambda (r) (first r)) rows)"), [])
        self.assertEqual(trust("(funcall (lambda (x) x) 1)"), [])
        self.assertEqual(trust("(read-line stream)"), [])
        self.assertEqual(trust("(defun reader (read-count) read-count)"), [])
        self.assertEqual(trust('(string= status "read")'), [])
        self.assertEqual(trust("(list :read t)"), [])
        self.assertEqual(trust('(defun f (x) "Uses eval and (defun g)" (funcall fn x))'), [])
        self.assertEqual(trust("(defun f (x) ; (eval x)\n  x)"), [])
        self.assertEqual(trust("(defun f (c) (eq c #\\())"), [])


class PackageTests(unittest.TestCase):
    def test_package_qualified_names_are_reported(self):
        self.assertEqual(trust('(sb-ext:posix-getenv "HOME")'),
                         ["it may not use package-qualified names (sb-ext:posix-getenv)"])
        self.assertEqual(trust("(cl-user::helper 1)"),
                         ["it may not use package-qualified names (cl-user::helper)"])
        self.assertEqual(trust("(list 'sb-impl::*x*)"),
                         ["it may not use package-qualified names (sb-impl::*x*)"])

    def test_uninterned_symbol_is_reported(self):
        self.assertEqual(trust("(list #:g1)"), ["it may not use package-qualified names (#:g1)"])

    def test_reader_evaluation_is_reported(self):
        self.assertEqual(trust("(defun f () #.(+ 1 2))"),
                         ["reader evaluation (#.) is not allowed"])

    def test_escaped_symbol_names_are_reported(self):
        self.assertEqual(trust("(|EVAL| '(+ 1 2))"),
                         ["it may not use escaped symbol names (|EVAL|): write the plain name"])
        self.assertTrue(trust("(\\E\\V\\A\\L '(+ 1 2))")[0].startswith(
            "it may not use escaped symbol names"))

    def test_keywords_strings_and_characters_are_fine(self):
        self.assertEqual(trust("(list :status 200 :test #'string=)"), [])
        self.assertEqual(trust('(string= t "12:30")'), [])
        self.assertEqual(trust('(format nil "~a:~a" h m)'), [])
        self.assertEqual(trust('(defun f () "12:30" 1)'), [])
        self.assertEqual(trust("(defun f () ; meets at 12:30\n  1)"), [])
        self.assertEqual(trust("(char= c #\\:)"), [])
        self.assertEqual(trust('(defun f () "a::b" 1)'), [])


class CodeOnlyFlowTests(unittest.TestCase):
    def test_each_rule_gives_at_most_one_sentence(self):
        # Rules 1, 2, 4 and 5 each fire once; rule 5 reports its package name, not #. too.
        text = ("(defun gg-check (x)\n  (defun helper (y) y)\n  (eval x)\n  (sb-ext:x 1)\n"
                "  #.(+ 1 2))")
        self.assertEqual(trust(text), [
            "the name gg-check is reserved for the harness: pick another name",
            "a tool is one function: it may not define a function helper inside itself",
            "it may not build or look up code at run time (eval)",
            "it may not use package-qualified names (sb-ext:x)",
        ])

    def test_clean_saved_style_tool_passes(self):
        text = ('(defun total-price (items)\n  "Sum of the price of each item in ITEMS."\n'
                '  (apply #\'+ (mapcar (lambda (item) (second item)) items)))')
        self.assertEqual(trust(text, reserved=webkit.NAMES), [])


class KitTests(unittest.TestCase):
    def test_every_kit_tool_passes(self):
        for tool in webkit.KIT:
            if tool["name"] in KIT_EXEMPT:
                continue
            with self.subTest(tool=tool["name"]):
                self.assertEqual(trust(tool["definition"], reserved=webkit.NAMES), [])


class PerformanceTests(unittest.TestCase):
    """A 300,000-character input must be checked in under a second: no backtracking."""

    LIMIT = 1.0
    SIZE = 300000

    def assert_fast(self, text):
        start = time.perf_counter()
        trust(text, reserved=webkit.NAMES)
        self.assertLess(time.perf_counter() - start, self.LIMIT)

    def repeated(self, unit):
        return unit * (self.SIZE // len(unit))

    def test_nested_flet_bindings(self):
        self.assert_fast("(defun f (x) " + self.repeated("(flet ((a () 1)) "))

    def test_unterminated_and_escaped_quotes(self):
        self.assert_fast(self.repeated('"\\"'))
        self.assert_fast(self.repeated('\\"'))
        self.assert_fast('"' * self.SIZE)

    def test_pipes_hashes_and_comment_markers(self):
        self.assert_fast("|" * self.SIZE)
        self.assert_fast("#" * self.SIZE)
        self.assert_fast(self.repeated("#|"))
        self.assert_fast(self.repeated(';"'))

    def test_long_runs_after_a_setf_or_a_defun(self):
        self.assert_fast("(setf " + " " * self.SIZE)
        self.assert_fast("(defun " + " " * self.SIZE)
        self.assert_fast("(flet (" + self.repeated("(a () 1) "))


if __name__ == "__main__":
    unittest.main()
