"""From the second live run with lanes, thinking and the screenshot check (sessions
e0ccb72fb9, 5b4a7501ac, ecbb133aa4, 07b4ec06de): a hung call was waited on for 60 s
twice, nine page replies were cut off at 2200 tokens, pages showed stray "n"
characters from ``\\n`` in Lisp strings, a failed round of screenshot fixes turned a
working build into a failed one, and there was no way to stop a build."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

import agent_session as ag  # noqa: E402
import modelgate  # noqa: E402
import oracle as orc  # noqa: E402
import visualcheck  # noqa: E402

BS = chr(92)


class StringEscapeTests(unittest.TestCase):
    def test_backslash_n_in_a_definition_becomes_the_line_break_that_was_meant(self):
        defn = ('(defun page (x) (format nil "<tr>' + BS + 'n<td>~a</td>' + BS + 'n</tr>" x))')
        plan = ag.normalize_plan({"action": "build", "name": "page", "description": "d",
                                  "definition": defn, "call": "(page 1)",
                                  "tests": [{"call": "(page 1)", "expect": "T"}]})
        self.assertIn("newline-escape-in-string", plan["auto_fixes"])
        self.assertNotIn(BS + "n", plan["definition"])
        self.assertIn("<tr>\n<td>~a</td>\n</tr>", plan["definition"])

    def test_other_escapes_and_code_outside_strings_are_left_alone(self):
        text = '(list #' + BS + 'n "a' + BS + '"b" "C:' + BS + BS + 'new" "tab' + BS + 'there")'
        self.assertEqual(ag.newline_escapes(text),
                         '(list #' + BS + 'n "a' + BS + '"b" "C:' + BS + BS + 'new" "tab here")')

    def test_the_fixed_page_really_has_no_stray_n_in_sbcl(self):
        defn = ag.newline_escapes('(defun page () "<p>a</p>' + BS + 'n<p>b</p>")')
        env = ag._worker_fn(defn + "\n(list (count #" + BS + "n (page)) (count #" + BS + "Newline (page)))")
        self.assertTrue(env.get("ok"), env.get("error"))
        self.assertEqual(env["return_value"].strip(), "(0 1)")

    def test_the_prompts_say_it_and_say_how_to_include_a_stylesheet(self):
        self.assertIn("never write " + BS + "n in a string", ag.WEB_REMINDER)
        self.assertIn("is inserted as it is, never wrapped again", ag.WEB_APP_CONTRACT)
        key, _ = orc.lisp_hints('malformed property list: "<!DOCTYPE html>"')[0]
        self.assertEqual(key, "plist-vs-string")


class CssCommaTests(unittest.TestCase):
    """Session 07b4ec06de left the app with NO styling: the stylesheet function was written as
    "a{..}," "b{..}," and a browser drops every rule that follows a comma."""

    LIVE = ('(defun css-style () (concatenate (quote string) "*{margin:0;padding:0;box-sizing:border-box;}," '
            '"body{font-family:Inter,sans-serif;color:#fff;}," ".nav-links a:hover{text-shadow:0 0 10px rgba(1,2,3,0.8);}," '
            '".btn{background:linear-gradient(135deg,#667eea,#764ba2);}"))')

    def test_commas_between_css_rules_are_dropped_and_commas_inside_rules_stay(self):
        import lispstyle
        fixed = lispstyle.fix_css_commas(self.LIVE)
        self.assertNotIn('},"', fixed)
        for kept in ("font-family:Inter,sans-serif", "rgba(1,2,3,0.8)", "linear-gradient(135deg,#667eea,#764ba2)",
                     "*{margin:0;padding:0;box-sizing:border-box;}"):
            self.assertIn(kept, fixed)
        self.assertEqual(lispstyle.fix_css_commas(".a,.b{x:1}, .c{y:2}".join('""')), '".a,.b{x:1} .c{y:2}"')
        self.assertEqual(lispstyle.fix_css_commas('"a:hover{x:1},li:not(.b){y:2} , p{z:3}"'),
                         '"a:hover{x:1}li:not(.b){y:2} p{z:3}"')

    def test_text_that_is_not_css_is_left_alone(self):
        import lispstyle
        for keep in ('"var x = [{a:1},{b:2}];"', '"a{color:red} b{color:blue}"', '"hello, world"', '"},"',
                     '"@media (max-width:6px){.a{x:1}}"', '(list 1 2)', '"{foo: {a:1}, bar: 2}"',
                     '"cfg = {a:1}, other"', '"f({a:1}, b)"', '"x{a:1}, 5"'):
            self.assertEqual(lispstyle.fix_css_commas(keep), keep, keep)

    def test_a_long_stylesheet_is_handled_at_once(self):
        # The first version of this repair froze the dashboard on a live build: its
        # pattern backtracked without end on one long string of rules.
        import lispstyle
        rules = "".join(".rule-%d:hover > a, .other-%d{margin:0;padding:%dpx;color:#fff;transition:all 0.3s;}" % (n, n, n)
                        for n in range(4000))
        for text in ('"%s"' % rules, '"%s,"' % rules, '"%s"' % rules.replace("}", "},"),
                     '"%s"' % ("a{b:c}" * 20000 + "{"), '"%s"' % ("}," * 50000), '"%s"' % ("x{y:z}, " * 20000)):
            started = time.perf_counter()
            lispstyle.fix_css_commas("(defun css () %s)" % text)
            self.assertLess(time.perf_counter() - started, 2.0, text[:40])
        fixed = lispstyle.fix_css_commas('"%s"' % rules.replace("}", "},"))
        self.assertEqual(fixed, '"%s"' % rules)

    def test_a_build_is_repaired_before_it_is_judged(self):
        plan = ag.normalize_plan({"action": "build", "name": "css-style", "description": "d",
                                  "definition": self.LIVE, "call": "(css-style)",
                                  "tests": [{"call": "(css-style)", "expect": "T"}]})
        self.assertIn("css-rule-commas", plan["auto_fixes"])
        self.assertNotIn('},"', plan["definition"])


class Busy(Exception):
    def __init__(self, text, status=None):
        super().__init__(text)
        self.status_code = status
        self.response = mock.Mock(headers={})


class TimeoutTests(unittest.TestCase):
    def setUp(self):
        self.waits = []
        old = ag.GATE
        ag.GATE = modelgate.ModelGate(start=4)
        self.addCleanup(setattr, ag, "GATE", old)
        patcher = mock.patch.object(ag.time, "sleep", self.waits.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fn(self, errors):
        todo = list(errors)

        def fn():
            if todo:
                raise todo.pop(0)
            return {"text": "ok"}
        return fn

    def test_a_timeout_is_asked_again_at_once_and_a_rate_limit_still_waits(self):
        ag._with_retry(self._fn([Busy("Request timed out.")]))
        self.assertEqual(self.waits, [0.5])
        del self.waits[:]
        ag._with_retry(self._fn([Busy("Error code: 429", 429)]))
        self.assertEqual(self.waits, [ag._RETRY_WAITS[0]])

    def test_one_try_means_one_try(self):
        with self.assertRaises(Busy):
            ag._with_retry(self._fn([Busy("Request timed out.")]), tries=1)
        self.assertEqual(self.waits, [])

    def _live(self, replies, **temp):
        saved = {k: getattr(ag._TEMP, k, None) for k in ("deep", "effort", "think_tokens")}
        try:
            for k, v in temp.items():
                setattr(ag._TEMP, k, v)
            with mock.patch.object(ag, "live_status", return_value={"available": True, "reason": ""}), \
                    mock.patch("cerebras_client.generate", side_effect=replies) as gen:
                return ag.live_generate("system", "user"), gen
        finally:
            for k, v in saved.items():
                setattr(ag._TEMP, k, v)

    def test_a_plain_call_waits_30_seconds_and_may_answer_at_length(self):
        _, gen = self._live([{"text": "{}"}])
        kw = gen.call_args.kwargs
        self.assertEqual((kw["timeout"], kw["max_tokens"]), (ag.CALL_TIMEOUT_S, ag.REPLY_TOKENS))
        self.assertEqual((ag.CALL_TIMEOUT_S, ag.REPLY_TOKENS), (30.0, 4500))

    def test_a_thinking_rewrite_that_runs_away_is_dropped_for_the_plain_answer(self):
        res, gen = self._live([Busy("Request timed out."),
                               {"text": "{}", "finish_reason": "stop", "output_tokens": 300,
                                "input_tokens": 900, "cost_usd": 0.001, "latency_ms": 700.0}],
                              deep=True, effort="low", think_tokens=5000)
        first, second = gen.call_args_list
        self.assertEqual((first.kwargs["timeout"], first.kwargs["max_tokens"],
                          first.kwargs["reasoning_effort"]), (ag.THINK_REPAIR_TIMEOUT_S, 5000, "low"))
        self.assertEqual(second.kwargs["reasoning_effort"], "none")
        self.assertEqual((res["text"], res["thinking_fallback"]), ("{}", True))
        self.assertEqual(res["latency_ms"], 8700.0)                  # the wait is counted honestly
        self.assertEqual(self.waits, [])                              # no retry of the thinking call

    def test_another_error_in_a_thinking_rewrite_is_not_swallowed(self):
        with self.assertRaises(ValueError):
            self._live([ValueError("bad request")], deep=True, effort="low", think_tokens=5000)

    def test_a_thought_out_plan_keeps_its_longer_wait(self):
        _, gen = self._live([{"text": "{}", "finish_reason": "stop"}], deep=True, effort="low")
        self.assertEqual(gen.call_args.kwargs["timeout"], ag.THINK_TIMEOUT_S)


def build(name, body):
    call = "(%s 1)" % name
    return {"action": "build", "name": name, "description": "d",
            "definition": "(defun %s (x) %s)" % (name, body),
            "tests": [{"call": call, "expect": "T"}], "call": call}


def worker(fail=()):
    def run(code):
        if "prin1-to-string (getf resp :state)" in code:
            return {"ok": True, "elapsed_ms": 1.0, "return_value": '(200 NIL "<h1>ok</h1>" 0 "NIL")'}
        bad = any(("(gg-check (%s " % n) in code for n in fail)
        return {"ok": True, "stdout": "", "error": "", "timed_out": False, "elapsed_ms": 1.0,
                "return_value": "(:GOT 9)" if bad else "T"}
    return run


PLAN = {"action": "plan", "steps": [{"name": n, "spec": "(%s x) -> part %s" % (n, n)}
                                    for n in ("one", "two", "three", "four")]}


class CancelTests(unittest.TestCase):
    def _session(self, parallel, delay=0.25):
        calls = []
        lock = threading.Lock()

        def gen(system, user):
            with lock:
                calls.append(time.perf_counter())
            name = next((n for n in ("one", "two", "three", "four") if "GOAL: (%s " % n in user), None)
            time.sleep(delay)
            if "All planned helper tools" in user:
                return ag._fake({"action": "use", "call": "(four 1)"})
            return ag._fake(build(name, "x") if name else PLAN)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sess = ag.Session("make four parts", gen, registry=ag.ToolRegistry(Path(self.tmp.name) / "t.json"),
                          worker_fn=worker(), log_path=Path(self.tmp.name) / "l.jsonl")
        sess.parallel = parallel
        return sess, calls

    def _run_and_cancel(self, sess, after):
        thread = threading.Thread(target=sess.run, daemon=True)
        thread.start()
        time.sleep(after)
        asked = sess.cancel()
        thread.join(10)
        self.assertFalse(thread.is_alive(), "the session did not stop")
        return asked

    def test_cancelling_a_build_in_lanes_stops_it_and_keeps_what_was_saved(self):
        sess, calls = self._session(parallel=2)
        self.assertTrue(self._run_and_cancel(sess, after=0.65))       # plan done, lanes under way
        self.assertEqual(sess.state, "cancelled")
        kinds = [e["kind"] for e in sess.events]
        self.assertIn("cancel_requested", kinds)
        self.assertLess(kinds.index("cancel_requested"), kinds.index("cancelled"))
        saved = next(e for e in sess.events if e["kind"] == "cancelled")["saved"]
        self.assertEqual(sorted(saved), sorted(t["name"] for t in sess.registry.load()))
        self.assertLess(len(saved), 4)                                # it did not run to the end
        summary = next(e for e in sess.events if e["kind"] == "summary")
        self.assertEqual(summary["outcome"], "cancelled")
        self.assertEqual(next(e for e in sess.events if e["kind"] == "done")["state"], "cancelled")
        self.assertFalse(any(e["kind"] in ("error", "gave_up") for e in sess.events))
        asked_after = len(calls)
        time.sleep(0.4)
        self.assertEqual(len(calls), asked_after)                     # no call was started afterwards

    def test_cancelling_a_build_in_order_stops_it_too(self):
        sess, _ = self._session(parallel=1, delay=0.2)
        self.assertTrue(self._run_and_cancel(sess, after=0.5))
        self.assertEqual(sess.state, "cancelled")
        self.assertLess(len(sess.registry.load()), 4)

    def test_a_reply_that_arrives_after_the_cancel_is_not_built(self):
        sess, _ = self._session(parallel=1, delay=0.3)
        self._run_and_cancel(sess, after=0.4)                          # the first step's reply is in flight
        self.assertEqual(sess.registry.load(), [])
        self.assertFalse(any(e["kind"] == "verdict" for e in sess.events))

    def test_cancel_is_refused_when_nothing_is_running(self):
        sess, _ = self._session(parallel=1, delay=0.0)
        sess.run()
        self.assertFalse(sess.cancel())
        self.assertEqual(sess.state, "done")

    def test_the_manager_cancels_the_running_session_and_skips_its_twin(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ag, "AGENT_DIR", Path(tmp)):
            mgr = ag.SessionManager(ag.ToolRegistry(Path(tmp) / "tools.json"))
            self.assertEqual(mgr.cancel(), (False, None))
            prompts = []

            def gen(system, user):
                prompts.append(user)
                time.sleep(0.3)
                return ag._fake(build("one", "x"))
            mgr.generators["demo"] = gen
            sid, err = mgr.start("make one", mode="demo", compare=True)
            self.assertIsNone(err)
            time.sleep(0.1)
            self.assertEqual(mgr.cancel(), (True, sid))
            for _ in range(60):
                if not mgr.busy():
                    break
                time.sleep(0.05)
            self.assertFalse(mgr.busy())
            self.assertEqual(mgr.get(sid)["state"], "cancelled")
            self.assertEqual(len(prompts), 1)                         # the no-memory twin never ran
            self.assertIsNone(mgr.get(sid)["compare"])                # and the page is not left waiting for it

    def test_the_route_reports_whether_anything_was_cancelled(self):
        import server as dash
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ag, "AGENT_DIR", Path(tmp)):
            agent = ag.SessionManager(ag.ToolRegistry(Path(tmp) / "tools.json"))
            ctx = SimpleNamespace(root=ROOT, manager=None, agent=agent)
            self.assertEqual(dash.dispatch("POST", "/api/agent/cancel", b"", ctx),
                             (200, {"cancelled": False, "session_id": None}))
            with mock.patch.object(agent, "cancel", return_value=(True, "abc123")):
                self.assertEqual(dash.dispatch("POST", "/api/agent/cancel", b"", ctx),
                                 (200, {"cancelled": True, "session_id": "abc123"}))


class FixRoundTests(unittest.TestCase):
    PAGES = [{"label": "GET /", "path": "/", "status": 200, "html": "<h1>x</h1>"}]
    PNG = bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"rest"

    def _session(self, replies, fail=()):
        self.prompts = []
        it = iter(replies)

        def gen(system, user):
            self.prompts.append(user)
            return ag._fake(next(it))

        def capture(html, out):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(self.PNG)
            return {"ok": True, "path": str(out), "error": "", "ms": 1.0}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(visualcheck, "collect_pages", lambda app, limit=3: list(self.PAGES))
        patcher.start()
        self.addCleanup(patcher.stop)
        reg = ag.ToolRegistry(Path(self.tmp.name) / "t.json")
        reg.add({"name": "handle-request", "description": "router",
                 "definition": "(defun handle-request (request state) (html-page 200 \"x\"))"})
        sess = ag.Session("make it look professional", gen, registry=reg, worker_fn=worker(fail),
                          log_path=Path(self.tmp.name) / "l.jsonl")
        sess.visual, sess.capture_fn = True, capture
        sess.shots_dir = Path(self.tmp.name) / "shots"
        sess.max_calls, sess._app = 30, True
        return sess

    @staticmethod
    def look(*problems):
        return {"done": not problems, "problems": list(problems), "fix": ""}

    @staticmethod
    def fix(*names):
        return {"action": "plan", "steps": [{"name": n, "spec": "(%s x) -> page" % n} for n in names]}

    def test_a_failed_round_of_fixes_leaves_the_build_as_it_was(self):
        bad = [build("page", "(list %d x)" % i) for i in range(4)]
        sess = self._session([self.look("the page is plain"), self.fix("page")] + bad +
                             [{"action": "stop"}, {"action": "stop"}, self.look("the page is plain")],
                             fail=["page"])
        sess._visual_review()
        self.assertEqual(sess.state, "running")                       # not turned into a failure
        self.assertEqual(sess._failed_steps, [])
        self.assertEqual(sess._summary()["visual"]["unfixed"], ["page"])
        self.assertFalse(any(e["kind"] == "gave_up" for e in sess.events))
        self.assertEqual([e["name"] for e in sess.events if e["kind"] == "step_failed"], ["page"])

    def test_the_fix_steps_are_told_what_the_screenshots_showed(self):
        sess = self._session([self.look("stray n characters above the table", "raw CSS shown as text"),
                              self.fix("page"), build("page", "x"), self.look()])
        sess._visual_review()
        step = next(p for p in self.prompts if "GOAL: (page x)" in p)
        self.assertIn("SCREENSHOTS OF THE APP AS IT IS NOW SHOW THESE PROBLEMS: stray n characters "
                      "above the table; raw CSS shown as text.", step)

    def test_fixing_goes_on_while_each_look_finds_fewer_problems(self):
        sess = self._session([self.look("a", "b", "c"), self.fix("page"), build("page", "1"),
                              self.look("a"), self.fix("page"), build("page", "2"),
                              self.look("a")])
        sess._visual_review()
        labels = [e["label"] for e in sess.events if e["kind"] == "model_call"]
        self.assertEqual(labels.count("visual-review"), 3)            # look, fix, look, fix, last look
        self.assertEqual(labels.count("visual-fix"), ag.MAX_VISUAL_FIXES)
        self.assertEqual(sess._summary()["visual"]["rounds"], 3)

    def test_fixing_stops_when_a_round_did_not_help(self):
        sess = self._session([self.look("a", "b"), self.fix("page"), build("page", "1"),
                              self.look("a", "c", "d")])
        sess._visual_review()
        labels = [e["label"] for e in sess.events if e["kind"] == "model_call"]
        self.assertEqual((labels.count("visual-review"), labels.count("visual-fix")), (2, 1))

    def test_a_function_built_in_a_later_round_is_no_longer_reported_missing(self):
        sess = self._session([build("page", "x")])
        sess._failed_steps, sess.state, sess._incomplete = ["page"], "failed", True
        sess._in_step = True
        self.assertTrue(sess._build_loop(build("page", "x"), "", quiet=True))
        self.assertEqual(sess._failed_steps, [])
        sess._smoke()
        self.assertEqual(sess.state, "running")
        self.assertEqual([e for e in sess.events if e["kind"] == "gave_up"], [])


if __name__ == "__main__":
    unittest.main()
