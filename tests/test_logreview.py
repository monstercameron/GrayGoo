"""Logging completeness and the log review: prompts, replies, evals, fixes, calls."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import logreview  # noqa: E402
import s_expr  # noqa: E402


def _worker(code):
    return {"ok": True, "stdout": "", "return_value": "T", "error": "",
            "timed_out": False, "elapsed_ms": 2.0}


BAD = {"action": "build", "name": "sq", "description": "square a number",
       "definition": "(defun sq (x) (* x x)))",                       # surplus paren
       "tests": [{"call": "(sq (2))", "expect": "4"},                 # needs quoting -> wrong
                 {"call": "(other-thing)", "expect": "1"}],           # unusable
       "call": "(sq 2)"}


class SessionLogTests(unittest.TestCase):
    def _run(self, tmp, replies):
        it = iter(replies)
        log = Path(tmp) / "s.jsonl"
        sess = ag.Session("square 2", lambda s, u: ag._fake(next(it)),
                          registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                          worker_fn=_worker, log_path=log)
        sess.run()
        return sess, logreview.read_jsonl(log)

    def test_every_model_call_logs_prompt_system_id_and_sampling(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess, events = self._run(tmp, [BAD])
            calls = [e for e in events if e["kind"] == "model_call"]
            systems = [e for e in events if e["kind"] == "system_prompt"]
            self.assertTrue(calls and all(e["prompt"] and e["system"] for e in calls))
            self.assertIn("temperature", calls[0])
            self.assertEqual(len(systems), 1)                         # text logged once
            self.assertEqual(systems[0]["text"], ag.SYSTEM_PROMPT)
            self.assertEqual(calls[0]["system"], systems[0]["id"])
            replies = [e for e in events if e["kind"] == "model_reply"]
            self.assertTrue(all("text" in e and "input_tokens" in e for e in replies))

    def test_automatic_fixes_are_logged_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess, events = self._run(tmp, [BAD])
            fix = next(e for e in events if e["kind"] == "auto_fix")
            for name in ("trimmed-surplus-paren", "added-docstring", "quoted-data-list",
                         "dropped-unusable-test"):
                self.assertIn(name, fix["fixes"])
            self.assertEqual(fix["dropped"], ["(other-thing)"])
            decision = next(e for e in events if e["kind"] == "decision")
            self.assertNotIn("auto_fixes", decision["plan"])          # not echoed to the model

    def test_json_retry_logs_the_text_actually_sent(self):
        replies = iter(["{broken", json.dumps({"action": "use", "call": "(sq 2)"})])
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            reg.add({"name": "sq", "description": "square", "definition": "(defun sq (x) (* x x))"})
            log = Path(tmp) / "s.jsonl"
            sess = ag.Session("anything", lambda s, u: dict(ag._fake({}), text=next(replies)),
                              registry=reg, worker_fn=_worker, log_path=log)
            sess.run()
            retry = [e for e in logreview.read_jsonl(log) if e["kind"] == "model_call"
                     and "retry" in e["label"]][0]
            self.assertIn("NOT VALID JSON", retry["prompt"])
            self.assertEqual(retry["temperature"], 0.0)

    def test_typed_repl_calls_are_logged_including_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "tools.json")
            reg.add({"name": "sq", "description": "d", "definition": "(defun sq (x) (* x x))"})
            mgr = ag.SessionManager(reg)
            mgr.call_tool("(sq 3)")
            mgr.call_tool('(open "x")')
            rows = logreview.read_jsonl(Path(tmp) / "calls.jsonl")
            self.assertEqual([r["call"] for r in rows], ["(sq 3)", '(open "x")'])
            self.assertEqual([r["ok"] for r in rows], [True, False])
            self.assertTrue(rows[1]["error"].startswith("refused"))
            self.assertEqual(rows[0]["project"], "scratch")


class ReviewTests(unittest.TestCase):
    EVENTS = [
        {"kind": "goal", "prompt": "p", "mode": "live", "t": 10, "arm": "main"},
        {"kind": "model_call", "label": "plan", "prompt": "x" * 100, "temperature": 0.0},
        {"kind": "model_reply", "input_tokens": 50, "output_tokens": 20, "cost_usd": 0.001,
         "latency_ms": 300},
        {"kind": "step", "name": "db-insert"},
        {"kind": "auto_fix", "fixes": ["quoted-data-list", "added-docstring"]},
        {"kind": "repl", "ok": False, "elapsed_ms": 100},
        {"kind": "verdict", "ok": False, "class": "RUNTIME_ERROR",
         "detail": "(db-insert 1): The function COMMON-LISP-USER::FOUND is undefined."},
        {"kind": "repair", "attempt": 1},
        {"kind": "model_call", "label": "rewrite", "prompt": "y" * 400, "temperature": 0.8},
        {"kind": "model_reply", "input_tokens": 200, "output_tokens": 900, "cost_usd": 0.01,
         "latency_ms": 1500},
        {"kind": "json_retry"},
        {"kind": "promoted", "name": "db-insert"},
        {"kind": "summary", "outcome": "success", "efficiency": {"wasted_tokens": 70}},
    ]

    def test_review_says_where_tokens_and_repairs_go(self):
        rep = logreview.review([("s1", self.EVENTS)],
                               calls=[{"ok": True}, {"ok": False}],
                               requests=[{"ms": 5}, {"ms": 9, "error": "boom"}])
        self.assertEqual((rep["sessions"], rep["outcomes"]), (1, {"success": 1}))
        self.assertEqual(rep["by_call_type"][0]["label"], "rewrite")          # biggest first
        self.assertEqual(rep["tokens"], 1170)
        self.assertEqual(rep["failure_classes"], {"RUNTIME_ERROR": 1})
        self.assertEqual(rep["lisp_slips"], {"undefined-function": 1})
        self.assertEqual(rep["automatic_fixes"], {"quoted-data-list": 1, "added-docstring": 1})
        self.assertEqual(rep["costliest_tools"][0]["name"], "db-insert")
        self.assertEqual(rep["costliest_tools"][0]["repairs"], 1)
        self.assertEqual((rep["invalid_json_retries"], rep["wasted_tokens"]), (1, 70))
        self.assertEqual(rep["typed_calls"], {"count": 2, "failed": 1})
        self.assertEqual(rep["mounted_requests"]["errors"], {"boom": 1})
        text = logreview.render(rep)
        self.assertIn("rewrite", text)
        self.assertIn("Log coverage", text)

    def test_filters_pick_sessions_by_mode_project_and_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sessions"
            folder.mkdir()
            for name, goal in (("a", {"mode": "live", "project": "blog", "arm": "main"}),
                               ("b", {"mode": "demo", "arm": "main"}),
                               ("c", {"mode": "live", "project": "blog", "arm": "nomem"})):
                (folder / (name + ".jsonl")).write_text(
                    json.dumps(dict({"kind": "goal", "prompt": "p", "t": 5}, **goal)) + "\n",
                    encoding="utf-8")
            ids = lambda **kw: sorted(s for s, _ in logreview.load_sessions(tmp, **kw))
            self.assertEqual(ids(), ["a", "b"])
            self.assertEqual(ids(mode="live", project="blog"), ["a"])
            self.assertEqual(ids(main_only=False), ["a", "b", "c"])


class CharLiteralTests(unittest.TestCase):
    """The log review's top error was a parser gap: #\\' could not be read."""

    def test_character_literals_with_delimiter_characters_parse(self):
        for src, n in (("(char= c #\\')", 3), ("(list #\\( #\\) #\\; #\\\" #\\,)", 6),
                       ("(char= c #\\Space)", 3), ("(replace-char s #\\b #\\x)", 4)):
            self.assertEqual(len(s_expr.parse(src)), n, src)

    def test_sql_escape_style_definition_now_validates(self):
        plan = {"action": "build", "name": "esc", "description": "double single quotes",
                "definition": "(defun esc (s) (remove #\\' s))",
                "tests": [{"call": "(esc \"a'b\")", "expect": "\"ab\""}], "call": "(esc \"a'b\")"}
        self.assertIsNone(ag.validate_build(ag.normalize_plan(plan)))


class LogDrivenFixTests(unittest.TestCase):
    """Each case is a failure taken from live session dfacadc677."""

    PLAN = {"action": "build", "name": "html-escape", "description": "escape html",
            "definition": "(defun html-escape (s) s)", "call": "(html-escape \"a\")",
            "tests": [{"call": "(html-escape \"it's\")", "expect": "\"it&#39;s\""}]}

    def test_hash_inside_an_expected_string_is_allowed(self):
        self.assertIsNone(ag.validate_build(ag.normalize_plan(dict(self.PLAN))))
        vec = dict(self.PLAN, tests=[{"call": "(html-escape \"a\")", "expect": "#(1 2)"}])
        self.assertIn("cannot contain '#'", ag.validate_build(ag.normalize_plan(vec)))

    def test_expected_value_missing_a_closing_paren_is_completed(self):
        plan = ag.normalize_plan({
            "action": "build", "name": "set-table", "description": "replace a table",
            "definition": "(defun set-table (state name rows) state)",
            "call": "(set-table '((\"a\" ())) \"a\" '((\"x\")))",
            "tests": [{"call": "(set-table '((\"a\" ())) \"a\" '((\"x\")))",
                       "expect": "((\"a\" ((\"x\")))"}]})
        self.assertEqual(plan["tests"][0]["expect"], "((\"a\" ((\"x\"))))")
        self.assertIn("completed-paren-in-expected-value", plan["auto_fixes"])
        over = dict(plan, tests=[{"call": plan["call"], "expect": "(1 2))"}])
        self.assertIn("expected value (1 2)) has unbalanced parentheses",
                      ag.validate_build(over))

    def test_one_flat_test_among_nested_ones_is_a_test_call_error(self):
        import oracle as orc
        calls = ["(check-login '((\"users\" ((\"alice\" \"pw\")))) \"alice\" \"pw\")",
                 "(check-login '(\"users\" ((\"alice\" \"pw\"))) \"alice\" \"pw\")"]
        info = {"call": calls[1], "error": 'The value "users" is not of type LIST', "got": None}
        self.assertTrue(orc.is_flat_data_error(info, calls))
        self.assertEqual(orc.failure_class([info], definition="(defun check-login (db u p) db)",
                                           calls=calls), "TEST_CALL_INVALID")
        # every test flat: nothing says nesting was intended, so it stays a code error
        self.assertFalse(orc.is_flat_data_error(info, [calls[1]]))
        # mixed with a genuine wrong answer: the real bug wins
        wrong = {"call": calls[0], "error": "", "got": "NIL", "expected": "T"}
        self.assertNotEqual(orc.failure_class([info, wrong], definition="(defun f (x) x)",
                                              calls=calls), "TEST_CALL_INVALID")

    def test_joining_a_list_of_strings_has_a_hint_and_the_guide_is_right(self):
        import lispstyle
        import oracle as orc
        err = 'The value "<ul>" is not of type CHARACTER when setting an element of (ARRAY CHARACTER)'
        self.assertEqual([k for k, _ in orc.lisp_hints(err)], ["string-of-strings"])
        self.assertIn("(apply #'concatenate 'string", lispstyle.STYLE_GUIDE)
        self.assertNotIn("one CONCATENATE over a mapped list", lispstyle.STYLE_GUIDE)


if __name__ == "__main__":
    unittest.main()
