"""Malformed model JSON: tolerant parsing, varied retries, and no dead sessions.

Replays the reply that killed live session 67321997ae: the last test's
``expect`` string had no closing quote, and the retry at temperature 0
returned the same broken text.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402

DEFN = ("(defun db-insert (db table row)\n  (mapcar (lambda (e) (if (string= (first e) table)\n"
        "      (list (first e) (append (second e) (list row))) e)) db))")
CALL = "(db-insert '((\"users\" ())) \"users\" '(\"alice\" \"pw\"))"
EXPECT = "((\"users\" ((\"alice\" \"pw\"))))"
GOOD = {"action": "build", "name": "db-insert", "description": "add a row",
        "definition": DEFN, "tests": [{"call": CALL, "expect": EXPECT}], "call": CALL}


def broken_like_the_live_reply():
    """Valid JSON with the closing quote of the last ``expect`` removed."""
    text = json.dumps(GOOD)
    marker = json.dumps(EXPECT) + "}]"
    assert marker in text
    return text.replace(marker, json.dumps(EXPECT)[:-1] + "}]")


class TolerantParseTests(unittest.TestCase):
    def test_missing_closing_quote_before_a_brace_is_repaired(self):
        bad = broken_like_the_live_reply()
        with self.assertRaises(ValueError):
            json.loads(bad)
        self.assertEqual(ag.extract_json(bad), GOOD)

    def test_reply_cut_off_mid_string_is_not_patched(self):
        # incomplete content must be re-asked, never silently accepted
        with self.assertRaises(ValueError):
            ag.extract_json('{"action":"use","call":"(sq 2')

    def test_valid_json_and_fenced_json_are_unchanged(self):
        self.assertEqual(ag.extract_json(json.dumps(GOOD)), GOOD)
        self.assertEqual(ag.extract_json("```json\n%s\n```" % json.dumps(GOOD)), GOOD)

    def test_garbage_still_raises(self):
        for bad in ("no json here", "{this is : not json at all", ""):
            with self.assertRaises(ValueError):
                ag.extract_json(bad)


class BalanceCallTests(unittest.TestCase):
    D3 = "(defun db-insert (db table row) db)"

    def test_live_dropped_paren_in_nested_data_is_restored_by_arity(self):
        bad = "(db-insert '((\"users\" ((\"bob\" \"x\"))) \"users\" '(\"alice\" \"pw\"))"
        good = "(db-insert '((\"users\" ((\"bob\" \"x\")))) \"users\" '(\"alice\" \"pw\"))"
        self.assertEqual(ag.balance_call(bad, self.D3), good)

    def test_unquoted_live_form_is_balanced_then_quoted(self):
        plan = ag.normalize_plan({"action": "build", "name": "db-insert", "definition": self.D3,
                                  "tests": [{"call": "(db-insert ((\"users\" ((\"bob\" \"x\"))) "
                                                     "\"users\" (\"alice\" \"pw\"))", "expect": "1"}]})
        self.assertEqual(plan["tests"][0]["call"],
                         "(db-insert '((\"users\" ((\"bob\" \"x\")))) \"users\" '(\"alice\" \"pw\"))")
        self.assertIsNone(ag.validate_build(
            dict(plan, description="d", call=plan["tests"][0]["call"])))

    def test_left_alone_when_balanced_ambiguous_or_variadic(self):
        ok = "(db-insert '((\"users\" ())) \"users\" '(1))"
        self.assertEqual(ag.balance_call(ok, self.D3), ok)
        amb = "(f '((1) '((2))"                       # two placements give 2 args
        self.assertEqual(ag.balance_call(amb, "(defun f (a b) a)"), amb)
        var = "(g '((1) 2)"
        self.assertEqual(ag.balance_call(var, "(defun g (a &rest more) a)"), var)
        paren_in_string = "(h \"a(b\" '(1))"
        self.assertEqual(ag.balance_call(paren_in_string, "(defun h (s l) s)"), paren_in_string)


class PruneAndTrimTests(unittest.TestCase):
    def test_surplus_closing_paren_after_the_defun_is_dropped(self):
        self.assertEqual(ag.trim_surplus_parens("(defun f (x) x))"), "(defun f (x) x)")
        self.assertEqual(ag.trim_surplus_parens("(defun f (x) x)"), "(defun f (x) x)")
        two = "(defun f (x) x) (defun g (y) y)"
        self.assertEqual(ag.trim_surplus_parens(two), two)      # not just stray parens

    def test_unusable_tests_are_dropped_when_good_ones_remain(self):
        plan = ag.normalize_plan({
            "action": "build", "name": "sq", "description": "d",
            "definition": "(defun sq (x) (* x x)))",
            "tests": [{"call": "(sq 2)", "expect": "4"},
                      {"call": "(db-init)", "expect": "1"},
                      {"call": "(sq 3) then more", "expect": "9"}]})
        self.assertEqual([t["call"] for t in plan["tests"]], ["(sq 2)"])
        self.assertEqual(plan["dropped_tests"], ["(db-init)", "(sq 3) then more"])
        self.assertEqual(plan["definition"], ag.lispstyle.ensure_docstring("(defun sq (x) (* x x))", "d"))

    def test_when_no_test_is_usable_validation_still_reports_it(self):
        plan = ag.normalize_plan({
            "action": "build", "name": "sq", "description": "d",
            "definition": "(defun sq (x) (* x x))", "call": "(sq 2)",
            "tests": [{"call": "(db-init)", "expect": "1"}]})
        self.assertEqual(len(plan["tests"]), 1)
        self.assertIn("does not call the tool", ag.validate_build(plan) or "")


class RetryTests(unittest.TestCase):
    def _session(self, tmp, gen, worker=None):
        return ag.Session("add a row to a table", gen,
                          registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                          worker_fn=worker or (lambda code: {
                              "ok": True, "stdout": "", "return_value": "T",
                              "error": "", "timed_out": False, "elapsed_ms": 1.0}),
                          log_path=Path(tmp) / "l.jsonl")

    def test_json_retries_raise_the_temperature_each_time(self):
        temps = []
        replies = iter(["{not json", "{still not json", json.dumps(GOOD)])

        def gen(system, user):
            temps.append(getattr(ag._TEMP, "value", None))
            return dict(ag._fake({}), text=next(replies))
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._session(tmp, gen)
            sess.run()
            self.assertEqual(sess.state, "done", sess.events)
        # the first retry stays at 0: its prompt has changed, and sampling can return nothing
        self.assertEqual(temps, [None, 0.0, 0.4])
        self.assertIsNone(getattr(ag._TEMP, "value", None))

    def test_unreadable_repair_reply_costs_one_attempt_not_the_session(self):
        bad_plan = dict(GOOD, tests=[{"call": "(db-insert) then stop", "expect": "1"}])
        replies = iter([json.dumps(bad_plan),            # fails validation -> repair
                        "{junk", "{junk", "{junk",       # repair reply unreadable 3x
                        json.dumps(GOOD)])               # next attempt succeeds

        def gen(system, user):
            return dict(ag._fake({}), text=next(replies))
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._session(tmp, gen)
            sess.run()
            kinds = [e["kind"] for e in sess.events]
            self.assertIn("bad_reply", kinds)
            self.assertNotIn("error", kinds)
            self.assertEqual(sess.state, "done", sess.events)


if __name__ == "__main__":
    unittest.main()
