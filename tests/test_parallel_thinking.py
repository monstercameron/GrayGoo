"""Thinking where it pays, and plan steps built at the same time.

Independent steps of a plan are built concurrently, each in its own lane; a
step waits only for the planned functions it calls. A 429 from the API lowers
how many model calls run at once. A thinking call whose reasoning used the
whole token budget is answered again without thinking instead of being lost.
"""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import modelgate  # noqa: E402


def build(name, body, params="(x)"):
    call = "(%s 1)" % name
    return {"action": "build", "name": name, "description": "d",
            "definition": "(defun %s %s %s)" % (name, params, body),
            "tests": [{"call": call, "expect": "T"}], "call": call}


def worker(fail_names=()):
    def run(code):
        bad = any(("(gg-check (%s " % n) in code for n in fail_names)
        return {"ok": True, "stdout": "", "error": "", "timed_out": False, "elapsed_ms": 1.0,
                "return_value": "(:GOT 9)" if bad else "T"}
    return run


class Model:
    """A thread-safe scripted model that answers by what the prompt asks for."""

    def __init__(self, plan, bodies, delay=0.08, slow=()):
        self.plan, self.bodies, self.delay, self.slow = plan, bodies, delay, dict(slow)
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.prompts = []

    def __call__(self, system, user):
        with self.lock:
            self.prompts.append(user)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            name = next((n for n in self.bodies if "GOAL: (%s " % n in user), None)
            time.sleep(self.slow.get(name, self.delay))
            if name and ("BUILD exactly this one" in user or "PREVIOUS ATTEMPT FAILED" in user):
                return ag._fake(build(name, self.bodies[name]))
            if "All planned helper tools" in user:
                return ag._fake({"action": "use", "call": "(%s 1)" % list(self.bodies)[-1]})
            if "Split it into" in user:
                return ag._fake({"action": "stop"})
            return ag._fake(self.plan)
        finally:
            with self.lock:
                self.active -= 1


PLAN = {"action": "plan", "steps": [
    {"name": "double", "spec": "(double x) -> twice x"},
    {"name": "triple", "spec": "(triple x) -> three times x"},
    {"name": "square", "spec": "(square x) -> x times x"},
    {"name": "all-three", "spec": "(all-three x) -> (list (double x) (triple x) (square x))"}]}
BODIES = {"double": "(* 2 x)", "triple": "(* 3 x)", "square": "(* x x)",
          "all-three": "(list (double x) (triple x) (square x))"}


def run(model, prompt="make all-three", parallel=3, run_worker=None, app=False):
    tmp = tempfile.mkdtemp()
    sess = ag.Session(prompt, model, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                      worker_fn=run_worker or worker(), log_path=Path(tmp) / "l.jsonl")
    sess.parallel = parallel
    if app:
        real = sess._run_steps

        def as_app(plan):
            sess._app = True
            return real(plan)
        sess._run_steps = as_app
    done = threading.Thread(target=sess.run, daemon=True)
    done.start()
    done.join(30)
    assert not done.is_alive(), "the session did not finish (lanes are stuck)"
    return sess


def at(sess, kind, **match):
    """Position of the first matching event (a step event's own "i" is its step number)."""
    return next(n for n, e in enumerate(sess.events) if e["kind"] == kind and
                all(e.get(k) == v for k, v in match.items()))


class LaneTests(unittest.TestCase):
    def test_step_dependencies_are_the_earlier_steps_a_spec_names(self):
        steps = [{"name": "row", "spec": "(row r) -> html, used by page"},
                 {"name": "page", "spec": "(page rows) -> calls (row r) for each"},
                 {"name": "route", "spec": "(route req) -> (page ...)"},
                 {"name": "rows", "spec": "(rows state) -> list"}]
        self.assertEqual(ag.step_deps(steps), [[], [0], [1], []])      # 'rows' is not 'row'

    def test_independent_steps_are_built_at_the_same_time_and_a_caller_waits(self):
        model = Model(PLAN, BODIES)
        sess = run(model)
        self.assertEqual(sess.state, "done", [e for e in sess.events if e["kind"] in ("gave_up", "error")])
        self.assertEqual(sorted(t["name"] for t in sess.registry.load()), sorted(BODIES))
        self.assertGreaterEqual(model.peak, 2)                          # calls really overlapped
        self.assertLessEqual(model.peak, 3)                             # ...within the session's cap
        # the caller started only after the three functions it names were saved
        for leaf in ("double", "triple", "square"):
            self.assertLess(at(sess, "step_done", name=leaf), at(sess, "step", name="all-three"))
        self.assertEqual(next(e for e in sess.events if e["kind"] == "step_wait")["name"], "all-three")
        par = next(e for e in sess.events if e["kind"] == "parallel")
        self.assertEqual((par["names"], par["limit"]), (list(BODIES), 3))
        summary = next(e for e in sess.events if e["kind"] == "summary")
        self.assertEqual(summary["concurrency"]["parallel"], True)
        self.assertEqual(summary["concurrency"]["lanes"], 4)
        self.assertGreaterEqual(summary["concurrency"]["peak"], 2)
        self.assertEqual(sess.model_calls, 6)                           # plan + 4 steps + final

    def test_every_event_of_a_lane_names_its_function(self):
        sess = run(Model(PLAN, BODIES))
        first = at(sess, "parallel")
        last = max(n for n, e in enumerate(sess.events) if e["kind"] == "step_done")
        inside = sess.events[first + 1:last + 1]
        self.assertTrue(all(e.get("lane") in BODIES for e in inside),
                        [e["kind"] for e in inside if e.get("lane") not in BODIES])
        for e in inside:
            if e["kind"] in ("promoted", "step", "step_done"):
                self.assertEqual(e["name"], e["lane"])
            if e["kind"] == "decision" and e["action"] == "build":
                self.assertEqual(e["plan"]["name"], e["lane"])
        self.assertFalse(any("lane" in e for e in sess.events[:first + 1] + sess.events[last + 1:]))

    def test_the_log_file_has_one_whole_event_per_line(self):
        import json
        sess = run(Model(PLAN, BODIES))
        lines = sess._log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(x) for x in lines], json.loads(json.dumps(sess.events)))

    def test_a_draft_that_calls_an_unnamed_planned_function_waits_for_it(self):
        plan = {"action": "plan", "steps": [
            {"name": "double", "spec": "(double x) -> twice x"},
            {"name": "quad", "spec": "(quad x) -> four times x"}]}     # does not name double
        model = Model(plan, {"double": "(* 2 x)", "quad": "(double (double x))"},
                      delay=0.02, slow={"double": 0.3})
        sess = run(model, prompt="make quad")
        self.assertEqual(sess.state, "done", [e for e in sess.events if e["kind"] in ("gave_up", "error")])
        wait = next(e for e in sess.events if e["kind"] == "step_wait")
        self.assertEqual((wait["name"], wait["on"]), ("quad", ["double"]))
        self.assertLess(at(sess, "promoted", name="double"), at(sess, "verdict", lane="quad"))

    def test_a_failed_lane_does_not_stop_the_other_lanes_of_an_app(self):
        model = Model(PLAN, BODIES, delay=0.01)
        sess = run(model, run_worker=worker(["triple"]), app=True)
        names = [t["name"] for t in sess.registry.load()]
        self.assertEqual(sorted(names), ["all-three", "double", "square"])
        self.assertEqual([e["name"] for e in sess.events if e["kind"] == "step_failed"], ["triple"])
        self.assertEqual(next(e for e in sess.events if e["kind"] == "step_done"
                              and e["name"] == "triple")["ok"], False)
        self.assertIn("triple", sess._failed_steps)

    def test_a_plan_that_is_not_an_app_stops_when_a_step_fails(self):
        model = Model(PLAN, BODIES, delay=0.01)
        sess = run(model, run_worker=worker(["double"]))
        self.assertEqual(sess.state, "failed")
        self.assertNotIn("all-three", [t["name"] for t in sess.registry.load()])
        gave = [e for e in sess.events if e["kind"] == "gave_up"][-1]
        self.assertNotIn("not built", gave["detail"])                   # the real failure, not a skip
        self.assertGreater(gave["attempts"], 0)
        self.assertFalse(any(e["kind"] == "step" and e["name"] == "all-three" for e in sess.events))

    def test_the_call_budget_still_stops_a_concurrent_run(self):
        sess = run(Model(PLAN, BODIES, delay=0.01), run_worker=worker(["double", "triple", "square"]))
        self.assertLessEqual(sess.model_calls, ag.MAX_MODEL_CALLS_PLAN)
        self.assertEqual(sess.state, "failed")

    def test_scripted_models_still_build_strictly_in_order(self):
        replies = iter([PLAN] + [build(n, BODIES[n]) for n in BODIES] +
                       [{"action": "use", "call": "(all-three 1)"}])
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("make all-three", lambda s, u: ag._fake(next(replies)),
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              worker_fn=worker(), log_path=Path(tmp) / "l.jsonl")
            self.assertEqual(sess.parallel, 1)
            sess.run()
            self.assertEqual(sess.state, "done")
            self.assertFalse(any(e["kind"] == "parallel" or "lane" in e for e in sess.events))
            self.assertEqual(next(e for e in sess.events if e["kind"] == "summary")
                             ["concurrency"]["parallel"], False)

    def test_the_live_model_gets_lanes_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", ag.live_generate, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              log_path=Path(tmp) / "l.jsonl")
            self.assertEqual(sess.parallel, ag.PARALLEL_STEPS)
            self.assertGreater(ag.PARALLEL_STEPS, 1)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Busy(Exception):
    def __init__(self, text, status=None, retry=None):
        super().__init__(text)
        self.status_code = status
        self.response = mock.Mock(headers={"retry-after": retry} if retry else {})


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        gate = modelgate.ModelGate(start=4, clock=self.clock)
        self.notes = []
        for name, value in (("GATE", gate),):
            old = getattr(ag, name)
            setattr(ag, name, value)
            self.addCleanup(setattr, ag, name, old)

        def sleep(seconds):
            self.clock.now += seconds                 # the wait passes at once
        patcher = mock.patch.object(ag.time, "sleep", sleep)
        patcher.start()
        self.addCleanup(patcher.stop)
        ag._TEMP.notify = lambda msg, **kw: self.notes.append(dict(kw, message=msg))
        self.addCleanup(setattr, ag._TEMP, "notify", None)

    def _flaky(self, errors):
        todo = list(errors)

        def fn():
            if todo:
                raise todo.pop(0)
            return {"text": "ok"}
        return fn

    def test_a_429_lowers_the_number_of_simultaneous_calls_and_waits_as_told(self):
        res = ag._with_retry(self._flaky([
            Busy("Error code: 429 - too_many_requests_error", 429, "5"),
            Busy("Error code: 429 - too_many_requests_error", 429, "2")]))
        self.assertEqual(res, {"text": "ok"})
        self.assertEqual([(n["rate_limited"], n["limit"], n["wait_s"]) for n in self.notes],
                         [(True, 2, 5.0), (True, 1, 2.0)])
        self.assertEqual(self.clock.now, 107.0)                         # waited 5 s, then 2 s
        self.assertEqual((ag.GATE.limit, ag.GATE.throttles), (1, 2))

    def test_a_timeout_is_retried_without_slowing_anything_down(self):
        res = ag._with_retry(self._flaky([Busy("Request timed out.")]))
        self.assertEqual(res, {"text": "ok"})
        self.assertEqual(ag.GATE.limit, 4)
        self.assertNotIn("rate_limited", self.notes[0])

    def test_an_ordinary_error_is_not_retried(self):
        with self.assertRaises(ValueError):
            ag._with_retry(self._flaky([ValueError("failed to generate a reply")]))
        self.assertEqual(self.notes, [])

    def test_a_rate_limit_that_does_not_clear_is_raised_after_the_retries(self):
        errors = [Busy("429 quota", 429, "1") for _ in range(len(ag._RETRY_WAITS) + 1)]
        with self.assertRaises(Busy):
            ag._with_retry(self._flaky(errors))
        self.assertEqual(len(self.notes), len(ag._RETRY_WAITS))
        self.assertEqual(ag.GATE.limit, 1)                              # never below one call

    def test_steady_success_raises_the_limit_again(self):
        ag.GATE.throttled(1)
        self.assertEqual(ag.GATE.limit, 2)
        self.clock.now += 60
        for _ in range(8):
            ag._with_retry(lambda: {"text": "ok"})
        self.assertEqual(ag.GATE.limit, 3)

    def test_a_session_logs_the_slowdown(self):
        errors = [Busy("Error code: 429", 429, "3")]

        def gen(system, user):
            return ag._with_retry(lambda: (_ for _ in ()).throw(errors.pop()) if errors
                                  else ag._fake({"action": "stop"}))
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              worker_fn=worker(), log_path=Path(tmp) / "l.jsonl")
            sess.run()
            wait = next(e for e in sess.events if e["kind"] == "model_wait")
            self.assertEqual((wait["rate_limited"], wait["limit"], wait["wait_s"]), (True, 2, 3.0))
            self.assertEqual(next(e for e in sess.events if e["kind"] == "summary")
                             ["concurrency"]["throttles"], 1)


class ThinkingTests(unittest.TestCase):
    def _live(self, replies, effort="low"):
        saved = (getattr(ag._TEMP, "deep", False), getattr(ag._TEMP, "effort", "low"))
        try:
            ag._TEMP.deep, ag._TEMP.effort = True, effort
            with mock.patch.object(ag, "live_status", return_value={"available": True, "reason": ""}), \
                    mock.patch("cerebras_client.generate", side_effect=replies) as gen:
                return ag.live_generate("system", "user"), gen
        finally:
            ag._TEMP.deep, ag._TEMP.effort = saved

    def test_a_thinking_call_has_room_to_think_and_says_that_it_did(self):
        res, gen = self._live([{"text": "{}", "finish_reason": "stop", "output_tokens": 2400}])
        self.assertEqual(gen.call_count, 1)
        kw = gen.call_args.kwargs
        self.assertEqual((kw["reasoning_effort"], kw["max_tokens"], kw["max_retries"]), ("low", 8000, 0))
        self.assertEqual(res["thinking"], "low")
        self.assertNotIn("thinking_fallback", res)

    def test_a_reply_lost_to_reasoning_is_answered_again_without_thinking(self):
        for lost in ({"text": "", "finish_reason": "length"},
                     {"text": "{\"action\": \"bui", "finish_reason": "length"},
                     {"text": None, "finish_reason": "stop"}):
            first = dict(lost, input_tokens=900, output_tokens=8000, cost_usd=0.0128, latency_ms=4000.0)
            second = {"text": "{}", "finish_reason": "stop", "input_tokens": 900,
                      "output_tokens": 300, "cost_usd": 0.0013, "latency_ms": 500.0}
            res, gen = self._live([first, second])
            self.assertEqual(gen.call_count, 2)
            self.assertEqual(gen.call_args.kwargs["reasoning_effort"], "none")
            self.assertEqual(res["text"], "{}")
            self.assertEqual((res["thinking"], res["thinking_fallback"]), (None, True))
            self.assertEqual((res["output_tokens"], res["input_tokens"]), (8300, 1800))   # both are paid for
            self.assertAlmostEqual(res["cost_usd"], 0.0141)
            self.assertEqual(res["latency_ms"], 4500.0)

    def test_a_plain_call_never_thinks(self):
        with mock.patch.object(ag, "live_status", return_value={"available": True, "reason": ""}), \
                mock.patch("cerebras_client.generate", return_value={"text": "{}"}) as gen:
            ag.live_generate("system", "user")
        self.assertEqual(gen.call_args.kwargs["reasoning_effort"], "none")
        self.assertEqual(gen.call_args.kwargs["max_tokens"], ag.REPLY_TOKENS)

    def _calls(self, prompt, replies, run_worker=None, lessons=None):
        it = iter(replies)
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session(prompt, lambda s, u: ag._fake(next(it)),
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              worker_fn=run_worker or worker(), log_path=Path(tmp) / "l.jsonl")
            sess.run()
            return sess, {e["label"]: e["deep"] for e in sess.events if e["kind"] == "model_call"}

    def test_a_small_goal_is_planned_without_thinking(self):
        _, calls = self._calls("double a number", [build("double", "(* 2 x)")])
        self.assertEqual(calls, {"plan": False})

    def test_a_long_goal_gets_a_thought_out_plan(self):
        goal = "write a function that takes a list of numbers and returns the sum of the squares of the odd ones"
        _, calls = self._calls(goal, [build("odd-squares", "x")])
        self.assertEqual(calls["plan"], True)

    def test_an_app_gets_a_thought_out_plan(self):
        sess, calls = self._calls("simple todo website", [{"action": "stop"}])
        self.assertEqual(calls["plan"], True)

    def test_splitting_a_failed_step_and_its_last_rewrite_think(self):
        plan = {"action": "plan", "steps": [{"name": "double", "spec": "(double x) -> twice x"},
                                            {"name": "quad", "spec": "(quad x) -> (double (double x))"}]}
        bad = [build("double", "(* %d x)" % i) for i in range(2, 6)]
        sess, calls = self._calls("make quad", [plan] + bad + [{"action": "stop"}, {"action": "stop"}],
                                  run_worker=worker(["double"]))
        self.assertEqual(calls["split"], True)
        rewrites = [e["deep"] for e in sess.events if e["kind"] == "model_call" and e["label"] == "rewrite"]
        self.assertEqual(rewrites, [False, True])
        self.assertEqual(calls["repair"], False)
        self.assertEqual(next(e for e in sess.events if e["kind"] == "summary")["thinking"]["calls"], 2)

    def test_thinking_is_rationed_per_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", lambda s, u: ag._fake({"action": "stop"}),
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"), log_path=Path(tmp) / "l.jsonl")
            sess.max_calls = ag.MAX_DEEP_CALLS + 3
            for _ in range(ag.MAX_DEEP_CALLS + 2):
                sess._ask("p", "probe", deep=True)
            deep = [e["deep"] for e in sess.events if e["kind"] == "model_call"]
            self.assertEqual(deep, [True] * ag.MAX_DEEP_CALLS + [False, False])

    def test_the_reply_event_says_how_the_thinking_went(self):
        def gen(system, user):
            return dict(ag._fake({"action": "stop"}), thinking="low", reasoning_tokens=1800,
                        reasoning="The spec asks for a pure function. " * 40)
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              log_path=Path(tmp) / "l.jsonl")
            sess._ask("p", "probe", deep=True)
            sess._ask("p", "probe")
            deep, plain = [e for e in sess.events if e["kind"] == "model_reply"]
            self.assertEqual((deep["thinking"], deep["reasoning_tokens"], deep["thinking_fallback"]),
                             ("low", 1800, False))
            self.assertEqual(len(deep["reasoning"]), 600)
            self.assertNotIn("thinking", plain)
            self.assertEqual(sess._think, {"calls": 1, "fallbacks": 0, "reasoning_tokens": 1800})

    def test_the_client_can_switch_off_the_sdk_retries_and_reports_reasoning(self):
        import cerebras_client
        usage = mock.Mock(prompt_tokens=10, completion_tokens=50,
                          completion_tokens_details=mock.Mock(reasoning_tokens=40))
        done = mock.Mock(choices=[mock.Mock(finish_reason="stop", message=mock.Mock(
            content="hi", reasoning="because"))], usage=usage, model="m", id="r1")
        client = mock.Mock()
        client.with_options.return_value.chat.completions.create.return_value = done
        with mock.patch.object(cerebras_client, "get_client", return_value=client):
            res = cerebras_client.generate("p", reasoning_effort="low", max_retries=0)
        client.with_options.assert_called_once_with(max_retries=0)
        self.assertEqual((res["text"], res["reasoning_tokens"], res["reasoning"]), ("hi", 40, "because"))


if __name__ == "__main__":
    unittest.main()
