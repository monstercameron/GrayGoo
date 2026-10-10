"""Cancelling a build is transactional at every stage of the pipeline.

A session is cancelled from inside one stage at a time: from the scripted model
(plan, step, repair, split, look, fix), from the fake Lisp worker (rehearsal,
regression check, smoke check) or from the fake screenshot function. Each run is
then held to the same invariants (``assert_clean_cancel``): what was saved before
the request stays saved, nothing half-validated is saved, no model call starts
after the request, and the session ends as cancelled within 10 seconds.

Four tests found bugs when this file was written; their comments say what was wrong.
"""
import json
import re
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
import visualcheck  # noqa: E402

PNG = bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"rest"
PAGES = [{"label": "GET /", "path": "/", "status": 200, "html": "<h1>x</h1>"}]
SMOKE_MARK = "prin1-to-string (getf resp :state)"      # only the smoke check's request has it
APP_CALL = "(handle-request '(:method \"GET\" :path \"/\") nil)"
APP_PROMPT = "make a website with one page"
APP_PLAN = {"action": "plan", "steps": [
    {"name": "handle-request", "spec": "(handle-request request state) -> the page"}]}
APP_BODY = ['(list :status 200 :body "one")', '(list :status 200 :body "two")']


def build_reply(name, body):
    """A build action for NAME with BODY, tested by one call that must return T."""
    if name == "handle-request":
        call, definition = APP_CALL, "(defun handle-request (request state) %s)" % body
    else:
        call, definition = "(%s 1)" % name, "(defun %s (x) %s)" % (name, body)
    return {"action": "build", "name": name, "description": "d", "definition": definition,
            "tests": [{"call": call, "expect": "T"}], "call": call}


def passed(name, body):
    """The definition as it is saved: the harness writes the description in as a docstring."""
    return ag.normalize_plan(build_reply(name, body))["definition"]


def parts_plan(*names):
    return {"action": "plan", "steps": [{"name": n, "spec": "(%s x) -> part %s" % (n, n)}
                                        for n in names]}


class Lisp:
    """The fake worker: every test passes unless a candidate is told to crash.

    A hook is a (marker, do) pair. It runs once, on the thread that evaluates the
    first form containing the marker, before that form's result is returned.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.hooks = []
        self.crash = {}           # candidate name -> rehearsals that still crash

    def hook(self, marker, do):
        with self.lock:
            self.hooks.append((marker, do))

    def __call__(self, code):
        with self.lock:
            due = [h for h in self.hooks if h[0] in code]
            for h in due:
                self.hooks.remove(h)
            crashed = next((n for n, left in self.crash.items()
                            if left > 0 and "(gg-check (%s " % n in code), None)
            if crashed:
                self.crash[crashed] -= 1
        for _, do in due:
            do()
        if SMOKE_MARK in code:
            return {"ok": True, "elapsed_ms": 1.0,
                    "return_value": '(200 NIL "<h1>ok</h1>" 0 "NIL")'}
        if crashed:
            return {"ok": False, "stdout": "", "error": "The variable X is unbound.",
                    "timed_out": False, "elapsed_ms": 1.0, "return_value": ""}
        return {"ok": True, "stdout": "", "error": "", "timed_out": False,
                "elapsed_ms": 1.0, "return_value": "T"}


class Model:
    """A thread-safe scripted model. It answers by what the prompt asks for, records
    every call it receives, and can run hooks (once each) when a prompt matches."""

    def __init__(self, plan, bodies=None, delay=0.0):
        self.plan = plan
        self.bodies = bodies or {}
        self.delay = delay
        self.look = {"done": True, "problems": [], "fix": ""}
        self.fix = None
        self.cond = threading.Condition()
        self.calls = []           # the prompts of the calls, in the order they started
        self.active = 0           # calls in flight right now
        self.used = {}            # name -> builds asked for so far
        self.hooks = []           # (when(system, user), do())
        self.raw = []             # (when(system, user), text): answered once, not as JSON

    def hook(self, when, do):
        self.hooks.append((when, do))

    def raw_reply(self, when, text):
        self.raw.append((when, text))

    def __call__(self, system, user):
        with self.cond:
            self.calls.append(user)
            self.active += 1
            self.cond.notify_all()
        try:
            with self.cond:
                due = [h for h in self.hooks if h[0](system, user)]
                for h in due:
                    self.hooks.remove(h)
                odd = next((r for r in self.raw if r[0](system, user)), None)
                if odd:
                    self.raw.remove(odd)
            for _, do in due:
                do()
            if odd:
                return dict(ag._fake({}), text=odd[1])
            if self.delay:
                time.sleep(self.delay)
            return ag._fake(self.answer(system, user))
        finally:
            with self.cond:
                self.active -= 1
                self.cond.notify_all()

    def answer(self, system, user):
        if system == ag.VISUAL_SYSTEM:
            return self.look
        if "AND SHOW THESE PROBLEMS" in user:
            return self.fix
        m = re.search(r"GOAL: \((\S+) ", user)
        name = m.group(1) if m else None
        if name and "Split it into" in user:
            return {"action": "plan", "steps": [{"name": name + "-a",
                                                 "spec": "(%s-a x) -> part" % name}]}
        if name and any(k in user for k in ("PREVIOUS ATTEMPT FAILED", "BUILD exactly this one",
                                            "Now build the ORIGINAL")):
            return self.build(name)
        return self.plan

    def build(self, name):
        with self.cond:
            n = self.used.get(name, 0)
            self.used[name] = n + 1
        bodies = self.bodies.get(name) or ["(list 1 x)"]
        return build_reply(name, bodies[min(n, len(bodies) - 1)])


class CancelHarness(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(visualcheck, "collect_pages",
                                    lambda app, limit=3: [dict(p) for p in PAGES])
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, model, prompt, *, pre=(), parallel=1, visual=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        path = folder / "tools.json"
        path.write_text("[]", encoding="utf-8")
        reg = ag.ToolRegistry(path)
        for tool in pre:
            reg.add(tool)
        self.pre = {t["name"] for t in reg.load() if not t.get("kit")}
        self.lisp = Lisp()
        sess = ag.Session(prompt, model, registry=reg, worker_fn=self.lisp,
                          log_path=folder / "log.jsonl")
        sess.parallel = parallel
        sess.visual = visual
        sess.shots_dir = folder / "shots"
        return sess

    def capture(self, sess, on_first=None):
        """A screenshot function that writes a picture; ON_FIRST runs once, inside the capture."""
        shots = []

        def capture_fn(html, out):
            shots.append(out)
            if len(shots) == 1 and on_first:
                on_first()
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(PNG)
            return {"ok": True, "path": str(out), "error": "", "ms": 1.0}
        sess.capture_fn = capture_fn
        return shots

    def run_session(self, sess):
        thread = threading.Thread(target=sess.run, daemon=True)
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive(), "the session thread was still alive 10 s after the cancel")

    def assert_clean_cancel(self, sess, model, saved, defs=None):
        """Invariants A to G of a session cancelled in the middle of a build.

        SAVED: the names the session is expected to have saved in total.
        DEFS: the definition that passed, for names promoted before the request.
        """
        ev = sess.events
        kinds = [e["kind"] for e in ev]
        # A: the state, the event order, no error and no give-up
        self.assertEqual(sess.state, "cancelled")
        self.assertIn("cancel_requested", kinds)
        self.assertIn("cancelled", kinds)
        req, end = kinds.index("cancel_requested"), kinds.index("cancelled")
        self.assertLess(req, end)
        self.assertEqual(kinds[-2:], ["summary", "done"])
        self.assertEqual(ev[-2]["outcome"], "cancelled")
        self.assertEqual(ev[-1]["state"], "cancelled")
        self.assertFalse({"error", "gave_up"} & set(kinds), kinds)
        self.assertFalse(any(k in ("verdict", "promoted") for k in kinds[end:]),
                         "a verdict or a promotion came after the cancelled event")
        # B: what was promoted before the request is still saved, as it passed
        reg = {t["name"]: t for t in sess.registry.load() if not t.get("kit")}
        before = [e["name"] for e in ev[:req] if e["kind"] == "promoted"]
        for name in before:
            self.assertIn(name, reg, "%s was verified before the cancel and is lost" % name)
            if defs and name in defs:
                self.assertEqual(reg[name]["definition"], defs[name])
        # C: every new saved function has a passing verdict in its own lane
        promoted = [e["name"] for e in ev if e["kind"] == "promoted"]
        for i, e in enumerate(ev):
            if e["kind"] != "promoted":
                continue
            self.assertLess(i, end)
            prior = [v for v in ev[:i] if v["kind"] == "verdict" and v.get("lane") == e.get("lane")]
            self.assertTrue(prior and prior[-1].get("ok") is True,
                            "%s was promoted without a passing verdict" % e["name"])
        for name in set(reg) - self.pre:
            self.assertIn(name, promoted, "%s was saved without a promotion" % name)
        # D: the file on disk is the registry
        on_disk = json.loads(sess.registry.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, sess.registry.load(retired=True))
        # E: no model call starts after the request, and the count stays put
        announced = sum(1 for e in ev[:end] if e["kind"] == "model_call")
        self.assertFalse(any(e["kind"] == "model_call" for e in ev[req:]),
                         "a model call was started after the cancel request")
        self.assertEqual(announced, len(model.calls))
        time.sleep(0.3)
        self.assertEqual(len(model.calls), announced)
        # F: the summary and the cancelled event name exactly the saved functions
        built = sorted(set(b["name"] for b in sess._summary()["built"]))
        self.assertEqual(built, sorted(set(promoted)))
        self.assertEqual(sorted(set(ev[end]["saved"])), built)
        self.assertEqual(sorted(set(saved)), built)
        # G: the session ended promptly after the request
        self.assertLess(ev[-1]["t"] - ev[req]["t"], 10.0)


class PipelineCancelTests(CancelHarness):
    def test_1_cancel_during_the_plan_call(self):
        model = Model(parts_plan("one", "two"))
        sess = self.session(model, "make two parts")
        model.hook(lambda s, u: "GOAL: make two parts" in u, sess.cancel)
        self.run_session(sess)
        self.assert_clean_cancel(sess, model, saved=[])
        self.assertEqual(len(model.calls), 1)
        self.assertFalse(any(e["kind"] in ("step", "promoted") for e in sess.events))

    def test_2_cancel_during_a_step_call_in_order(self):
        model = Model(parts_plan("one", "two"), {"one": ["(list 1 x)"], "two": ["(list 2 x)"]})
        sess = self.session(model, "make two parts")
        model.hook(lambda s, u: "GOAL: (one " in u, sess.cancel)
        self.run_session(sess)
        self.assert_clean_cancel(sess, model, saved=[])
        self.assertEqual(len(model.calls), 2)                       # the plan and the first step
        self.assertEqual(sess.registry.load(), [])

    def test_3_cancel_during_a_step_call_while_other_lanes_are_in_flight(self):
        model = Model(parts_plan("one", "two", "three"),
                      {"one": ["(list 1 x)"], "two": ["(list 2 x)"], "three": ["(list 3 x)"]},
                      delay=0.2)
        sess = self.session(model, "make three parts", parallel=3)
        seen = []

        def cancel_with_company():
            with model.cond:
                seen.append(model.cond.wait_for(lambda: model.active >= 3, timeout=5))
            sess.cancel()
        model.hook(lambda s, u: "GOAL: (two " in u, cancel_with_company)
        self.run_session(sess)
        self.assertEqual(seen, [True])                              # all three calls were in flight
        self.assert_clean_cancel(sess, model, saved=[])
        self.assertEqual(len(model.calls), 4)                       # the plan and three steps
        self.assertTrue(any(e["kind"] == "parallel" for e in sess.events))

    def test_4_cancel_during_the_rehearsal_of_the_candidate_under_test(self):
        # The candidate "one" is under test when the cancel arrives. Its tests all run
        # and pass, so its verdict is complete when it is saved: it IS saved, and the
        # session stops before the next step. (Not a bug: nothing half-validated.)
        model = Model(parts_plan("one", "two"), {"one": ["(list 1 x)"], "two": ["(list 2 x)"]})
        sess = self.session(model, "make two parts")
        self.lisp.hook("(gg-check (one ", sess.cancel)
        self.run_session(sess)
        ev = sess.events
        kinds = [e["kind"] for e in ev]
        req = kinds.index("cancel_requested")
        self.assertLess(req, kinds.index("verdict"))                 # the cancel landed mid-rehearsal
        self.assertTrue(all(e.get("ok") is True for e in ev if e["kind"] == "verdict"))
        promoted = [i for i, e in enumerate(ev) if e["kind"] == "promoted"]
        self.assertEqual([ev[i]["name"] for i in promoted], ["one"])
        self.assertGreater(promoted[0], req)                         # saved after the request
        self.assert_clean_cancel(sess, model, saved=["one"],
                                 defs={"one": passed("one", "(list 1 x)")})
        self.assertEqual(len(model.calls), 2)

    def test_5_cancel_during_a_repair_call(self):
        model = Model(parts_plan("one", "two"),
                      {"one": ["(list 1 x)", "(list 2 x)"], "two": ["(list 3 x)"]})
        sess = self.session(model, "make two parts")
        self.lisp.crash["one"] = 1                                   # the first candidate crashes
        model.hook(lambda s, u: "PREVIOUS ATTEMPT FAILED" in u, sess.cancel)
        self.run_session(sess)
        self.assert_clean_cancel(sess, model, saved=[])
        labels = [e["label"] for e in sess.events if e["kind"] == "model_call"]
        self.assertEqual(labels[-1], "repair")
        self.assertEqual(sum(1 for e in sess.events if e["kind"] == "verdict"), 1)  # the repair was not run

    def test_6_cancel_during_a_split_call(self):
        model = Model(parts_plan("page"),
                      {"page": ["(list 1 x)", "(list 2 x)", "(list 3 x)", "(list 4 x)"]})
        sess = self.session(model, "make one page")
        self.lisp.crash["page"] = 99                                 # every candidate crashes
        model.hook(lambda s, u: "Split it into" in u, sess.cancel)
        self.run_session(sess)
        self.assert_clean_cancel(sess, model, saved=[])
        kinds = [e["kind"] for e in sess.events]
        self.assertIn("replan", kinds)                               # the split was asked for
        labels = [e["label"] for e in sess.events if e["kind"] == "model_call"]
        self.assertEqual(labels[-1], "split")
        self.assertEqual(sess.registry.load(), [])

    def test_7_cancel_during_the_regression_check_of_a_replaced_function(self):
        # The replacement of "inner" passes its own tests; the regression check of
        # "outer", which calls it, runs when the cancel arrives. The check completes,
        # so the replacement is saved complete and verified. The next step never runs.
        pre = [
            {"name": "inner", "description": "d", "definition": "(defun inner (x) (* 2 x))",
             "tests": [{"call": "(inner 1)", "expect": "2"}], "call": "(inner 1)"},
            {"name": "outer", "description": "d", "definition": "(defun outer (x) (inner x))",
             "tests": [{"call": "(outer 1)", "expect": "T"}], "call": "(outer 1)"}]
        model = Model(parts_plan("inner", "other"),
                      {"inner": ["(+ x x)"], "other": ["(list 3 x)"]})
        sess = self.session(model, "make inner twice", pre=pre)
        self.lisp.hook("(gg-check (outer ", sess.cancel)
        self.run_session(sess)
        kinds = [e["kind"] for e in sess.events]
        self.assertGreater(
            [i for i, e in enumerate(sess.events) if e["kind"] == "repl" and e.get("label") == "regression"][-1],
            kinds.index("cancel_requested"))
        inner = next(t for t in sess.registry.load() if t["name"] == "inner")
        self.assertEqual(inner["definition"], passed("inner", "(+ x x)"))
        outer = next(t for t in sess.registry.load() if t["name"] == "outer")
        self.assertEqual(outer["definition"], "(defun outer (x) (inner x))")
        self.assert_clean_cancel(sess, model, saved=["inner"])

    def test_8_cancel_during_the_smoke_check(self):
        # Was a bug: nothing after the smoke check looked at the cancel flag, so the
        # session ended as "done". _run_guarded now checks it once _run returns.
        model = Model(APP_PLAN, {"handle-request": APP_BODY})
        sess = self.session(model, APP_PROMPT, visual=False)
        self.lisp.hook(SMOKE_MARK, sess.cancel)
        self.run_session(sess)
        self.assert_clean_cancel(sess, model, saved=["handle-request"],
                                 defs={"handle-request": passed("handle-request", APP_BODY[0])})

    def test_9_cancel_while_the_screenshots_are_taken(self):
        model = Model(APP_PLAN, {"handle-request": APP_BODY})
        sess = self.session(model, APP_PROMPT, visual=True)
        shots = self.capture(sess, on_first=sess.cancel)
        self.run_session(sess)
        self.assertEqual(len(shots), 1)
        self.assertTrue(any(e["kind"] == "screenshot" for e in sess.events))
        self.assertNotIn("visual-review", [e["label"] for e in sess.events if e["kind"] == "model_call"])
        self.assert_clean_cancel(sess, model, saved=["handle-request"],
                                 defs={"handle-request": passed("handle-request", APP_BODY[0])})

    def test_10_cancel_during_the_model_call_that_looks_at_the_screenshots(self):
        model = Model(APP_PLAN, {"handle-request": APP_BODY})
        model.look = {"done": False, "problems": ["the page is plain"], "fix": ""}
        sess = self.session(model, APP_PROMPT, visual=True)
        self.capture(sess)
        model.hook(lambda s, u: s == ag.VISUAL_SYSTEM, sess.cancel)
        self.run_session(sess)
        kinds = [e["kind"] for e in sess.events]
        self.assertEqual(kinds.count("screenshot"), 1)
        self.assertNotIn("visual_review", kinds)                     # the look's answer was not used
        self.assertNotIn("visual_fix", kinds)
        self.assertEqual([e["label"] for e in sess.events if e["kind"] == "model_call"][-1], "visual-review")
        self.assert_clean_cancel(sess, model, saved=["handle-request"],
                                 defs={"handle-request": passed("handle-request", APP_BODY[0])})

    def _fix_setup(self):
        model = Model(APP_PLAN, {"handle-request": APP_BODY})
        model.look = {"done": False, "problems": ["the page is plain"], "fix": ""}
        model.fix = {"action": "plan", "steps": [
            {"name": "handle-request", "spec": "(handle-request request state) -> styled page"}]}
        sess = self.session(model, APP_PROMPT, visual=True)
        self.capture(sess)
        return model, sess

    def test_11_cancel_during_the_planning_of_the_round_of_fixes(self):
        model, sess = self._fix_setup()
        model.hook(lambda s, u: "AND SHOW THESE PROBLEMS" in u, sess.cancel)
        self.run_session(sess)
        self.assertIn("visual_fix", [e["kind"] for e in sess.events])
        labels = [e["label"] for e in sess.events if e["kind"] == "model_call"]
        self.assertEqual(labels[-1], "visual-fix")
        self.assert_clean_cancel(sess, model, saved=["handle-request"],
                                 defs={"handle-request": passed("handle-request", APP_BODY[0])})

    def test_13_cancel_called_twice_on_a_session_changes_nothing_the_second_time(self):
        model = Model(parts_plan("one"), {"one": ["(list 1 x)"]})
        sess = self.session(model, "make one part")
        answers = []
        model.hook(lambda s, u: "GOAL: make one part" in u,
                   lambda: answers.append((sess.cancel(), sess.cancel())))
        self.run_session(sess)
        self.assertEqual(answers, [(True, False)])
        self.assertEqual(sum(1 for e in sess.events if e["kind"] == "cancel_requested"), 1)
        self.assert_clean_cancel(sess, model, saved=[])

    def test_11b_cancel_during_the_build_of_a_fix(self):
        model, sess = self._fix_setup()
        model.hook(lambda s, u: "SCREENSHOTS OF THE APP AS IT IS NOW SHOW THESE PROBLEMS" in u,
                   sess.cancel)
        self.run_session(sess)
        labels = [e["label"] for e in sess.events if e["kind"] == "model_call"]
        self.assertEqual(labels[-1], "step")
        # the first version was verified and saved before the request; the fix never was
        self.assert_clean_cancel(sess, model, saved=["handle-request"],
                                 defs={"handle-request": passed("handle-request", APP_BODY[0])})


class LateCancelTests(CancelHarness):
    def test_a_cancel_during_the_last_rehearsal_is_not_dropped(self):
        # Was a bug: a single build has no model call after its rehearsal, so a cancel
        # during that rehearsal ended as "done". The tool that passed stays saved.
        model = Model(build_reply("one", "(list 1 x)"))
        sess = self.session(model, "make one part")
        self.lisp.hook("(gg-check (one ", sess.cancel)
        self.run_session(sess)
        self.assertEqual(sess.state, "cancelled")
        self.assertIn("cancelled", [e["kind"] for e in sess.events])

    def test_a_retry_reply_in_flight_at_the_cancel_is_not_built(self):
        # Was a bug: only the first reply of a call was checked against the cancel
        # flag; the reply of an invalid-JSON retry was rehearsed and saved.
        model = Model(parts_plan("one", "two"), {"one": ["(list 1 x)"], "two": ["(list 2 x)"]})
        sess = self.session(model, "make two parts")
        model.raw_reply(lambda s, u: "GOAL: (one " in u and "YOUR PREVIOUS" not in u,
                        "{ action: build")
        model.hook(lambda s, u: "YOUR PREVIOUS REPLY WAS CUT OFF" in u, sess.cancel)
        self.run_session(sess)
        kinds = [e["kind"] for e in sess.events]
        req = kinds.index("cancel_requested")
        self.assertFalse(any(e["kind"] == "promoted" and i > req
                             for i, e in enumerate(sess.events)),
                         "the reply that was in flight at the cancel was built and saved")
        self.assert_clean_cancel(sess, model, saved=[])


class ManagerCancelTests(CancelHarness):
    def wait_idle(self, mgr):
        deadline = time.time() + 10
        while mgr.busy() and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(mgr.busy(), "the manager stayed busy 10 s after the cancel")

    def manager(self, tmp):
        return ag.SessionManager(ag.ToolRegistry(Path(tmp) / "tools.json"))

    def test_12_cancel_through_the_manager_while_a_compared_session_runs(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ag, "AGENT_DIR", Path(tmp)):
            mgr = self.manager(tmp)
            started, box, calls, answers = threading.Event(), [], [], []

            def gen(system, user):
                started.wait(10)
                calls.append(user)
                if len(calls) == 1:
                    answers.append(mgr.cancel())
                return ag._fake({"action": "use", "call": "(nothing 1)"})
            mgr.generators["demo"] = gen
            sid, err = mgr.start("make one part", mode="demo", compare=True)
            self.assertIsNone(err)
            box.append(sid)
            started.set()
            self.wait_idle(mgr)
            self.assertEqual(answers, [(True, sid)])
            snap = mgr.get(sid)
            self.assertEqual(snap["state"], "cancelled")
            self.assertIsNone(snap["compare"])                       # the twin is not waited for
            self.assertEqual(len(calls), 1)                          # the no-memory twin never ran

    def test_13m_cancelling_twice_or_after_the_end_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ag, "AGENT_DIR", Path(tmp)):
            mgr = self.manager(tmp)
            started, box, answers = threading.Event(), [], []

            def gen(system, user):
                started.wait(10)
                answers.append((mgr.cancel(), mgr.cancel()))
                return ag._fake({"action": "use", "call": "(nothing 1)"})
            mgr.generators["demo"] = gen
            sid, _ = mgr.start("make one part", mode="demo")
            box.append(sid)
            started.set()
            self.wait_idle(mgr)
            self.assertEqual(answers, [((True, sid), (False, None))])
            snap = mgr.get(sid)
            self.assertEqual(snap["state"], "cancelled")
            events = len(snap["events"])
            self.assertEqual(mgr.cancel(), (False, None))            # after the end: nothing happens
            self.assertEqual(mgr.get(sid)["state"], "cancelled")
            self.assertEqual(len(mgr.get(sid)["events"]), events)

    def test_13b_cancel_after_a_finished_session_changes_nothing(self):
        model = Model(build_reply("one", "(list 1 x)"))
        sess = self.session(model, "make one part")
        self.run_session(sess)
        self.assertEqual(sess.state, "done")
        events, registry = len(sess.events), sess.registry.load()
        self.assertFalse(sess.cancel())
        self.assertFalse(sess.cancel())
        self.assertEqual(sess.state, "done")
        self.assertEqual(len(sess.events), events)
        self.assertEqual(sess.registry.load(), registry)

    def test_the_no_memory_twin_can_be_cancelled_through_the_manager(self):
        # Was a bug: the no-memory twin was not known to SessionManager.cancel(), so
        # it could not be stopped. The manager now tracks it while it runs.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ag, "AGENT_DIR", Path(tmp)):
            mgr = self.manager(tmp)
            started, box, answers, twin = threading.Event(), [], [], []

            def gen(system, user):
                started.wait(10)
                snap = mgr.get(box[0])
                if snap["state"] != "running" and snap["compare"] == "running":
                    twin.append(user)                                # the twin's model calls
                    if len(twin) == 1:
                        answers.append(mgr.cancel())
                return ag._fake({"action": "use", "call": "(nothing 1)"})
            mgr.generators["demo"] = gen
            sid, _ = mgr.start("make one part", mode="demo", compare=True)
            box.append(sid)
            started.set()
            self.wait_idle(mgr)
            self.assertTrue(answers and answers[0][0],
                            "the running no-memory twin could not be cancelled")
            self.assertEqual(len(twin), 1)


if __name__ == "__main__":
    unittest.main()
