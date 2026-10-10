"""From the second CRM run (session 311858f386, "finish this crm and make sure it
works"): 34 calls, failed. Exact response expectations failed on incidental
details, the regression guard blocked the requested change, and one failed step
left the whole app unwired."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import webkit  # noqa: E402


def check(pairs):
    """Run ``(gg-check got 'want)`` for each pair in real SBCL; returns a list of 'T'/'F'."""
    body = " ".join("(if (eq t (gg-check %s '%s)) 'y 'n)" % (g, w) for g, w in pairs)
    env = ag._worker_fn("%s\n(list %s)" % (ag.GG_CHECK, body))
    assert env.get("ok"), env.get("error")
    return env["return_value"].strip("()").split()


class ResponseComparisonTests(unittest.TestCase):
    LOGIN = ("'(:status 303 :headers ((\"Location\" \"/\") (\"Set-Cookie\" \"sid=abc; HttpOnly; "
             "SameSite=Lax; Path=/\")) :body \"\" :state ((\"users\" ()) (\"sessions\" ((\"abc\" \"admin\")))))")

    def test_incidental_details_do_not_fail_a_response(self):
        got = check([
            # the live handle-login case: the model guessed other cookie attributes
            (self.LOGIN, "(:status 303 :headers ((\"Location\" \"/\") (\"Set-Cookie\" \"sid=abc; HttpOnly; Path=/\")) "
                         ":body \"\" :state ((\"users\" ()) (\"sessions\" ((\"abc\" \"admin\")))))"),
            # fewer headers expected than sent, no :state asserted
            (self.LOGIN, "(:status 303 :headers ((\"Location\" \"/\")))"),
            # "..." in an expected body is a wildcard
            ("'(:status 200 :headers ((\"Content-Type\" \"text/html; charset=utf-8\")) :body \"<html><h1>Login</h1><form>x</form></html>\")",
             "(:status 200 :body \"<html>...Login...</html>\")"),
            # unchanged state may be left out of the response
            ("'(:status 303 :headers ((\"Location\" \"/add\")) :body \"\")",
             "(:status 303 :headers ((\"Location\" \"/add\")) :body \"\" :state-unchanged ((\"products\" ())))"),
        ])
        self.assertEqual(got, ["Y", "Y", "Y", "Y"])

    def test_real_differences_still_fail(self):
        got = check([
            (self.LOGIN, "(:status 302 :headers ((\"Location\" \"/\")))"),                       # status
            (self.LOGIN, "(:status 303 :headers ((\"Location\" \"/login\")))"),                  # redirect target
            (self.LOGIN, "(:status 303 :headers ((\"Set-Cookie\" \"sid=zzz; Path=/\")))"),       # cookie value
            (self.LOGIN, "(:status 303 :state ((\"users\" ()) (\"sessions\" ())))"),             # state
            ("'(:status 200 :body \"<p>Goodbye</p>\")", "(:status 200 :body \"<p>...Hello...</p>\")"),
            ("'(:status 303 :body \"\" :state ((\"products\" ((\"x\")))))",
             "(:status 303 :body \"\" :state-unchanged ((\"products\" ())))"),                    # it DID change
            ("5", "(:status 200)"),                                                                # not a response
        ])
        self.assertEqual(got, ["N"] * 7)

    def test_an_expected_state_equal_to_the_input_is_marked_unchanged(self):
        call = ("(handle-add-product '(:method \"POST\" :form ((\"name\" \"\"))) "
                "'((\"products\" ()) (\"users\" ())))")
        same = "(:status 303 :headers ((\"Location\" \"/add\")) :body \"\" :state ((\"products\" ()) (\"users\" NIL)))"
        out = ag.unchanged_state(same, call, "handle-add-product", 1)
        self.assertIn(":state-unchanged", out)
        self.assertNotIn(":state ", out)
        changed = same.replace("((\"products\" ())", "((\"products\" ((\"a\")))")
        self.assertEqual(ag.unchanged_state(changed, call, "handle-add-product", 1), changed)
        self.assertEqual(ag.unchanged_state("T", call, "handle-add-product", 1), "T")

    def test_surplus_parens_are_trimmed_from_calls_and_expected_values(self):
        plan = ag.normalize_plan({
            "action": "build", "name": "f", "description": "d", "definition": "(defun f (x) x)",
            "call": "(f 1)", "tests": [{"call": "(let ((r (f 1))) (and (= r 1))))", "expect": "(1 2)))"}]})
        self.assertEqual(plan["tests"][0]["call"], "(let ((r (f 1))) (and (= r 1)))")
        self.assertEqual(plan["tests"][0]["expect"], "(1 2)")
        # a LET around the tool call is rebalanced as a whole; any other call is just trimmed
        self.assertIn("rebalanced-let-around-call", plan["auto_fixes"])
        direct = ag.normalize_plan({
            "action": "build", "name": "f", "description": "d", "definition": "(defun f (x) x)",
            "call": "(f 1)", "tests": [{"call": "(f 1)))", "expect": "1"}]})
        self.assertEqual(direct["tests"][0]["call"], "(f 1)")
        self.assertIn("trimmed-surplus-paren-in-call", direct["auto_fixes"])


def build(name, body, params="(request state)", call=None):
    call = call or "(%s '(:method \"GET\" :path \"/\") '((\"products\" ())))" % name
    return {"action": "build", "name": name, "description": "d",
            "definition": "(defun %s %s %s)" % (name, params, body),
            "tests": [{"call": call, "expect": "T"}], "call": call}


def worker(fail_names=()):
    def run(code):
        if "prin1-to-string (getf resp :state)" in code:
            return {"ok": True, "elapsed_ms": 1.0, "return_value": '(200 NIL "<h1>ok</h1>" 0 "NIL")'}
        last = code.rsplit("(defun ", 1)[-1].split(" ", 1)[0] if "(defun " in code else ""
        bad = any(("(gg-check (%s " % n) in code for n in fail_names)
        return {"ok": True, "stdout": "", "error": "", "timed_out": False, "elapsed_ms": 1.0,
                "return_value": "(:GOT 9)" if bad else "T"}
    return run


class PlanFlowTests(unittest.TestCase):
    def _session(self, tmp, replies, run, prior=()):
        it = iter(replies)
        self.prompts = []

        def gen(system, user):
            self.prompts.append(user)
            return ag._fake(next(it))
        reg = ag.ToolRegistry(Path(tmp) / "t.json")
        webkit.seed(reg)
        reg.add({"name": "handle-request", "description": "old router",
                 "definition": "(defun handle-request (request state) (html-page 200 \"old\"))",
                 "tests": [{"call": "(handle-request '(:method \"GET\" :path \"/\") '((\"products\" ())))",
                            "expect": "(:status 200 :body \"old\")"}]})
        sess = ag.Session("finish this crm and make sure it works", gen, registry=reg,
                          worker_fn=run, log_path=Path(tmp) / "l.jsonl")
        sess.prior_goals = list(prior)
        return sess, reg

    def test_a_failed_step_does_not_stop_the_rest_of_an_app_from_being_built(self):
        plan = {"action": "plan", "steps": [
            {"name": "render-home", "spec": "(render-home request state) -> page"},
            {"name": "broken-part", "spec": "(broken-part request state) -> page"},
            {"name": "handle-request", "spec": "(handle-request request state) routes to render-home"}]}
        bad = [build("broken-part", '(html-page 200 "%d")' % i) for i in range(4)]
        split_refused = {"action": "stop"}
        replies = [plan, build("render-home", '(html-page 200 "home")'),
                   bad[0], bad[1], bad[2], bad[3], {"action": "stop"}, split_refused,
                   build("handle-request", "(render-home request state)")]
        with tempfile.TemporaryDirectory() as tmp:
            sess, reg = self._session(tmp, replies, worker(["broken-part"]))
            sess.run()
            kinds = [e["kind"] for e in sess.events]
            self.assertIn("step_failed", kinds)
            names = [t["name"] for t in reg.load() if not t.get("kit")]
            self.assertIn("render-home", names)
            new_router = next(t for t in reg.load() if t["name"] == "handle-request")
            self.assertIn("render-home", new_router["definition"])           # the app WAS rewired
            self.assertEqual(next(e for e in sess.events if e["kind"] == "smoke")["ok"], True)
            self.assertEqual(sess.state, "failed")                           # and reported honestly
            summary = next(e for e in sess.events if e["kind"] == "summary")
            self.assertTrue(any("broken-part" in m for m in summary["missing_features"]))
            self.assertIn("could not be built: broken-part",
                          [e for e in sess.events if e["kind"] == "gave_up"][-1]["detail"])

    def test_replacing_a_function_is_allowed_when_its_caller_is_rebuilt_later_in_the_plan(self):
        plan = {"action": "plan", "steps": [
            {"name": "render-home", "spec": "(render-home request state) -> page"},
            {"name": "handle-request", "spec": "(handle-request request state) routes to render-home"}]}
        with tempfile.TemporaryDirectory() as tmp:
            sess, reg = self._session(tmp, [plan, build("render-home", '(html-page 200 "v2")'),
                                            build("handle-request", "(render-home request state)")],
                                      worker())
            reg.add({"name": "render-home", "description": "old page",
                     "definition": "(defun render-home (request state) (html-page 200 \"v1\"))"})
            reg.add({"name": "handle-request", "description": "old router",
                     "definition": "(defun handle-request (request state) (render-home request state))",
                     "tests": [{"call": "(handle-request '(:method \"GET\") '((\"products\" ())))",
                                "expect": "(:status 200 :body \"v1\")"}]})
            sess.run()
            classes = [e.get("class") for e in sess.events if e["kind"] == "verdict"]
            self.assertNotIn("REGRESSION", classes)
            self.assertNotIn("regression", [e.get("label") for e in sess.events if e["kind"] == "repl"])
            self.assertIn("v2", next(t for t in reg.load() if t["name"] == "render-home")["definition"])

    def test_a_follow_up_prompt_inherits_what_the_project_was_asked_for(self):
        plan = {"action": "plan", "steps": [
            {"name": "handle-request", "spec": "(handle-request request state) routes"}]}
        first = ("simple crm website using sqlite, product management pages, security , css styling, "
                 "and seeded products, prices and descriptions")
        with tempfile.TemporaryDirectory() as tmp:
            sess, reg = self._session(tmp, [plan, {"action": "stop"},
                                            build("handle-request", '(html-page 200 "x")')],
                                      worker(), prior=[first])
            sess.run()
            self.assertIn("EARLIER GOALS OF THIS PROJECT", self.prompts[0])
            self.assertIn("product management pages", self.prompts[0])
            cov = next(e for e in sess.events if e["kind"] == "coverage")
            self.assertIn("login", [f["key"] for f in cov["features"]])
            self.assertIn("YOUR PLAN LEAVES OUT", self.prompts[1])

    def test_the_manager_passes_earlier_prompts_of_the_project(self):
        import inspect
        self.assertIn("prior_goals", inspect.getsource(ag.SessionManager.start))


if __name__ == "__main__":
    unittest.main()
