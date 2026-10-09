"""Tests for TEST_CALL_INVALID: a broken test call is repaired without touching the definition.

The live failure: a correct DB-INSERT definition was rewritten three times because
its test calls used unquoted data lists, which SBCL reports as COMPILER_ERROR.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import oracle as orc  # noqa: E402

DEF = ("(defun db-insert (db table row) "
       "(let* ((entry (assoc table db)) "
       "(new-entry (cons table (append (cdr entry) (list row))))) "
       "(if entry (subst new-entry entry db) (append db (list new-entry)))))")
LIVE_BAD_CALL = '(db-insert (("users" ())) "users" \'(1 "alice" "pw"))'
# a bare symbol the call uses but nothing defines; the normaliser leaves symbols alone
REPLAY_BAD_CALL = '(db-insert then "users" \'(1 "alice" "pw"))'
GOOD_CALL = "(db-insert '((\"users\" ())) \"users\" '(1 \"alice\" \"pw\"))"
EXPECT = '(("users" (1 "alice" "pw")))'
COMPILE_ERR = ("Execution of a form compiled with errors.\n"
               "Form:\n  ((\"users\" NIL))\n"
               "Compile-time error:\n  illegal function call")
UNBOUND_ERR = "The variable THEN is unbound."


def _info(call, error, got=None):
    return {"call": call, "error": error, "got": got, "confidence": "high"}


def _session(tmp, **kw):
    return ag.Session(kw.pop("prompt", "x"), kw.pop("gen", lambda s, u: None),
                      registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                      log_path=Path(tmp) / "l.jsonl", **kw)


class TestCallClassifierTests(unittest.TestCase):
    def test_live_unquoted_list_is_a_test_call_error(self):
        infos = [_info(LIVE_BAD_CALL, COMPILE_ERR)]
        self.assertTrue(orc.is_test_call_error(infos[0], DEF))
        self.assertEqual(orc.failure_class(infos, None, definition=DEF), "TEST_CALL_INVALID")

    def test_same_form_inside_the_definition_is_a_definition_error(self):
        defn = "(defun f (x) ((\"users\" nil)))"
        info = _info("(f 1)", "Execution of a form compiled with errors.\n"
                     "Form:\n  ((\"users\" NIL))\nCompile-time error:\n  illegal function call")
        self.assertFalse(orc.is_test_call_error(info, defn))
        self.assertEqual(orc.failure_class([info], None, definition=defn), "COMPILER_ERROR")

    def test_normalisation_ignores_case_whitespace_quotes_and_nil(self):
        self.assertEqual(orc._norm_lisp("(( \"users\"  NIL ))"),
                         orc._norm_lisp('(("users" ()))'))
        self.assertEqual(orc._norm_lisp("'(a B)"), orc._norm_lisp("(a b)"))

    def test_real_compile_error_in_the_definition_is_still_compiler_error(self):
        defn = "(defun bad-form (x) (setf x))"
        info = _info("(bad-form 1)", "Execution of a form compiled with errors.\n"
                     "Form:\n  (SETF X)\nCompile-time error:\n  odd number of args")
        self.assertEqual(orc.failure_class([info], None, definition=defn), "COMPILER_ERROR")

    def test_undefined_variable_used_only_by_the_call(self):
        info = _info("(f then)", "The variable THEN is unbound.")
        self.assertEqual(orc.failure_class([info], None, definition="(defun f (x) x)"),
                         "TEST_CALL_INVALID")

    def test_undefined_function_with_package_prefix_in_the_call(self):
        info = _info("(f 'insert)", "undefined variable: COMMON-LISP-USER::INSERT")
        self.assertEqual(orc.failure_class([info], None, definition="(defun f (x) x)"),
                         "TEST_CALL_INVALID")

    def test_unbound_name_the_definition_uses_is_not_blamed_on_the_call(self):
        defn = "(defun f (x) (let* ((then x)) then))"
        info = _info("(f then)", "The variable THEN is unbound.")
        self.assertEqual(orc.failure_class([info], None, definition=defn), "RUNTIME_ERROR")

    def test_whole_symbol_match_only(self):
        # "insert" is inside "db-insert": not the same symbol
        info = _info("(db-insert 1)", "The variable INSERT is unbound.")
        self.assertEqual(orc.failure_class([info], None, definition="(defun f (x) x)"),
                         "RUNTIME_ERROR")

    def test_illegal_head_on_a_form_not_in_the_definition(self):
        info = _info("(f 1)", "Execution of a form compiled with errors.\n"
                     "Form:\n  (3 4)\nCompile-time error:\n  illegal function call")
        self.assertTrue(orc.is_test_call_error(info, "(defun f (x) x)"))

    def test_existing_classes_unchanged_without_definition(self):
        self.assertEqual(orc.failure_class([{"got": None, "confidence": "high",
                                             "error": "Execution of a form compiled with errors."}]),
                         "COMPILER_ERROR")
        self.assertEqual(orc.failure_class([{"got": "1", "error": "", "confidence": "low"}]),
                         "TEST_WRONG")

    def test_label_exists_for_postmortems(self):
        self.assertIn("TEST_CALL_INVALID", orc.CLASS_LABELS)
        with tempfile.TemporaryDirectory() as tmp:
            path = orc.write_postmortem(Path(tmp), "abc", {"root_cause": "TEST_CALL_INVALID",
                                        "root_cause_meaning": orc.CLASS_LABELS["TEST_CALL_INVALID"]})
            self.assertTrue(path.exists())


class TestCallRepairSessionTests(unittest.TestCase):
    @staticmethod
    def _worker(code):
        # the broken test call fails in the worker; everything else passes
        if REPLAY_BAD_CALL in code:
            return {"ok": False, "return_value": "", "stdout": "", "error": UNBOUND_ERR,
                    "timed_out": False, "elapsed_ms": 1.0}
        return {"ok": True, "return_value": "T" if "gg-check" in code else "OK",
                "stdout": "", "error": "", "timed_out": False, "elapsed_ms": 1.0}

    def test_broken_test_call_is_repaired_keeping_the_definition(self):
        first = {"action": "build", "name": "db-insert", "description": "insert a row",
                 "definition": DEF, "call": REPLAY_BAD_CALL,
                 "tests": [{"call": REPLAY_BAD_CALL, "expect": EXPECT}]}
        # the model also tries to change the definition: the session must keep DEF
        second = {"action": "build", "name": "db-insert", "description": "insert a row",
                  "definition": "(defun db-insert (db table row) (list table row))",
                  "call": GOOD_CALL,
                  "tests": [{"call": GOOD_CALL, "expect": EXPECT}]}
        replies = iter([first, second])
        prompts = []

        def gen(system, user):
            prompts.append(user)
            return ag._fake(next(replies))

        with tempfile.TemporaryDirectory() as tmp:
            sess = _session(tmp, gen=gen, worker_fn=self._worker)
            sess.run()
            self.assertEqual(sess.state, "done",
                             [e for e in sess.events if e["kind"] in ("error", "gave_up")])
            verdicts = [e for e in sess.events if e["kind"] == "verdict"]
            self.assertEqual(verdicts[0]["class"], "TEST_CALL_INVALID")
            kinds = [e["kind"] for e in sess.events]
            self.assertIn("test_call_repair", kinds)
            self.assertNotIn("repeat_candidate", kinds)
            self.assertEqual(sess.model_calls, 2)

            # the second prompt asks for tests only and quotes the error
            self.assertIn("Fix ONLY the test calls", prompts[1])
            self.assertNotIn("Fix ONLY the compile error", prompts[1])
            self.assertIn("The variable THEN is unbound", prompts[1])
            self.assertIn("single leading quote", prompts[1])
            self.assertIn(ag.lispstyle.ensure_docstring(DEF, "insert a row"), prompts[1])

            saved = _session_registry_load(tmp)
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["definition"], ag.lispstyle.ensure_docstring(DEF, "insert a row"))
            self.assertEqual(saved[0]["call"], GOOD_CALL)
            self.assertEqual([t["call"] for t in saved[0]["tests"]], [GOOD_CALL])


def _session_registry_load(tmp):
    return ag.ToolRegistry(Path(tmp) / "t.json").load()


class UndefinedFunctionWordingTests(unittest.TestCase):
    def test_sbcl_the_function_x_is_undefined_in_the_call_is_a_test_call_error(self):
        info = {"call": "(db-insert (db-init) \"users\" '(1 \"a\"))",
                "error": "The function COMMON-LISP-USER::DB-INIT is undefined.", "got": None}
        defn = "(defun db-insert (db table row) (append db (list row)))"
        self.assertTrue(orc.is_test_call_error(info, defn))
        self.assertEqual(orc.failure_class([info], definition=defn), "TEST_CALL_INVALID")
        # a helper the DEFINITION calls is the definition's problem, not the test's
        self.assertFalse(orc.is_test_call_error(
            info, "(defun db-insert (db table row) (db-init))"))


if __name__ == "__main__":
    unittest.main()
