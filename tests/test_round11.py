"""From the first live builds with context compaction (sessions 7b71fd4b4a, db962ae2aa).

Compaction did its job (34-39% less prompt text, 9 of 19 repairs sent as small edits,
regression checks in 8 ms). What went wrong: the stylesheet function and the page
functions used different class names, so pages stayed half styled through every round
of fixes; plain calls sampled above temperature 0 answered with nothing; replies cut
off at the token limit were "repaired" into functions without tests; state was split
over several arguments; and a compile error never reached the model."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import oracle as orc  # noqa: E402
import visualcheck  # noqa: E402
import webkit  # noqa: E402

CSS = ("*{margin:0}body{font-family:'Inter',sans-serif;background:url(https://fonts.example.com/a.b.css)}"
       ".glass,.stat-card:hover{transition:transform 0.2s}a.btn-primary > span{color:#fff}"
       "@media (max-width:600px){.container{padding:0.5rem}}")
SHEET = '(defun css-style () "the stylesheet" "%s")' % CSS
PAGE = ('(defun render-login-page (request state) (html-page 200 (concatenate (quote string) '
        '"<div class=\\"login-card glass\\"><input class=\\"form-input\\"><button class=\'btn-primary\'>Go</button></div>")))')


class ClassNameTests(unittest.TestCase):
    def test_classes_are_read_from_selectors_only(self):
        self.assertEqual(visualcheck.css_classes(CSS), ["glass", "stat-card", "btn-primary", "container"])
        self.assertEqual(visualcheck.css_classes("no css here"), [])

    def test_classes_used_by_a_page_are_read_from_html_and_from_lisp_source(self):
        self.assertEqual(visualcheck.html_classes('<div class="a b-c"><p class=\'d\'>x</p></div>'), ["a", "b-c", "d"])
        self.assertEqual(visualcheck.html_classes(PAGE), ["login-card", "glass", "form-input", "btn-primary"])

    def test_the_gap_between_pages_and_stylesheet_is_found(self):
        page = {"html": '<html><head><style>%s</style></head><body><div class="login-card glass">'
                        '<a class="btn-primary">x</a></div></body></html>' % CSS}
        gaps = visualcheck.style_gaps([page])
        self.assertEqual(gaps["undefined"], ["login-card"])
        self.assertIn("glass", gaps["defined"])
        self.assertFalse(gaps["framework"])

    def test_a_page_that_loads_a_css_framework_is_not_judged(self):
        page = {"html": '<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>'
                        '<div class="flex items-center">x</div>'}
        self.assertEqual(visualcheck.style_gaps([page]), {"undefined": [], "defined": [], "framework": True})

    def _session(self, tmp, tools):
        reg = ag.ToolRegistry(Path(tmp) / "t.json")
        for t in tools:
            reg.add(t)
        sess = ag.Session("make it look professional", lambda s, u: ag._fake({"action": "stop"}),
                          registry=reg, log_path=Path(tmp) / "l.jsonl")
        sess._app = True
        return sess

    def test_a_page_step_is_told_the_classes_the_stylesheet_defines(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._session(tmp, [{"name": "css-style", "description": "css", "definition": SHEET}])
            prompt = sess._step_prompt({"name": "render-login-page", "spec": "(render-login-page request state) -> page"})
            self.assertIn("THE STYLESHEET css-style DEFINES ONLY THESE CLASSES: glass, stat-card, "
                          "btn-primary, container.", prompt)
            router = sess._step_prompt({"name": "handle-request", "spec": "(handle-request request state)"})
            self.assertNotIn("DEFINES ONLY THESE CLASSES", router)

    def test_the_stylesheet_step_is_told_the_classes_the_pages_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._session(tmp, [
                {"name": "css-style", "description": "css", "definition": SHEET},
                {"name": "render-login-page", "description": "page", "definition": PAGE},
                {"name": "handle-request", "description": "router", "definition":
                 "(defun handle-request (request state) (render-login-page request state))"}])
            prompt = sess._step_prompt({"name": "css-style", "spec": "(css-style) -> css text"})
            self.assertIn("THE SAVED PAGES USE THESE CLASSES: login-card, glass, form-input, btn-primary.", prompt)
            self.assertNotIn("DEFINES ONLY THESE CLASSES", prompt)

    def test_nothing_is_added_outside_a_web_app_or_before_a_stylesheet_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._session(tmp, [{"name": "render-login-page", "description": "page", "definition": PAGE}])
            self.assertEqual(sess._class_note({"name": "render-home"}), "")
            sess = self._session(tmp, [{"name": "css-style", "description": "css", "definition": SHEET}])
            sess._app = False
            self.assertEqual(sess._class_note({"name": "render-home"}), "")

    def test_the_fix_planner_is_given_the_likely_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._session(tmp, [])
            sess._style_gaps = {"undefined": ["login-card", "form-input"], "defined": ["glass"], "framework": False}
            note = sess._gap_note()
            self.assertIn("the stylesheet has no rule for them: login-card, form-input.", note)
            self.assertIn("The stylesheet defines: glass.", note)
            sess._style_gaps = {"undefined": ["x"], "defined": [], "framework": True}
            self.assertEqual(sess._gap_note(), "")


class StateArgumentTests(unittest.TestCase):
    def fix(self, defn, call):
        plan = ag.normalize_plan({"action": "build", "name": defn.split()[1], "description": "d",
                                  "definition": defn, "call": call, "tests": [{"call": call, "expect": "T"}]})
        return plan["tests"][0]["call"]

    def test_tables_written_as_extra_name_and_rows_arguments_are_folded_into_the_state(self):
        req = "'(:method \"GET\" :path \"/\" :cookies ())"
        self.assertEqual(
            self.fix("(defun render-dashboard (request state) nil)",
                     "(render-dashboard %s '((\"products\" ((\"Widget\" \"10\" \"A widget\")))) \"users\" "
                     "'((\"user\" \"pass\")) \"sessions\" ())" % req),
            "(render-dashboard %s '((\"products\" ((\"Widget\" \"10\" \"A widget\"))) (\"users\" ((\"user\" \"pass\"))) "
            "(\"sessions\" ())))" % req)
        self.assertEqual(
            self.fix("(defun render-nav (request state) nil)",
                     "(render-nav %s '((\"sessions\" ((\"abc\" \"admin\")))) \"users\" '((\"admin\" \"pass\")))" % req),
            "(render-nav %s '((\"sessions\" ((\"abc\" \"admin\"))) (\"users\" ((\"admin\" \"pass\")))))" % req)

    def test_extra_arguments_that_are_not_tables_are_left_alone(self):
        req = "'(:method \"GET\")"
        call = "(render-nav %s '((\"sessions\" ())) \"users\" 5)" % req
        self.assertEqual(self.fix("(defun render-nav (request state) nil)", call), call)


def build(name, body, expect="T"):
    return {"action": "build", "name": name, "description": "d", "definition": "(defun %s (x) %s)" % (name, body),
            "tests": [{"call": "(%s 1)" % name, "expect": expect}], "call": "(%s 1)" % name}


class ReplyHandlingTests(unittest.TestCase):
    def _session(self, tmp, gen):
        return ag.Session("make f", gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                          worker_fn=lambda c: {"ok": True, "stdout": "", "error": "", "timed_out": False,
                                               "elapsed_ms": 1.0, "return_value": "(:GOT 9)"},
                          log_path=Path(tmp) / "l.jsonl")

    def test_a_reply_cut_off_at_the_token_limit_is_asked_again_not_patched_up(self):
        whole = ag._fake(build("f", "x"))
        cut = dict(whole, text=whole["text"][:whole["text"].index('"tests"')], finish_reason="length")
        replies = iter([cut, whole])
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._session(tmp, lambda s, u: next(replies))
            plan = sess._ask("p", "probe")
            self.assertEqual(len(plan["tests"]), 1)                       # the complete reply, with its tests
            retry = [e for e in sess.events if e["kind"] == "json_retry"][0]
            self.assertIn("cut off at the token limit", retry["reason"])
            self.assertEqual(sess.model_calls, 2)

    def test_a_plain_rewrite_does_not_sample_and_a_thinking_one_still_does(self):
        replies = iter([build("f", "(* %d x)" % i, expect="2") for i in range(2, 8)])
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._session(tmp, lambda s, u: ag._fake(next(replies)))
            sess.max_calls = 4
            sess.run()
            rewrites = [(e["deep"], e["temperature"]) for e in sess.events
                        if e["kind"] == "model_call" and e["label"] == "rewrite"]
            self.assertEqual(rewrites, [(False, 0.0), (True, ag.REWRITE_TEMPERATURE)])
        self.assertEqual(ag.JSON_RETRY_TEMPS[0], 0.0)

    def test_an_empty_reply_is_asked_again_at_temperature_zero(self):
        replies = iter([{"text": "", "finish_reason": "stop", "output_tokens": 1}, ag._fake({"action": "stop"})])
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._session(tmp, lambda s, u: next(replies))
            sess._ask("THE QUESTION", "rewrite", temperature=0.7)
            calls = [(e["label"], e["temperature"]) for e in sess.events if e["kind"] == "model_call"]
            self.assertEqual(calls, [("rewrite", 0.7), ("rewrite (retry: empty reply)", 0.0)])

    def test_a_definition_that_is_too_long_is_sent_back_with_the_reason(self):
        big = build("css-style", '"%s"' % ("a{b:c}" * 1300))
        problem = ag.validate_build(ag.normalize_plan(big))
        self.assertIn("characters long: keep it under %d" % ag.MAX_DEFINITION_CHARS, problem)
        self.assertIsNone(ag.validate_build(ag.normalize_plan(build("f", '"%s"' % ("a{b:c}" * 100)))))

    def test_a_doubled_percent_in_a_string_is_one_percent(self):
        plan = ag.normalize_plan(build("f", '(format nil "<div style=\\"width:100%%;height:50%\\">~a</div>" x)'))
        self.assertIn("width:100%;height:50%", plan["definition"])
        self.assertIn("doubled-percent", plan["auto_fixes"])


class CompileErrorTests(unittest.TestCase):
    SBCL = ('Execution of a form compiled with errors.\nForm:\n  (FORMATTER "<ul>~{<li>~a</li>~}~}</ul>")\n'
            'Compile-time error:\n  during macroexpansion of (FORMATTER "<ul>~{<li>~a</li>~}~}</ul>"). Use\n'
            '*BREAK-ON-SIGNALS* to intercept.\n\n error in FORMAT: No corresponding open brace\n'
            '  <ul>~{<li>~a</li>~}~}</ul>\n                      ^\n--- backtrace ---\n0: (X)')

    def test_the_cause_of_a_compile_error_is_what_is_reported(self):
        cause = ag.error_cause(self.SBCL)
        self.assertTrue(cause.startswith("does not compile: during macroexpansion"))
        self.assertIn("error in FORMAT: No corresponding open brace", cause)
        self.assertNotIn("BREAK-ON-SIGNALS", cause)
        self.assertNotIn("backtrace", cause)
        self.assertEqual(ag.error_cause("The variable X is unbound.\n--- backtrace ---\n0: (Y)"),
                         "The variable X is unbound.")
        self.assertEqual(ag.error_cause(""), "failed")

    def test_a_format_error_brings_its_own_advice(self):
        keys = [k for k, _ in orc.lisp_hints(ag.error_cause(self.SBCL))]
        self.assertIn("format-directive", keys)

    def test_the_cause_reaches_the_failure_detail_in_real_sbcl(self):
        bad = {"action": "build", "name": "page", "description": "d",
               "definition": '(defun page (xs) "list" (format nil "<ul>~{<li>~a</li>~}~}</ul>" xs))',
               "tests": [{"call": "(page '(1 2))", "expect": "T"}], "call": "(page '(1 2))"}
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", lambda s, u: ag._fake({"action": "stop"}),
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"), log_path=Path(tmp) / "l.jsonl")
            verdict = sess._rehearse(ag.normalize_plan(bad), "")
            ag.lispserver.close_all()
        self.assertFalse(verdict["ok"])
        self.assertIn("does not compile:", verdict["detail"])
        self.assertIn("No corresponding open brace", verdict["detail"])


if __name__ == "__main__":
    unittest.main()
