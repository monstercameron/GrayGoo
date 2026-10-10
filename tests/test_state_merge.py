"""Split STATE arguments are merged into one quoted list of tables; nothing else moves."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import webkit  # noqa: E402

# Calls and expectations copied from tests/test_webkit.py::StateShapeTests
# (test methods cannot be imported, so the literals are repeated here).
SHAPE_CASES = [
    ("(session-row '(\"sessions\" ((\"alice\" 100 \"abc\"))) \"abc\")", "session-row", 0,
     "(session-row '((\"sessions\" ((\"alice\" 100 \"abc\")))) \"abc\")"),
    ("(render-posts-page '(\"posts\" ()))", "render-posts-page", 0,
     "(render-posts-page '((\"posts\" ())))"),
    ("(login-redirect '(\"users\" ((\"alice\" \"pw\")) \"sessions\" ()) \"alice\" 100 \"abc\")",
     "login-redirect", 0,
     "(login-redirect '((\"users\" ((\"alice\" \"pw\"))) (\"sessions\" ())) \"alice\" 100 \"abc\")"),
    ("(handle-logout '(:cookies ((\"sid\" \"abc\"))) '(\"sessions\" ((\"alice\" 100 \"abc\")) (\"users\" ())))",
     "handle-logout", 1,
     "(handle-logout '(:cookies ((\"sid\" \"abc\"))) '((\"sessions\" ((\"alice\" 100 \"abc\"))) (\"users\" ())))"),
    ("(handle-login '(:method \"GET\" :path \"/login\") '(:users ((\"a\" \"p\")) :sessions ()))",
     "handle-login", 1,
     "(handle-login '(:method \"GET\" :path \"/login\") '((\"users\" ((\"a\" \"p\"))) (\"sessions\" ())))"),
    ("(table-rows '((\"posts\" ((\"Hi\" \"text\"))) (\"users\" ())) \"posts\")", "table-rows", 0,
     "(table-rows '((\"posts\" ((\"Hi\" \"text\"))) (\"users\" ())) \"posts\")"),
    ("(table-rows '() \"posts\")", "table-rows", 0, "(table-rows '() \"posts\")"),
    ("(table-rows state \"posts\")", "table-rows", 0, "(table-rows state \"posts\")"),
    ("(table-rows '(1 2 3) \"posts\")", "table-rows", 0, "(table-rows '(1 2 3) \"posts\")"),
]

ADD_PRODUCT = ("(handle-add-product '(:method \"POST\" :path \"/add-product\" :cookies ((\"sid\" \"abc\"))) "
               "'((\"products\" ())) '(\"users\" ()) '(\"sessions\" ((\"abc\" \"admin\"))))")
ADD_PRODUCT_FIXED = ("(handle-add-product '(:method \"POST\" :path \"/add-product\" :cookies ((\"sid\" \"abc\"))) "
                     "'((\"products\" ()) (\"users\" ()) (\"sessions\" ((\"abc\" \"admin\")))))")
LOGIN = ("(handle-login '(:method \"POST\" :path \"/login\" :nonce \"abc\") "
         "'((\"users\" ((\"admin\" \"pass\")))) '(\"sessions\" ()))")
LOGIN_FIXED = ("(handle-login '(:method \"POST\" :path \"/login\" :nonce \"abc\") "
               "'((\"users\" ((\"admin\" \"pass\"))) (\"sessions\" ())))")


class StateMergeTests(unittest.TestCase):
    def fix(self, call, name, index=1, arity=2):
        return webkit.fix_state_args(call, name, index, arity=arity)

    def test_split_state_in_an_add_call_becomes_one_list_of_tables(self):
        self.assertEqual(self.fix(ADD_PRODUCT, "handle-add-product"), ADD_PRODUCT_FIXED)

    def test_split_state_in_a_login_call_becomes_one_list_of_tables(self):
        self.assertEqual(self.fix(LOGIN, "handle-login"), LOGIN_FIXED)

    def test_merge_inside_a_property_test_leaves_the_rest_of_the_form_alone(self):
        call = ("(let ((r (handle-login '(:method \"POST\" :form ((\"u\" \"a\"))) "
                "'((\"users\" ())) '(\"sessions\" ())))) "
                "(and (listp r) (assoc \"Location\" (getf r :headers) :test #'equal)))")
        self.assertEqual(
            self.fix(call, "handle-login"),
            "(let ((r (handle-login '(:method \"POST\" :form ((\"u\" \"a\"))) "
            "'((\"users\" ()) (\"sessions\" ()))))) "
            "(and (listp r) (assoc \"Location\" (getf r :headers) :test #'equal)))")

    def test_correct_arity_gets_only_the_nesting_repair(self):
        self.assertEqual(self.fix("(handle-login '(:method \"POST\") '((\"users\" ())))", "handle-login"),
                         "(handle-login '(:method \"POST\") '((\"users\" ())))")
        self.assertEqual(self.fix("(handle-login '(:method \"GET\") '(\"users\" ()))", "handle-login"),
                         "(handle-login '(:method \"GET\") '((\"users\" ())))")

    def test_extra_arguments_that_are_not_tables_leave_the_call_unchanged(self):
        for call in ("(f '((\"a\" ())) 5)",
                     "(f '((\"a\" ())) '(1 2))",
                     "(f '((\"a\" ())) state)"):
            self.assertEqual(self.fix(call, "f", index=0, arity=1), call)

    def test_a_request_plist_among_the_extras_is_never_merged(self):
        for call in ("(handle-login '(:method \"POST\" :cookies ((\"sid\" \"abc\"))) "
                     "'((\"users\" ())) '(:method \"GET\"))",
                     "(handle-login '(:method \"POST\") '((\"users\" ())) "
                     "'(:cookies ((\"sid\" \"abc\"))))"):
            self.assertEqual(self.fix(call, "handle-login"), call)

    def test_state_not_last_is_never_merged(self):
        call = "(g '((\"a\" ())) '(\"b\" ()) '(\"c\" ()))"
        self.assertEqual(self.fix(call, "g", index=0, arity=2), call)
        self.assertEqual(self.fix("(g '(\"a\" ()) '(\"b\" ()) '(\"c\" ()))", "g", index=0, arity=2),
                         "(g '((\"a\" ())) '(\"b\" ()) '(\"c\" ()))")

    def test_arity_none_reproduces_the_old_behaviour(self):
        for call, name, index, expected in SHAPE_CASES:
            self.assertEqual(webkit.fix_state_args(call, name, index), expected, call)
            self.assertEqual(webkit.fix_state_args(call, name, index, arity=None), expected, call)

    def test_arity_none_never_merges_extra_arguments(self):
        for name, call in (("handle-add-product", ADD_PRODUCT), ("handle-login", LOGIN)):
            self.assertEqual(webkit.fix_state_args(call, name, 1), call, name)
            self.assertEqual(webkit.fix_state_args(call, name, 1, arity=None), call, name)


if __name__ == "__main__":
    unittest.main()
