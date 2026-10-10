"""From the CRM run (session 55bc077457): the build "succeeded" with no login, no
forms, an unused stylesheet and unescaped HTML. These tests cover the checks that
now catch that, and the speed-ups added in the same round."""
import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import lispserver  # noqa: E402
import webkit  # noqa: E402

CRM = ("simple crm website using sqlite, product management pages, security , css styling, "
       "and seeded products, prices and descriptions")


def build(name, body, call=None, expect="T", params="(request state)", description="d"):
    call = call or "(%s '(:method \"GET\" :path \"/\") '((\"products\" ())))" % name
    return {"action": "build", "name": name, "description": description,
            "definition": "(defun %s %s %s)" % (name, params, body),
            "tests": [{"call": call, "expect": expect}], "call": call}


def app_worker(code):
    """Fake SBCL: tests pass; the smoke request gets a well-formed response."""
    if "prin1-to-string (getf resp :state)" in code:
        return {"ok": True, "elapsed_ms": 1.0, "return_value": '(200 NIL "<h1>CRM</h1>" 0 "NIL")'}
    return {"ok": True, "stdout": "", "return_value": "T", "error": "", "timed_out": False,
            "elapsed_ms": 1.0}


class PropertyAndLintTests(unittest.TestCase):
    def test_expect_t_accepts_any_true_value_and_still_rejects_nil(self):
        env = ag._worker_fn(ag.GG_CHECK + "\n(list (gg-check (search \"b\" \"abc\") 't) "
                            "(gg-check nil 't) (gg-check 3 '3) (gg-check 3 '4))")
        self.assertTrue(env.get("ok"), env.get("error"))
        self.assertEqual(ag.one_line(env["return_value"]), "(T (:GOT NIL) T (:GOT 3))")

    def test_the_live_unescaped_row_function_is_refused_and_the_fixed_one_passes(self):
        bad = {"action": "build", "name": "render-product-row", "description": "row",
               "definition": "(defun render-product-row (row) (format nil \"<tr><td>~a</td></tr>\" "
                             "(first row)))",
               "tests": [{"call": "(render-product-row '(\"W\"))", "expect": "\"<tr><td>W</td></tr>\""}],
               "call": "(render-product-row '(\"W\"))"}
        self.assertIn("unescaped text in HTML", ag.validate_build(ag.normalize_plan(dict(bad))))
        good = dict(bad, definition=bad["definition"].replace("(first row)", "(html-escape (first row))"))
        self.assertIsNone(ag.validate_build(ag.normalize_plan(good)))

    def test_the_contract_asks_for_forms_escaping_and_property_tests(self):
        for needle in ("(html-escape ...)", "form to add one", "refuses empty required fields",
                       "never expect a whole page"):
            self.assertIn(needle, ag.WEB_APP_CONTRACT)


class CoverageFlowTests(unittest.TestCase):
    def _run(self, replies, worker=app_worker):
        prompts = []
        it = iter(replies)

        def gen(system, user):
            prompts.append(user)
            return ag._fake(next(it))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reg = ag.ToolRegistry(Path(tmp.name) / "t.json")
        sess = ag.Session(CRM, gen, registry=reg, worker_fn=worker,
                          log_path=Path(tmp.name) / "l.jsonl")
        sess.run()
        return sess, prompts, reg

    PARTIAL = {"action": "plan", "steps": [
        {"name": "css-style", "spec": "(css-style) -> css string"},
        {"name": "render-products-page", "spec": "(render-products-page state) -> page listing products"},
        {"name": "handle-request", "spec": "(handle-request request state) routes GET / to render-products-page"}]}

    def test_a_plan_that_skips_features_is_sent_back_once_with_what_is_missing(self):
        fuller = {"action": "plan", "steps": self.PARTIAL["steps"][:2] + [
            {"name": "handle-login", "spec": "(handle-login request state) login form and session"},
            {"name": "handle-add-product", "spec": "(handle-add-product request state) add form + POST"},
            {"name": "handle-edit-product", "spec": "(handle-edit-product request state) edit form"},
            {"name": "handle-delete-product", "spec": "(handle-delete-product request state) delete"},
            {"name": "initial-state", "spec": "(initial-state) three seeded products"},
            self.PARTIAL["steps"][2]]}
        steps = [build(s["name"], '(html-page 200 "x")',
                       params="()" if s["name"] in ("css-style", "initial-state") else
                       ("(state)" if s["name"] == "render-products-page" else "(request state)"),
                       call="(%s)" % s["name"] if s["name"] in ("css-style", "initial-state") else
                       ("(render-products-page '((\"products\" ())))" if s["name"] == "render-products-page" else None))
                 for s in fuller["steps"]]
        sess, prompts, reg = self._run([self.PARTIAL, fuller] + steps)
        self.assertIn("THE GOAL ASKS FOR ALL OF THESE", prompts[0])
        self.assertIn("up to 10 steps", prompts[0])
        self.assertIn("YOUR PLAN LEAVES OUT", prompts[1])
        self.assertIn("login", prompts[1])
        cov = next(e for e in sess.events if e["kind"] == "coverage")
        self.assertTrue(cov["extended"])
        self.assertEqual(cov["missing"], [])
        self.assertEqual(len(next(e for e in sess.events if e["kind"] == "plan")["steps"]), 8)
        self.assertEqual(sess.state, "done", [e for e in sess.events if e["kind"] in ("gave_up", "error")])

    def test_the_finished_app_is_checked_without_a_model_call_and_gaps_are_reported(self):
        steps = [build("css-style", '"body{}"', params="()", call="(css-style)", expect='"body{}"'),
                 build("render-products-page", '(html-page 200 "x")', params="(state)",
                       call="(render-products-page '((\"products\" ())))"),
                 build("handle-request", "(render-products-page state)")]
        # the planner refuses to extend: the run goes on, and the summary says what is missing
        sess, prompts, reg = self._run([self.PARTIAL, {"action": "stop"}] + steps)
        labels = [e["label"] for e in sess.events if e["kind"] == "model_call"]
        self.assertNotIn("final", labels)
        smoke = next(e for e in sess.events if e["kind"] == "smoke")
        self.assertEqual((smoke["call"], smoke["ok"], smoke["status"]), ("GET /", True, 200))
        summary = next(e for e in sess.events if e["kind"] == "summary")
        self.assertEqual(summary["smoke"]["ok"], True)
        self.assertTrue(any("login" in m for m in summary["missing_features"]))
        self.assertEqual(summary["unused_functions"], ["css-style"])
        self.assertEqual(next(e for e in sess.events if e["kind"] == "result")["value"], "<h1>CRM</h1>")

    def test_an_app_that_fails_its_own_check_is_not_reported_as_success(self):
        def worker(code):
            if "prin1-to-string (getf resp :state)" in code:
                return {"ok": False, "error": "The value 5 is not of type LIST", "elapsed_ms": 1.0,
                        "return_value": ""}
            return app_worker(code)
        steps = [build("css-style", '"body{}"', params="()", call="(css-style)", expect='"body{}"'),
                 build("render-products-page", '(html-page 200 "x")', params="(state)",
                       call="(render-products-page '((\"products\" ())))"),
                 build("handle-request", "(render-products-page state)")]
        sess, _, _ = self._run([self.PARTIAL, {"action": "stop"}] + steps, worker)
        self.assertEqual(sess.state, "failed")
        self.assertIn("failed its own check", next(e for e in sess.events if e["kind"] == "gave_up")["detail"])


class FinalCallRepairTests(unittest.TestCase):
    def test_the_answer_call_gets_the_state_shape_repair_too(self):
        seen = []

        def worker(code):
            seen.append(code)
            return {"ok": True, "stdout": "", "return_value": "1", "error": "", "timed_out": False,
                    "elapsed_ms": 1.0}
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            reg.add({"name": "count-rows", "description": "count the rows of a table in the state",
                     "definition": "(defun count-rows (state name) (length (table-rows state name)))"})
            replies = iter([{"action": "use", "call": "(count-rows '(\"posts\" ((\"a\"))) \"posts\")"}] * 2)
            sess = ag.Session("count rows in posts", lambda s, u: ag._fake(next(replies)), registry=reg,
                              worker_fn=worker, log_path=Path(tmp) / "l.jsonl")
            sess.run()
            self.assertEqual(sess.state, "done", sess.events[-3:])
            self.assertIn("(count-rows '((\"posts\" ((\"a\")))) \"posts\")", seen[-1])


class DraftAheadTests(unittest.TestCase):
    def test_leaf_steps_are_drafted_together_and_dependents_wait(self):
        plan = {"action": "plan", "steps": [
            {"name": "double", "spec": "(double x) -> twice x"},
            {"name": "triple", "spec": "(triple x) -> three times x"},
            {"name": "both", "spec": "(both x) -> (list (double x) (triple x))"}]}
        bodies = {"double": "(* 2 x)", "triple": "(* 3 x)", "both": "(list (double x) (triple x))"}
        started = []

        def gen(system, user):
            if "BUILD exactly this one" in user:
                name = next(n for n in ("double", "triple", "both") if "GOAL: (%s " % n in user)
                started.append(name)
                time.sleep(0.15)
                return ag._fake(build(name, bodies[name], params="(x)", call="(%s 1)" % name))
            if "All planned helper tools" in user:
                return ag._fake({"action": "use", "call": "(both 1)"})
            return ag._fake(plan)
        real = ag.live_generate
        ag.live_generate = gen                          # the draft-ahead path is live-model only
        self.addCleanup(setattr, ag, "live_generate", real)
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("make both", gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              worker_fn=app_worker, log_path=Path(tmp) / "l.jsonl")
            sess.parallel = 1                           # in order: drafts ahead instead of lanes
            t0 = time.perf_counter()
            sess.run()
            took = time.perf_counter() - t0
            self.assertEqual(sess.state, "done", [e for e in sess.events if e["kind"] in ("gave_up", "error")])
            calls = {e["prompt"].split("GOAL: (")[1].split(" ")[0]: e.get("ahead")
                     for e in sess.events if e["kind"] == "model_call" and e["label"] == "step"}
            self.assertEqual(calls, {"double": True, "triple": True, "both": False})
            self.assertEqual(sorted(started[:2]), ["double", "triple"])     # both leaves began first
            self.assertLess(took, 0.15 * 3 + 0.25)                          # not three calls in a row
            self.assertEqual(sess.model_calls, 5)                           # plan + 3 steps + final


class WarmRehearsalTests(unittest.TestCase):
    """Real SBCL: tests in the warm process give the same verdicts, faster, and leave nothing behind."""

    def _verdicts(self, fast):
        replies = iter([
            build("sq", "(* x x)", params="(x)", call="(sq 3)", expect="9"),
            build("cube", "(* x (sq x)", params="(x)", call="(cube 2)", expect="8"),      # paren is completed
            build("bad-fn", "(car x)", params="(x)", call="(bad-fn 5)", expect="5"),
            {"action": "stop"}])
        old = ag.FAST_REHEARSAL
        ag.FAST_REHEARSAL = fast
        self.addCleanup(setattr, ag, "FAST_REHEARSAL", old)
        out = []
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            for prompt in ("square 3", "cube 2", "bad"):
                sess = ag.Session(prompt, lambda s, u: ag._fake(next(replies)), registry=reg,
                                  log_path=Path(tmp) / ("%s.jsonl" % prompt.split()[0]))
                sess.max_calls = 1
                t0 = time.perf_counter()
                sess.run()
                out.append(([(e.get("ok"), e.get("class")) for e in sess.events if e["kind"] == "verdict"],
                            [e.get("value") for e in sess.events if e["kind"] == "result"],
                            time.perf_counter() - t0))
            base = "%s\n%s" % (ag.GG_CHECK, reg.prelude())
            key = "rehearse:" + hashlib.sha256(base.encode("utf-8")).hexdigest()
            leftover = lispserver.cached_server(key, base).eval("(fboundp 'bad-fn)") if fast else None
        return out, leftover

    def test_same_verdicts_as_fresh_processes_and_no_leftover_candidate(self):
        fresh, _ = self._verdicts(False)
        warm, leftover = self._verdicts(True)
        self.assertEqual([r[:2] for r in warm], [r[:2] for r in fresh])
        self.assertEqual(warm[0][0], [(True, None)])
        self.assertEqual(warm[2][0][0][0], False)
        self.assertEqual(leftover["return_value"], "NIL")
        print("\nrehearsal seconds fresh %.2f, warm %.2f"
              % (sum(r[2] for r in fresh), sum(r[2] for r in warm)))

    def tearDown(self):
        lispserver.close_all()


class ToolsApiTests(unittest.TestCase):
    def test_unused_functions_are_flagged_in_the_tools_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = ag.SessionManager(ag.ToolRegistry(Path(tmp) / "tools.json"))
            webkit.seed(mgr.registry)
            mgr.registry.add({"name": "css-style", "description": "css",
                              "definition": '(defun css-style () "body{}")'})
            mgr.registry.add({"name": "handle-request", "description": "app",
                              "definition": '(defun handle-request (request state) (html-page 200 "x"))'})
            meta = {t["name"]: t["meta"] for t in mgr.tools()}
            self.assertTrue(meta["css-style"].get("unused"))
            self.assertFalse(meta["handle-request"].get("unused"))
            self.assertFalse(meta["html-page"].get("unused"))


if __name__ == "__main__":
    unittest.main()
