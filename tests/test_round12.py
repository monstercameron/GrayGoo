"""Stylesheet and pages must agree on class names.

Three prompts in a row ("make it responsive", "you regressed, fix the styling", "ffs fix
the styling", sessions 5a3ed6d9a8, 791da3e1fb, db090ead72) each rewrote the stylesheet
function alone, blind to the classes the pages use: it ended with 22 rules of which the
pages used almost none, and 20 page classes had no rule. Every test passed each time."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import visualcheck  # noqa: E402

PAGE = ('(defun render-login-page (request state) "login page" (html-page 200 (concatenate (quote string) '
        '"<style>" (css-style) "</style><div class=\\"login-card\\"><input class=\\"form-input\\">'
        '<button class=\\"btn\\">Go</button></div>")))')
ROUTER = '(defun handle-request (request state) "router" (render-login-page request state))'
OLD_PAGE = '(defun render-old (request state) "dead page" (html-page 200 "<p class=\\"ghost\\">x</p>"))'


def sheet(*classes):
    return '(defun css-style () "stylesheet" "body{margin:0}%s")' % "".join(".%s{padding:4px}" % c for c in classes)


def sheet_plan(*classes):
    return {"action": "build", "name": "css-style", "description": "stylesheet", "definition": sheet(*classes),
            "tests": [{"call": "(search \"body\" (css-style))", "expect": "T"}], "call": "(css-style)"}


def worker(code):
    if "prin1-to-string (getf resp :state)" in code:
        return {"ok": True, "elapsed_ms": 1.0, "return_value": '(200 NIL "<h1>ok</h1>" 0 "NIL")'}
    return {"ok": True, "stdout": "", "error": "", "timed_out": False, "elapsed_ms": 1.0, "return_value": "T"}


class Base(unittest.TestCase):
    def session(self, replies=(), saved_sheet=("btn",), prompt="simple crm website, fix the styling"):
        self.prompts, it = [], iter(replies)

        def gen(system, user):
            self.prompts.append(user)
            return ag._fake(next(it))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reg = ag.ToolRegistry(Path(tmp.name) / "t.json")
        for name, defn in (("css-style", sheet(*saved_sheet)), ("render-login-page", PAGE),
                           ("render-old", OLD_PAGE), ("handle-request", ROUTER)):
            reg.add({"name": name, "description": name, "definition": defn,
                     "tests": [{"call": "(search \"x\" \"x\")", "expect": "T"}]})
        sess = ag.Session(prompt, gen, registry=reg, worker_fn=worker, log_path=Path(tmp.name) / "l.jsonl")
        sess.visual, sess._app = False, True
        sess.max_calls = 10
        return sess


class StyleFactsTests(Base):
    def test_the_saved_code_says_which_used_classes_have_no_rule(self):
        facts = self.session()._style_facts()
        self.assertEqual(facts["sheet"], "css-style")
        self.assertEqual(facts["defined"], ["btn"])
        self.assertEqual(facts["used"], ["login-card", "form-input", "btn"])     # not "ghost": that page is dead
        self.assertEqual(facts["missing"], ["login-card", "form-input"])

    def test_no_facts_outside_a_web_app_and_nothing_missing_with_a_css_framework(self):
        sess = self.session()
        sess._app = False
        self.assertIsNone(sess._style_facts())
        sess = self.session()
        sess.registry.add({"name": "render-login-page", "description": "d", "definition": PAGE.replace(
            "<style>", "<script src=\\\"https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4\\\"></script><style>")})
        self.assertEqual(sess._style_facts()["missing"], [])

    def test_a_stylesheet_without_rules_for_the_pages_classes_is_not_saved(self):
        sess = self.session()
        problem = sess._style_problem(sheet_plan("card", "nav", "btn"))
        self.assertIn("no rule for 2 class(es) that the saved pages use: login-card, form-input.", problem)
        self.assertIsNone(sess._style_problem(sheet_plan("login-card", "form-input", "btn", "extra")))
        page = {"action": "build", "name": "render-home", "definition":
                '(defun render-home (request state) "p" (html-page 200 "<p class=\\"brand-new\\">x</p>"))'}
        self.assertIsNone(sess._style_problem(page))                    # pages are not held to it

    def test_the_planner_is_told_the_facts(self):
        sess = self.session([{"action": "stop"}])
        with mock.patch.object(ag.Session, "_run_steps", return_value=False):
            sess.run()
        plan_prompt = self.prompts[0]
        self.assertIn("STYLE FACTS, read from the saved code: the pages use the classes login-card, "
                      "form-input, btn.", plan_prompt)
        self.assertIn("The stylesheet function css-style defines btn.", plan_prompt)
        self.assertIn("These 2 used classes have NO rule and render unstyled now: login-card, form-input.",
                      plan_prompt)


class StyleFlowTests(Base):
    def test_a_stylesheet_rewritten_with_other_class_names_is_sent_back_until_it_covers_the_pages(self):
        fix = {"action": "edit", "name": "css-style",
               "edits": [{"old": ".btn{padding:4px}", "new": ".btn{padding:4px}.login-card{padding:9px}.form-input{padding:2px}"}]}
        sess = self.session([sheet_plan("glass", "hero", "btn"), fix])
        sess.run()
        verdicts = [e for e in sess.events if e["kind"] == "verdict"]
        self.assertFalse(verdicts[0]["ok"])
        self.assertIn("no rule for 2 class(es) that the saved pages use", verdicts[0]["detail"])
        self.assertIn("login-card, form-input", self.prompts[1])                   # the repair is told which
        saved = next(t for t in sess.registry.load() if t["name"] == "css-style")["definition"]
        self.assertTrue(all(c in visualcheck.css_classes(saved) for c in ("login-card", "form-input", "btn")))
        self.assertEqual(sess.state, "done", [e for e in sess.events if e["kind"] in ("error", "gave_up")])
        self.assertEqual(sess._style_facts()["missing"], [])
        # one changed function is still a change to the app: it is checked as a whole
        self.assertEqual([e["ok"] for e in sess.events if e["kind"] == "smoke"], [True])
        self.assertFalse(any(e["kind"] == "style_repair" for e in sess.events))     # nothing left to repair

    def test_after_a_build_the_stylesheet_is_brought_up_to_date_with_the_pages(self):
        sess = self.session([sheet_plan("btn", "login-card", "form-input")], saved_sheet=("btn",))
        self.assertTrue(sess._style_repair())
        ev = next(e for e in sess.events if e["kind"] == "style_repair")
        self.assertEqual((ev["sheet"], ev["missing"]), ("css-style", ["login-card", "form-input"]))
        step = self.prompts[0]
        self.assertIn("ADD a rule for each of these classes", step)
        self.assertIn("THE SAVED PAGES USE THESE CLASSES: login-card, form-input, btn.", step)
        self.assertEqual(sess._style_facts()["missing"], [])
        self.assertFalse(sess._style_repair())                                      # and then there is nothing to do

    def test_a_failed_repair_of_the_stylesheet_leaves_the_build_as_it_was(self):
        bad = sheet_plan("btn")                                                     # never covers the pages
        sess = self.session([bad, bad, bad, bad, {"action": "stop"}, {"action": "stop"}])
        sess._style_repair()
        self.assertEqual(sess.state, "running")
        self.assertEqual(sess._failed_steps, [])
        self.assertIn("css-style", sess._fix_failed)


if __name__ == "__main__":
    unittest.main()
