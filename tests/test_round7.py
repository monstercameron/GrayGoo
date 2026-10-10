"""From the first live run with lanes and thinking (sessions d6bf2118d8, ef1821fd41,
e9ffff6f9e): all four builds succeeded, but 10 verdicts failed on a test call one
paren short after nested state data, three thinking calls spent 8000 tokens on that
and answered nothing, and four plain rewrites came back empty."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402

DEFN = "(defun handle-edit-product (request state) (list :status 303 :state state))"
# verbatim from the log: 25 opening, 24 closing; the ")" is missing after the state data
LOGGED = ("(let ((r (handle-edit-product '(:method \"POST\" :path \"/edit\" :form ((\"name\" \"Widget\") "
          "(\"price\" \"15.00\") (\"description\" \"Updated\")) :cookies ((\"sid\" \"abc\"))) "
          "'((\"products\" ((\"Widget\" \"10.00\" \"A useful widget\"))) (\"users\" ()) "
          "(\"sessions\" ((\"abc\" \"admin\")))))) (and (= (getf r :status) 303) "
          "(search \"15.00\" (getf r :state))))")
GOOD = LOGGED.replace("\"admin\")))))) (and", "\"admin\"))))))) (and")


class LetCallTests(unittest.TestCase):
    def test_the_logged_call_is_rebalanced_into_the_intended_form(self):
        self.assertNotEqual(ag._paren_depth(LOGGED), 0)
        fixed = ag.balance_let_call(LOGGED, DEFN)
        self.assertEqual(" ".join(fixed.split()), " ".join(GOOD.split()))
        form = ag.s_expr.parse(fixed)
        self.assertEqual(len(form[1]), 1)                       # one binding, not two
        self.assertEqual(len(form[1][0][1]), 3)                 # the tool with its two arguments
        self.assertEqual(str(form[2][0]).lower(), "and")        # the body is the check

    def test_surplus_parens_after_the_arguments_are_dropped_too(self):
        extra = GOOD.replace("\"admin\"))))))) (and", "\"admin\"))))))))) (and")
        self.assertEqual(" ".join(ag.balance_let_call(extra, DEFN).split()), " ".join(GOOD.split()))

    def test_a_paren_in_a_string_is_not_counted(self):
        defn = "(defun page (request state) state)"
        call = "(let ((r (page '(:path \"/a)\") '((\"t\" ())))) (search \"(\" (getf r :body)))"
        fixed = ag.balance_let_call(call, defn)
        self.assertEqual(fixed, "(let ((r (page '(:path \"/a)\") '((\"t\" ()))))) (search \"(\" (getf r :body)))")
        self.assertEqual(ag._paren_depth(fixed), 0)

    def test_balanced_and_other_shapes_are_left_alone(self):
        two = "(let ((r (handle-edit-product '(:a 1) '((\"t\" ())))) (x 1)) (list r x))"
        for call in (GOOD, two, "(handle-edit-product '(:a 1) '((\"t\" ()))",
                     "(let ((r (other-tool '(:a 1) '((\"t\" ()))) (and r))",
                     "(let ((r (handle-edit-product '(:a 1)))) (and r"):   # too few arguments
            self.assertEqual(ag.balance_let_call(call, DEFN), call, call)
        self.assertEqual(ag.balance_let_call(LOGGED, "(defun handle-edit-product (a &rest b) a)"), LOGGED)

    def test_a_build_with_the_logged_call_is_fixed_before_it_is_judged(self):
        plan = ag.normalize_plan({"action": "build", "name": "handle-edit-product", "description": "d",
                                  "definition": DEFN, "call": LOGGED,
                                  "tests": [{"call": LOGGED, "expect": "T"}]})
        self.assertIn("rebalanced-let-around-call", plan["auto_fixes"])
        self.assertEqual(ag._paren_depth(plan["tests"][0]["call"]), 0)
        self.assertIsNone(ag.validate_build(plan))


def build(name, body, call=None, expect="T"):
    call = call or "(%s 1)" % name
    return {"action": "build", "name": name, "description": "d",
            "definition": "(defun %s (x) %s)" % (name, body),
            "tests": [{"call": call, "expect": expect}], "call": "(%s 1)" % name}


def worker(value="T"):
    return lambda code: {"ok": True, "stdout": "", "error": "", "timed_out": False,
                         "elapsed_ms": 1.0, "return_value": value}


class Run:
    def __init__(self, replies, value="T", prompt="make f"):
        self.prompts, it = [], iter(replies)

        def gen(system, user):
            self.prompts.append((system, user))
            return ag._fake(next(it))
        self.tmp = tempfile.TemporaryDirectory()
        self.sess = ag.Session(prompt, gen, registry=ag.ToolRegistry(Path(self.tmp.name) / "t.json"),
                               worker_fn=worker(value), log_path=Path(self.tmp.name) / "l.jsonl")
        self.sess.max_calls = 8
        self.sess.run()
        self.calls = [e for e in self.sess.events if e["kind"] == "model_call"]
        self.tools = self.sess.registry.load()
        self.tmp.cleanup()


class RepairRoutingTests(unittest.TestCase):
    def test_a_broken_test_call_keeps_the_code_and_repairs_only_the_tests(self):
        bad = build("f", "(* 2 x)", call="(f ((((1)")                 # not fixable by the normaliser
        good_tests = dict(build("f", "(+ x 99)"), tests=[{"call": "(f 1)", "expect": "T"}])
        run = Run([bad, good_tests])
        verdict = next(e for e in run.sess.events if e["kind"] == "verdict")
        self.assertEqual(verdict["class"], "TEST_CALL_INVALID")
        self.assertEqual([c["label"] for c in run.calls], ["plan", "test-calls"])
        self.assertEqual(run.prompts[1][0], ag.TEST_SYSTEM)
        self.assertIn("instead of wrapping the call in LET", run.prompts[1][1])
        self.assertIn("(:status 200 :body \"...text...\")", run.prompts[1][1])
        saved = run.tools[0]
        self.assertIn("(* 2 x)", saved["definition"])                # the first definition was kept
        self.assertEqual(run.sess.state, "done")

    def test_thinking_is_not_spent_on_problems_that_are_not_about_the_code(self):
        bad = [build("f", "(* %d x)" % i, call="(f ((((1)") for i in range(2, 7)]
        run = Run(bad + [{"action": "stop"}])
        self.assertFalse(any(c["deep"] for c in run.calls), [(c["label"], c["deep"]) for c in run.calls])

    def test_a_wrong_result_still_gets_a_thinking_last_rewrite_with_the_smaller_budget(self):
        seen = []
        real = ag.Session._generate

        def spy(self, system, text):
            seen.append((getattr(ag._TEMP, "deep", False), getattr(ag._TEMP, "think_tokens", None)))
            return real(self, system, text)
        with mock.patch.object(ag.Session, "_generate", spy):
            run = Run([build("f", "(* %d x)" % i, expect="2") for i in range(2, 7)], value="(:GOT 9)")
        labels = [(c["label"], c["deep"]) for c in run.calls]
        self.assertEqual(labels[:4], [("plan", False), ("repair", False), ("rewrite", False), ("rewrite", True)])
        self.assertIn((True, ag.THINK_TOKENS_REPAIR), seen)
        self.assertLess(ag.THINK_TOKENS_REPAIR, ag.THINK_TOKENS["low"])
        plain, deep = [p[1] for p, c in zip(run.prompts, run.calls) if c["label"] == "rewrite"][:2]
        self.assertNotIn("before answering", plain)                  # that wording drew empty replies
        self.assertIn("hand-trace the first failing test", deep)
        self.assertIn("from scratch", plain)

    def test_the_thinking_budget_reaches_the_api_call(self):
        saved = {k: getattr(ag._TEMP, k, None) for k in ("deep", "effort", "think_tokens")}
        try:
            ag._TEMP.deep, ag._TEMP.effort, ag._TEMP.think_tokens = True, "low", 5000
            with mock.patch.object(ag, "live_status", return_value={"available": True, "reason": ""}), \
                    mock.patch("cerebras_client.generate",
                               return_value={"text": "{}", "finish_reason": "stop"}) as gen:
                ag.live_generate("system", "user")
        finally:
            for k, v in saved.items():
                setattr(ag._TEMP, k, v)
        self.assertEqual(gen.call_args.kwargs["max_tokens"], 5000)


class EmptyReplyTests(unittest.TestCase):
    def test_an_empty_reply_is_asked_again_with_the_same_question(self):
        replies = iter([{"text": "", "finish_reason": "stop", "input_tokens": 9, "output_tokens": 1,
                         "cost_usd": 0.0, "latency_ms": 250.0},
                        ag._fake({"action": "stop"})])
        prompts = []

        def gen(system, user):
            prompts.append(user)
            return next(replies)
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              log_path=Path(tmp) / "l.jsonl")
            sess._ask("THE QUESTION", "probe")
            self.assertEqual(prompts, ["THE QUESTION", "THE QUESTION"])       # no "cut off" complaint
            labels = [e["label"] for e in sess.events if e["kind"] == "model_call"]
            self.assertEqual(labels, ["probe", "probe (retry: empty reply)"])
            self.assertEqual([e["finish_reason"] for e in sess.events if e["kind"] == "model_reply"][0], "stop")

    def test_a_cut_off_reply_still_gets_the_explanation(self):
        replies = iter([dict(ag._fake({"action": "stop"}), text='{"action": "bui'),
                        ag._fake({"action": "stop"})])
        prompts = []

        def gen(system, user):
            prompts.append(user)
            return next(replies)
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              log_path=Path(tmp) / "l.jsonl")
            sess._ask("THE QUESTION", "probe")
            self.assertIn("YOUR PREVIOUS REPLY WAS CUT OFF", prompts[1])


class PageTestGuidanceTests(unittest.TestCase):
    def test_the_web_reminder_says_how_to_test_a_page(self):
        for phrase in ("expect a response plist holding only what matters",
                       "(:status 303 :headers ((\"Location\" \"/notes\")))",
                       "(:status 200 :body \"...Hello...\")", "Do not wrap the call in LET"):
            self.assertIn(phrase, ag.WEB_REMINDER)

    def test_the_recommended_expectations_really_pass_in_sbcl(self):
        # the shapes the reminder recommends, against what the kit actually returns
        import webkit
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            webkit.seed(reg)
            code = ("%s\n%s\n(list (gg-check (redirect-to \"/login\") '(:status 303 :headers "
                    "((\"Location\" \"/login\")))) (gg-check (html-page 200 \"<h1>Hi Widget!</h1>\") "
                    "'(:status 200 :body \"...Widget...\")) (gg-check (html-page 200 \"<h1>Hi</h1>\") "
                    "'(:status 200 :body \"...Widget...\")))" % (ag.GG_CHECK, reg.prelude()))
            env = ag._worker_fn(code)
        self.assertTrue(env.get("ok"), env.get("error"))
        got = " ".join(env["return_value"].upper().split())
        self.assertTrue(got.startswith("(T T (:GOT"), got)


class ChangedToolTests(unittest.TestCase):
    """Session 3a0278e204 ("refine the html markup to be quite fancy"): 7 steps planned,
    5 answered "use the saved tool", reported as a success with nothing refined."""

    PLAN = {"action": "plan", "steps": [
        {"name": "page-a", "spec": "(page-a x) -> fancy page a"},
        {"name": "page-b", "spec": "(page-b x) -> fancy page b"}]}

    def _run(self, replies):
        prompts, it = [], iter(replies)

        def gen(system, user):
            prompts.append(user)
            return ag._fake(next(it))
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            reg.add({"name": "page-a", "description": "plain page a",
                     "definition": "(defun page-a (x) x)"})
            sess = ag.Session("refine the html markup to be quite fancy", gen, registry=reg,
                              worker_fn=worker(), log_path=Path(tmp) / "l.jsonl")
            sess.run()
            return sess, prompts, {t["name"]: t["definition"] for t in reg.load()}

    def test_a_step_named_after_a_saved_tool_is_told_it_is_a_change(self):
        sess, prompts, tools = self._run([
            self.PLAN, build("page-a", "(list :fancy x)"), build("page-b", "(list :b x)"),
            {"action": "use", "call": "(page-b 1)"}])
        step_a = next(p for p in prompts if "GOAL: (page-a x)" in p)
        step_b = next(p for p in prompts if "GOAL: (page-b x)" in p)
        self.assertIn("A tool named page-a is ALREADY SAVED", step_a)
        self.assertIn("refine the html markup to be quite fancy", step_a)
        self.assertIn("action use is not an answer here", step_a)
        self.assertNotIn("ALREADY SAVED", step_b)                    # page-b is new
        self.assertIn(":fancy", tools["page-a"])                     # and it really was replaced
        self.assertEqual(next(e for e in sess.events if e["kind"] == "summary")["kept"], [])

    def test_a_change_the_model_declines_is_reported_not_hidden(self):
        sess, prompts, tools = self._run([
            self.PLAN, {"action": "use", "call": "(page-a 1)", "why": "it already matches"},
            build("page-b", "(list :b x)"), {"action": "use", "call": "(page-b 1)"}])
        kept = next(e for e in sess.events if e["kind"] == "step_kept")
        self.assertEqual((kept["name"], kept["why"]), ("page-a", "it already matches"))
        self.assertEqual(next(e for e in sess.events if e["kind"] == "summary")["kept"], ["page-a"])
        self.assertEqual(tools["page-a"], "(defun page-a (x) x)")

    def test_the_contract_and_the_reminder_agree_on_how_to_test_a_page(self):
        self.assertNotIn("(let ((r (page-fn", ag.WEB_APP_CONTRACT)
        self.assertIn("(page-fn request state) => (:status 200 :body \"...Hello...\")", ag.WEB_APP_CONTRACT)


if __name__ == "__main__":
    unittest.main()
