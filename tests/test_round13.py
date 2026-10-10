"""The 15 pipeline issues appended to issues.md: budgets of one build, undoing a round of
fixes that made the app worse, checking a finished app by trying it, candidates that may
not touch functions they do not own, and a report of what was actually verified."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import visualcheck  # noqa: E402


def worker(code):
    if "prin1-to-string (getf resp :state)" in code:
        return {"ok": True, "elapsed_ms": 1.0, "return_value": '(200 NIL "<h1>ok</h1>" 0 "NIL")'}
    return {"ok": True, "stdout": "", "error": "", "timed_out": False, "elapsed_ms": 1.0, "return_value": "T"}


def build(name, body="x", params="(x)"):
    call = "(%s 1)" % name
    return {"action": "build", "name": name, "description": "d",
            "definition": "(defun %s %s %s)" % (name, params, body),
            "tests": [{"call": call, "expect": "T"}], "call": call}


class Base(unittest.TestCase):
    def session(self, replies=(), tools=(), prompt="make f", **kw):
        self.prompts, it = [], iter(replies)

        def gen(system, user):
            self.prompts.append(user)
            reply = next(it)
            return reply if "text" in reply else ag._fake(reply)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reg = ag.ToolRegistry(Path(tmp.name) / "t.json")
        for t in tools:
            reg.add(dict({"description": "d", "tests": []}, **t))
        sess = ag.Session(prompt, gen, registry=reg, worker_fn=worker,
                          log_path=Path(tmp.name) / "l.jsonl", **kw)
        sess.visual = False
        sess.shots_dir = Path(tmp.name) / "shots"
        return sess


class BudgetTests(Base):
    def test_a_build_stops_at_its_spend_limit_and_keeps_what_it_saved(self):
        paid = dict(ag._fake(build("f")), cost_usd=0.30)
        sess = self.session([paid, paid, paid])
        sess.max_usd, sess.max_calls = 0.50, 20
        sess._ask("p", "probe")
        sess._ask("p", "probe")                                    # $0.60 spent: over the limit now
        with self.assertRaises(ag.BudgetExhausted) as caught:
            sess._ask("p", "probe")
        self.assertEqual(caught.exception.limit, "the spend limit of $0.50 for one build")
        self.assertEqual(sess.model_calls, 2)                       # the third call was never made

    def test_a_build_stops_at_its_time_limit(self):
        sess = self.session([build("f")])
        sess.max_seconds, sess._t0 = 5, time.time() - 6
        with self.assertRaises(ag.BudgetExhausted) as caught:
            sess._ask("p", "probe")
        self.assertIn("time limit of 5 s", caught.exception.limit)

    def test_the_report_names_the_limit_that_was_reached(self):
        sess = self.session([dict(ag._fake({"action": "stop"}), cost_usd=0.9)])
        sess.max_usd = 0.10
        sess._ask("warm-up", "probe")
        sess.run()
        gave = next(e for e in sess.events if e["kind"] == "gave_up")
        self.assertTrue(gave["detail"].startswith("Stopped at the spend limit of $0.10 for one build."))
        self.assertEqual(sess.state, "failed")

    def test_defaults_and_the_call_limit_wording_are_kept(self):
        sess = self.session()
        self.assertEqual((sess.max_usd, sess.max_seconds), (ag.MAX_SESSION_USD, ag.MAX_SESSION_SECONDS))
        sess.max_calls = 0
        sess.run()
        self.assertIn("Stopped at the limit of 0 model calls.",
                      next(e for e in sess.events if e["kind"] == "gave_up")["detail"])


class RegistrySnapshotTests(Base):
    def test_a_snapshot_puts_the_registry_back_and_says_what_changed(self):
        sess = self.session(tools=[{"name": "a", "definition": "(defun a () 1)"},
                                   {"name": "b", "definition": "(defun b () 1)"}])
        reg = sess.registry
        snap = reg.snapshot()
        reg.add({"name": "a", "description": "d", "definition": "(defun a () 2)"})
        reg.add({"name": "c", "description": "d", "definition": "(defun c () 3)"})
        self.assertEqual(reg.restore(snap), ["a", "c"])
        self.assertEqual({t["name"]: t["definition"] for t in reg.load()},
                         {"a": "(defun a () 1)", "b": "(defun b () 1)"})
        self.assertEqual(reg.restore(snap), [])                      # nothing left to undo


class RelevanceTests(unittest.TestCase):
    def test_an_older_tool_the_call_concerns_keeps_its_description(self):
        def tool(name, desc):
            return {"name": name, "description": desc, "definition": "(defun %s (request state) nil)" % name,
                    "session": "abc", "tests": [{"call": "(%s '(:method \"GET\" :path \"/\" :cookies ((\"sid\" "
                                                         "\"abc\"))) '((\"products\" ((\"Widget\" \"10.00\")))))" % name,
                                                 "expect": "T"}]}
        tools = [tool("render-part-%d" % n, "Renders part %d of a page with all its markup" % n) for n in range(48)]
        tools.insert(3, tool("monthly-totals", "Computes invoice totals grouped by calendar month"))
        focus = "(render-report request state) shows the invoice totals for every calendar month"
        text, level = ag.compact_registry(tools, focus, ag.REGISTRY_BUDGET)
        self.assertEqual(level, 1)
        self.assertIn("monthly-totals (request state): Computes invoice totals grouped by calendar month", text)
        self.assertIn("render-part-7 (request state)", text)
        self.assertNotIn("render-part-7 (request state):", text)      # unrelated ones stay bare


ROUTER = {"name": "handle-request", "definition": "(defun handle-request (request state) (list :status 200 :body \"x\"))"}


class AppCheckTests(Base):
    def test_a_finished_app_is_tried_and_its_functions_compared(self):
        sess = self.session(tools=[ROUTER])
        sess._app = True
        sess._smoke()
        kinds = [e["kind"] for e in sess.events]
        self.assertIn("acceptance", kinds)
        self.assertIn("interface_check", kinds)
        acc = next(e for e in sess.events if e["kind"] == "acceptance")
        self.assertEqual(next(r for r in acc["results"] if r["id"] == "front-page")["ok"], True)
        v = sess._summary()["verification"]
        self.assertEqual((v["app_answers"], v["interfaces"], v["screens"], v["model_said_done"]),
                         (True, 0, None, None))
        self.assertEqual(v["scenarios"]["failed"], 0)

    def test_nothing_is_claimed_for_levels_that_did_not_run(self):
        v = self.session()._summary()["verification"]
        self.assertEqual((v["function_tests"], v["app_answers"], v["scenarios"], v["interfaces"],
                          v["style_missing"], v["screens"]), (0, None, None, None, None, None))

    def test_a_feature_that_is_named_but_does_not_work_is_reported_missing(self):
        sess = self.session(tools=[ROUTER, {"name": "handle-login", "definition":
                                            "(defun handle-login (request state) \"login security\" nil)"}],
                            prompt="simple crm website with login security")
        sess._app = True
        sess._features = ag.goalcheck.goal_features(sess.prompt)
        self.assertTrue(any(f["key"] == "login" for f in sess._features))
        self.assertEqual(sess._missing_now(), [])                    # named in the code: looks covered
        sess._accept = {"results": [{"id": "login-accepts-user", "label": "a user can sign in", "ok": False,
                                     "detail": "signing in as demo did not get past the login page"},
                                    {"id": "login-rejects-wrong-password", "label": "x", "ok": True, "detail": ""}],
                        "passed": 1, "failed": 1, "skipped": 0, "failed_labels": ["a user can sign in"]}
        missing = sess._missing_now()
        self.assertEqual(len(missing), 1)
        self.assertIn("trying the app showed it does not work", missing[0])


class GuardedFixTests(Base):
    PLAN = {"action": "plan", "steps": [{"name": "page", "spec": "(page x) -> page"}]}

    def _app(self, replies):
        # the router calls page: a planned function nothing calls would fail the build's proof of doneness
        router = {"name": "handle-request", "definition":
                  "(defun handle-request (request state) (page 1) (list :status 200 :body \"x\"))"}
        sess = self.session(replies, tools=[router, {"name": "page", "definition": "(defun page (x) :old)"}])
        sess._app, sess.max_calls = True, 20
        sess._smoke()
        return sess

    def test_a_round_of_fixes_that_breaks_a_passing_check_is_undone(self):
        sess = self._app([build("page", ":new")])
        real = ag.acceptance.run_scenarios
        calls = []

        def scenarios(app, limit_forms=3):
            calls.append(1)
            out = real(app, limit_forms)
            if len(calls) == 1:                                     # the first check after the fix round
                out.append({"id": "create-item", "label": "an item can be added", "ok": False,
                            "detail": "the added item did not appear"})
            return out
        with mock.patch.object(ag.acceptance, "run_scenarios", scenarios):
            kept = sess._guarded_fix(self.PLAN, "fixes")
        self.assertFalse(kept)
        self.assertIn(":old", next(t for t in sess.registry.load() if t["name"] == "page")["definition"])
        back = next(e for e in sess.events if e["kind"] == "visual_rollback")
        self.assertIn("1 check(s) that passed before the fixes now fail: an item can be added", back["reason"])
        self.assertEqual(back["restored"], ["page"])
        self.assertNotIn("page", [n for n, _ in sess._built])       # it is not reported as built
        self.assertEqual(sess._app_score(), (True, 0))              # and the checks describe the old app again

    def test_a_round_of_fixes_that_harms_nothing_is_kept(self):
        sess = self._app([build("page", ":new")])
        self.assertTrue(sess._guarded_fix(self.PLAN, "fixes"))
        self.assertIn(":new", next(t for t in sess.registry.load() if t["name"] == "page")["definition"])
        self.assertFalse(any(e["kind"] == "visual_rollback" for e in sess.events))

    def test_visual_fixes_that_make_the_pictures_worse_are_undone(self):
        png = bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"x"

        def capture(html, out):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(png)
            return {"ok": True, "path": str(out), "error": "", "ms": 1.0}
        sess = self._app([{"done": False, "problems": ["the page is plain"], "fix": ""}, self.PLAN,
                          build("page", ":new"),
                          {"done": False, "problems": ["the page is plain", "the table is gone", "no title"], "fix": ""}])
        sess.visual, sess.capture_fn = True, capture
        pages = [{"label": "GET /", "path": "/", "status": 200, "html": "<h1>x</h1>"}]
        with mock.patch.object(visualcheck, "collect_pages", lambda app, limit=3: list(pages)):
            sess._visual_review()
        self.assertIn(":old", next(t for t in sess.registry.load() if t["name"] == "page")["definition"])
        back = next(e for e in sess.events if e["kind"] == "visual_rollback")
        self.assertIn("3 problem(s) after the fixes and 1 before", back["reason"])
        self.assertEqual(sess._summary()["visual"]["problems"], ["the page is plain"])   # the verdict of what is saved

    def test_what_the_free_checks_found_broken_gets_one_round_of_fixes(self):
        sess = self._app([self.PLAN, build("page", ":fixed")])
        sess._accept = {"results": [{"id": "create-item", "label": "an item can be added", "ok": False,
                                     "detail": "the added item did not appear"}],
                        "passed": 1, "failed": 1, "skipped": 0, "failed_labels": ["an item can be added"]}
        sess._behaviour_fix()
        self.assertIn("THE FINISHED APP WAS TRIED AND THESE CHECKS FAILED: an item can be added "
                      "(the added item did not appear).", self.prompts[0])
        self.assertIn("THE APP WAS TRIED AND THESE CHECKS FAILED: an item can be added",
                      self.prompts[1])                                # the step is told too
        self.assertIn(":fixed", next(t for t in sess.registry.load() if t["name"] == "page")["definition"])
        calls = sess.model_calls
        sess._behaviour_fix()                                         # once per build
        self.assertEqual(sess.model_calls, calls)


class PhoneShotTests(Base):
    def _look(self, prompt):
        sizes = []

        def capture(html, out, width=None, height=None):
            sizes.append((width, height))
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"x")
            return {"ok": True, "path": str(out), "error": "", "ms": 1.0}
        sess = self.session([{"done": True, "problems": [], "fix": ""}], tools=[ROUTER], prompt=prompt)
        sess._app, sess.visual, sess.capture_fn = True, True, capture
        pages = [{"label": "GET /", "path": "/", "status": 200, "html": "<h1>x</h1>"},
                 {"label": "GET /login", "path": "/login", "status": 200, "html": "<h1>y</h1>"}]
        with mock.patch.object(visualcheck, "collect_pages", lambda app, limit=3: list(pages)):
            sess._visual_review()
        return sess, sizes

    def test_a_goal_that_asks_for_a_phone_layout_is_also_shown_at_phone_width(self):
        sess, sizes = self._look("simple crm website, responsive on mobile")
        self.assertEqual(sorted(sizes, key=str), sorted([(None, None), (None, None), (390, 844)], key=str))
        self.assertIn("3. GET / at phone width (390 px)", self.prompts[0])
        self.assertEqual(len(sess._visual["shots"]), 3)

    def test_any_other_goal_is_shown_at_desktop_width_only(self):
        _, sizes = self._look("simple crm website with a telephone directory")
        self.assertEqual(sizes, [(None, None), (None, None)])


class ReaderTests(unittest.TestCase):
    def test_a_string_that_reads_quote_marker_is_a_string(self):
        import s_expr
        self.assertEqual(s_expr.parse('(f "quote-marker" \'x)'), ["f", "quote-marker", ["quote", "x"]])


class FitTests(Base):
    def test_a_candidate_may_not_touch_functions_it_does_not_own(self):
        sess = self.session(tools=[{"name": "helper", "definition": "(defun helper (x) x)"}])
        bad = build("f", "(progn (setf (symbol-function 'helper) (lambda (y) y)) x)")
        self.assertIn("not allowed beside the harness's own functions", sess._fit_problem(bad))
        self.assertIsNone(sess._fit_problem(build("f", "(helper x)")))

    def test_a_call_with_the_wrong_number_of_arguments_is_caught_before_anything_runs(self):
        sess = self.session(tools=[{"name": "find-user", "definition": "(defun find-user (name password state) nil)"}])
        problem = sess._fit_problem(build("f", "(find-user x x)"))
        self.assertIn("find-user", problem)
        self.assertIn("3", problem)
        self.assertIsNone(sess._fit_problem(build("f", "(find-user x x x)")))

    def test_the_planner_is_told_about_mismatches_in_the_saved_code(self):
        tools = [{"name": "handle-request", "definition":
                  "(defun handle-request (request state) (if (string= (getf request :path) \"/\") "
                  "(render-home request state) (html-page 404 \"x\")))"},
                 {"name": "render-home", "definition":
                  "(defun render-home (request state) (html-page 200 \"<form method=\\\"POST\\\" "
                  "action=\\\"/add-product\\\"></form>\"))"}]
        sess = self.session([{"action": "stop"}], tools=tools, prompt="simple crm website, add products")
        with mock.patch.object(ag.Session, "_run_steps", return_value=False):
            sess.run()
        self.assertIn("INTERFACE FACTS, read from the saved code", self.prompts[0])
        self.assertIn("/add-product", self.prompts[0])


class NearMissBuildTests(unittest.TestCase):
    """Live build 44bdfb6d97: a correct monthly-payment was lost to a hand-computed test value."""

    DEFINITION = ('(defun monthly-payment (principal annual-rate years) '
                  '"Monthly payment of a fixed-rate loan." '
                  '(let* ((r (/ annual-rate 1200.0)) (n (* years 12))) '
                  '(if (zerop r) (/ principal n) (/ (* principal r) (- 1 (expt (+ 1 r) (- n)))))))')

    def test_a_correct_function_survives_a_wrong_hand_computed_expectation(self):
        first = {"action": "build", "name": "monthly-payment", "description": "Monthly payment of a loan",
                 "definition": self.DEFINITION, "call": "(monthly-payment 150000 6.5 30)",
                 "tests": [{"call": "(monthly-payment 150000 6.5 30)", "expect": "951.12"},
                           {"call": "(monthly-payment 100000 5.0 15)", "expect": "805.40"}]}
        second = {"action": "build", "name": "monthly-payment", "tests": [
            {"call": "(let ((p (monthly-payment 150000 6.5 30))) (and (floatp p) (< 900 p 1000)))", "expect": "T"},
            {"call": "(< (monthly-payment 100000 5.0 30) (monthly-payment 100000 5.0 15))", "expect": "T"}]}
        replies, prompts = iter([first, second]), []

        def gen(system, user):
            prompts.append(user)
            return ag._fake(next(replies))
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("monthly payment of a 150000 loan at 6.5 percent over 30 years", gen,
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"), log_path=Path(tmp) / "l.jsonl")
            sess.run()
            kinds = [e["kind"] for e in sess.events]
            first_verdict = next(e for e in sess.events if e["kind"] == "verdict")
            self.assertEqual(first_verdict["class"], "TEST_WRONG")     # the test is blamed, not the code
            self.assertIn("rescue", kinds)
            self.assertNotIn("repair", kinds)                          # the code was never sent back
            self.assertEqual(sess.model_calls, 2)
            saved = next(t for t in sess.registry.load() if t["name"] == "monthly-payment")
            self.assertEqual(" ".join(saved["definition"].split()), " ".join(self.DEFINITION.split()))
            self.assertEqual(sess.state, "done")
            self.assertIn("The definition stays EXACTLY as it is", prompts[1])


class ContractTests(unittest.TestCase):
    def test_the_contract_asks_for_hashed_passwords_and_a_demo_account(self):
        for phrase in ("never stored or compared as plain text", "hash-password", "password-matches-p",
                       "one user named demo whose password is demo"):
            self.assertIn(phrase, ag.WEB_APP_CONTRACT_LOGIN)     # the password sentences are for login goals only
        self.assertEqual(visualcheck.DEMO_USER, ("demo", "demo"))


if __name__ == "__main__":
    unittest.main()
