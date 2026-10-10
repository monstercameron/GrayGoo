"""Fixes from the command-line todo run: frozen string expectations, CASE on strings,
plists written as code, joining words, the sub-command convention, string case in
expected values, and the deep-call budget."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import lispstyle  # noqa: E402
import oracle as orc  # noqa: E402
import webkit  # noqa: E402


def _session(tmp, **kw):
    return ag.Session(kw.pop("prompt", "x"), kw.pop("gen", lambda s, u: None),
                      registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                      log_path=Path(tmp) / "l.jsonl", **kw)


class FrozenStringExpectationTests(unittest.TestCase):
    """A: only facts are frozen; a string literal is the model's own formatting."""

    def test_a_passing_string_expectation_is_not_frozen_and_may_be_corrected(self):
        plan = {"name": "tag", "description": "wrap in brackets",
                "definition": '(defun tag (x) (format nil "[~a]" x))',
                "call": "(tag 1)", "tests": [{"call": "(tag 1)", "expect": '"[1]"'}]}
        with tempfile.TemporaryDirectory() as tmp:
            sess = _session(tmp)
            self.assertTrue(sess._rehearse(plan, "")["ok"])
            self.assertNotIn("(tag 1)", sess._frozen)
            spelled = dict(plan, tests=[{"call": "(tag 1)", "expect": '"[1]~%"'}])
            self.assertIsNone(ag.validate_build(spelled, sess._frozen))

    def test_a_passing_number_expectation_is_still_frozen(self):
        plan = {"name": "sq", "description": "d", "definition": "(defun sq (x) (* x x))",
                "call": "(sq 3)", "tests": [{"call": "(sq 3)", "expect": "9"}]}
        with tempfile.TemporaryDirectory() as tmp:
            sess = _session(tmp)
            self.assertTrue(sess._rehearse(plan, "")["ok"])
            self.assertEqual(sess._frozen["(sq 3)"], "9")
            drifted = dict(plan, tests=[{"call": "(sq 3)", "expect": "10"}])
            self.assertIn("frozen", ag.validate_build(drifted, sess._frozen))

    def test_an_oracle_set_string_expectation_stays_frozen(self):
        plan = {"name": "md5-hash", "description": "MD5 of a string",
                "definition": "(defun md5-hash (s) s)", "call": '(md5-hash "abc")',
                "tests": [{"call": '(md5-hash "abc")', "expect": '"0"'}]}
        with tempfile.TemporaryDirectory() as tmp:
            sess = _session(tmp)
            sess._reference_pass(plan, {"infos": [{"index": 0, "got": '"0"'}]})
            self.assertEqual(sess._frozen['(md5-hash "abc")'],
                             '"900150983cd24fb0d6963f7d28e17f72"')
            changed = dict(plan, tests=[{"call": '(md5-hash "abc")', "expect": '"0"'}])
            self.assertIn("frozen", ag.validate_build(changed, sess._frozen))


class CaseOnStringsTests(unittest.TestCase):
    """B: CASE compares with EQL, so string keys never match."""

    REASON = "CASE compares with EQL and can never match a string"

    def _plan(self, definition):
        return {"name": "run", "description": "d", "definition": definition,
                "call": "(run nil)", "tests": [{"call": "(run nil)", "expect": "0"}]}

    def test_string_keys_are_refused_with_the_reason(self):
        definition = '(defun run (args) (case (first args) ("add" 1) ("list" 2) (t 0)))'
        self.assertIn(self.REASON, lispstyle.case_problems(definition)[0])
        problem = ag.validate_build(self._plan(definition))
        self.assertIn(self.REASON, problem)
        self.assertIn('(string= x "add")', problem)

    def test_list_keys_and_ecase_and_ccase_are_refused_too(self):
        for definition in ('(defun k (x) (case x (("a" "b") 1) (t 0)))',
                           '(defun k (x) (ecase x ("a" 1)))',
                           '(defun k (x) (ccase x ("a" 1)))'):
            self.assertTrue(lispstyle.case_problems(definition), definition)

    def test_numbers_characters_symbols_and_keywords_are_not_flagged(self):
        for definition in ('(defun k (n) (case n (1 "one") (2 "two") (t "many")))',
                           "(defun k (c) (case c (#\\a 1) (#\\b 2) (t 0)))",
                           "(defun k (x) (case x (:add 1) ((red blue) 2) (otherwise 3)))",
                           "(defun k (x) (case x ((nil) 1) (t 0)))"):
            self.assertEqual(lispstyle.case_problems(definition), [], definition)

    def test_the_word_case_in_strings_comments_and_names_is_not_flagged(self):
        for definition in ('(defun f (x) "use CASE (case x (\\"a\\" 1)) here" x)',
                           '(defun case-name (x) x)',
                           '(defun g (x) ; (case x ("a" 1))\n  x)'):
            self.assertEqual(lispstyle.case_problems(definition), [], definition)


class KeywordCallHintTests(unittest.TestCase):
    """C: a plist written as code gets its own hint, not undefined-function."""

    def test_a_keyword_called_as_a_function_gets_the_plist_hint_only(self):
        keys = [k for k, _ in orc.lisp_hints("The function :OUTPUT is undefined.")]
        self.assertIn("keyword-call", keys)
        self.assertNotIn("undefined-function", keys)
        advice = {k: a for k, _, a in orc.HINTS}["keyword-call"]
        self.assertIn("(list :output text :state state)", advice)

    def test_an_ordinary_undefined_function_still_gets_its_hint(self):
        keys = [k for k, _ in orc.lisp_hints("The function FORMAT-ROW is undefined.")]
        self.assertIn("undefined-function", keys)
        self.assertNotIn("keyword-call", keys)

    def test_unbound_and_plist_as_alist_hints_are_unaffected(self):
        self.assertEqual([k for k, _ in orc.lisp_hints("The variable X is unbound.")],
                         ["unbound"])
        keys = [k for k, _ in orc.lisp_hints("The value :COOKIES is not of type LIST")]
        self.assertIn("plist-as-alist", keys)
        self.assertNotIn("keyword-call", keys)


class JoinStringsTests(unittest.TestCase):
    """D: a kit tool that joins a list of strings with a separator, run in SBCL."""

    def test_the_join_strings_tests_pass_in_sbcl(self):
        tool = next(t for t in webkit.KIT if t["name"] == "join-strings")
        self.assertGreaterEqual(len(tool["tests"]), 3)
        checks = " ".join("(gg-check %s '%s)" % (c, e) for c, e in tool["tests"])
        env = ag._worker_fn("%s\n%s\n(list %s)" % (ag.GG_CHECK, tool["definition"], checks))
        self.assertTrue(env.get("ok"), env.get("error"))
        self.assertEqual(set(env["return_value"].strip("()").split()), {"T"})

    def test_join_strings_is_a_state_helper_and_is_documented_for_both_apps(self):
        self.assertIn("join-strings", webkit.STATE_NAMES)
        self.assertIn("(join-strings strings separator)", webkit.USAGE)
        self.assertIn("(join-strings", ag.CLI_APP_CONTRACT)


class SubCommandConventionTests(unittest.TestCase):
    """E: handle-command dispatches; one cmd-<word> tool per command gets the rest."""

    def test_the_contract_states_one_dispatch_convention(self):
        text = ag.CLI_APP_CONTRACT
        for phrase in ("(first args)", "(rest args)", "cmd-<word>",
                       "(cmd-<word> args state now)", "AFTER the command word"):
            self.assertIn(phrase, text)

    def test_the_contract_keeps_its_required_wording_and_stays_short(self):
        text = ag.CLI_APP_CONTRACT
        for phrase in ("(handle-command args state now)", "never read the clock",
                       "COMMAND-LINE APP CONTRACT"):
            self.assertIn(phrase, text)
        self.assertLess(len(text), 1900)


class StringCaseGuideTests(unittest.TestCase):
    """F: the style guide says strings keep their case in expected values."""

    def test_the_guide_says_strings_keep_their_case_and_quotes(self):
        self.assertIn("STRINGS keep their exact case and their double quotes",
                      lispstyle.STYLE_GUIDE)
        self.assertIn("symbols and keywords may be lower case", lispstyle.STYLE_GUIDE)


class DeepCallBudgetTests(unittest.TestCase):
    """G: a thinking call gets a budget its reasoning cannot use up (8000 tokens)."""

    def test_a_low_effort_thinking_call_gets_8000_tokens(self):
        self.assertGreaterEqual(ag.MAX_DEEP_CALLS, 1)
        saved = (getattr(ag._TEMP, "deep", False), getattr(ag._TEMP, "effort", "low"))
        try:
            ag._TEMP.deep, ag._TEMP.effort = True, "low"
            with mock.patch.object(ag, "live_status", return_value={"available": True,
                                                                     "reason": ""}), \
                    mock.patch("cerebras_client.generate",
                               return_value={"text": "{}"}) as gen:
                ag.live_generate("system", "user")
        finally:
            ag._TEMP.deep, ag._TEMP.effort = saved
        self.assertEqual(gen.call_args.kwargs["max_tokens"], 8000)
        self.assertEqual(gen.call_args.kwargs["reasoning_effort"], "low")


if __name__ == "__main__":
    unittest.main()
