"""Guards drawn from five failed Continue attempts on one live project (2026-10-10).

Builds 550876d320, 6c3f289bf5, 950fccf453, ae7bcff4ac and 9503ef3968: an empty model
reply ended a whole build, a prompt sent again rebuilt functions that were saved, a
build was started with two cents left, and test values with doubled escapes were
rejected as unbalanced. Real SBCL, scripted model.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402

PROMPT = "a command line colour tool: the command mix A B prints the two names joined"
ENTRY = {"action": "build", "name": "handle-command", "description": "Runs one command",
         "definition": '(defun handle-command (args state now) "Runs one command." '
                       '(declare (ignore state now)) (list :output (if (equal (first args) "mix") '
                       '(join-two (second args) (third args)) "commands: mix, help")))',
         "call": "(getf (handle-command '(\"help\") '() 0) :output)",
         "tests": [{"call": "(getf (handle-command '(\"help\") '() 0) :output)",
                    "expect": '"commands: mix, help"'}]}
JOIN = {"action": "build", "name": "join-two", "description": "Joins two words with a dash",
        "definition": '(defun join-two (a b) "Joins two words with a dash." '
                      '(concatenate (quote string) a "-" b))',
        "call": '(join-two "red" "blue")',
        "tests": [{"call": '(join-two "red" "blue")', "expect": '"red-blue"'}]}
PLAN = {"action": "plan", "steps": [
    {"name": "join-two", "spec": '(join-two "red" "blue") => "red-blue"'},
    {"name": "handle-command", "spec": "(handle-command args state now) -> plist"}]}


def empty_reply():
    reply = ag._fake({"action": "stop"})
    reply["text"] = ""
    return reply


class Script:
    def __init__(self, empty_until_recovery=False):
        self.empty, self.prompts, self.recovering = empty_until_recovery, [], False

    def __call__(self, system, user):
        self.prompts.append(user)
        if "RECOVERY - these planned functions" in user:
            self.recovering = True
            return ag._fake({"action": "plan", "steps": [PLAN["steps"][0]]})
        if "GOAL: (handle-command " in user:
            return ag._fake(ENTRY)
        if "GOAL: (join-two " in user:
            return empty_reply() if self.empty and not self.recovering else ag._fake(JOIN)
        return ag._fake(PLAN)


def session(script, tmp, **attrs):
    sess = ag.Session(PROMPT, script, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                      log_path=Path(tmp) / "l.jsonl")
    sess.visual = False
    for key, value in attrs.items():
        setattr(sess, key, value)
    return sess


class EmptyReplyTests(unittest.TestCase):
    def test_an_empty_reply_costs_one_function_and_the_build_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Script(empty_until_recovery=True)
            sess = session(script, tmp)
            sess.run()
            kinds = [e["kind"] for e in sess.events]
            self.assertNotIn("error", kinds)                         # it used to end the whole build
            bad = next(e for e in sess.events if e["kind"] == "bad_reply")
            self.assertEqual(bad["name"], "join-two")
            self.assertIn("recovery", kinds)
            self.assertEqual(sess.state, "done")
            self.assertEqual(sorted(t["name"] for t in sess.registry.load() if not t.get("kit")),
                             ["handle-command", "join-two"])


class RepeatPromptTests(unittest.TestCase):
    def _saved(self, tmp):
        reg = ag.ToolRegistry(Path(tmp) / "t.json")
        reg.add({"name": "join-two", "description": JOIN["description"], "definition": JOIN["definition"],
                 "tests": JOIN["tests"], "call": JOIN["call"]})
        return reg

    def test_a_prompt_sent_again_does_not_rebuild_what_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._saved(tmp)
            script = Script()
            sess = session(script, tmp, prior_goals=[PROMPT])
            sess.run()
            trimmed = next(e for e in sess.events if e["kind"] == "plan_trimmed")
            self.assertEqual(trimmed["kept_as_saved"], ["join-two"])
            self.assertFalse([p for p in script.prompts if "GOAL: (join-two " in p])   # never asked for
            self.assertEqual(sess.state, "done")
            self.assertEqual([n for n, _ in sess._built], ["handle-command"])

    def test_a_new_prompt_may_change_a_saved_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._saved(tmp)
            script = Script()
            sess = session(script, tmp, prior_goals=["an earlier, different prompt"])
            sess.run()
            self.assertNotIn("plan_trimmed", [e["kind"] for e in sess.events])
            self.assertTrue([p for p in script.prompts if "GOAL: (join-two " in p])

    def test_a_plan_of_saved_functions_only_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(Script(), tmp, prior_goals=[PROMPT])
            sess.registry.add({"name": "join-two", "description": "d", "definition": JOIN["definition"]})
            plan = {"action": "plan", "steps": [PLAN["steps"][0], PLAN["steps"][1]]}
            only_saved = {"action": "plan", "steps": [PLAN["steps"][0]]}
            self.assertEqual(sess._trim_repeat(only_saved), only_saved)
            trimmed = sess._trim_repeat(plan)
            self.assertEqual([x["name"] for x in trimmed["steps"]], ["handle-command"])   # the entry point may rewire
            sess._recovering = True
            self.assertEqual(sess._trim_repeat(plan), plan)


class EscapedQuoteTests(unittest.TestCase):
    def test_a_value_whose_every_quote_is_escaped_loses_the_backslashes(self):
        doubled = '(:output \\"Saved (1)\\" :state ((\\"loans\\" ((1 100)))))'
        self.assertEqual(ag.unescape_quotes(doubled), '(:output "Saved (1)" :state (("loans" ((1 100)))))')

    def test_a_value_with_a_real_string_is_left_alone(self):
        for text in ('"He said \\"hi\\""', '(:output "a \\"b\\" c")', "(1 2 3)", '"plain"', None, 5):
            self.assertEqual(ag.unescape_quotes(text), text)

    def test_the_repair_is_applied_and_named_when_a_plan_is_normalised(self):
        plan = ag.normalize_plan({
            "action": "build", "name": "f", "description": "d",
            "definition": '(defun f () "d" (list :output "Saved (1)"))', "call": "(f)",
            "tests": [{"call": "(f)", "expect": '(:output \\"Saved (1)\\")'}]})
        self.assertEqual(plan["tests"][0]["expect"], '(:output "Saved (1)")')
        self.assertIn("unescaped-quotes-in-expected-value", plan["auto_fixes"])
        self.assertIsNone(ag.validate_build(plan))


class SpendLimitTests(unittest.TestCase):
    def manager(self, tmp):
        mgr = ag.SessionManager(registry=ag.ToolRegistry(Path(tmp) / "tools.json"))
        mgr.generators["live"] = lambda system, user: ag._fake({"action": "stop"})
        return mgr

    def test_no_build_is_started_on_what_is_left_of_a_used_up_limit(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ag, "AGENT_DIR", Path(tmp)), \
                mock.patch.object(ag, "live_status", return_value={"available": True, "reason": ""}):
            mgr = self.manager(tmp)
            mgr.live_spend = mgr.live_cap - 0.02                 # two cents left: not a build
            sid, err = mgr.start("square 12", mode="live")
            self.assertIsNone(sid)
            self.assertIn("live spending limit reached: $0.98 of the $1.00", err)
            self.assertIn("Allow more to go on", err)

    def test_the_user_allows_more_one_step_at_a_time(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ag, "AGENT_DIR", Path(tmp)):
            mgr = self.manager(tmp)
            self.assertEqual(mgr.allow_spend(), 2.0)
            self.assertEqual(mgr.allow_spend(50), 2.0)            # never more than one step per acknowledgement
            self.assertEqual(mgr.allow_spend("x"), 2.0)
            self.assertEqual(mgr.allow_spend(), 3.0)              # and no ceiling: the user decides each time

    def test_a_build_gets_what_is_left_and_never_more_than_one_builds_limit(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ag, "AGENT_DIR", Path(tmp)), \
                mock.patch.object(ag, "live_status", return_value={"available": True, "reason": ""}):
            mgr = self.manager(tmp)
            mgr.live_spend = 0.75
            sid, err = mgr.start("square 12", mode="live")
            self.assertIsNone(err)
            self.assertAlmostEqual(mgr._sessions[sid].max_usd, 0.25)
            for _ in range(200):
                if not mgr.busy():
                    break
                __import__("time").sleep(0.02)


class SpendPauseTests(unittest.TestCase):
    """At its spending limit a build waits for the user instead of ending."""

    def paused_session(self, tmp):
        calls = []

        def gen(system, user):
            calls.append(user)
            return dict(ag._fake({"action": "stop"}), cost_usd=0.25)
        sess = ag.Session("square 12", gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                          log_path=Path(tmp) / "l.jsonl")
        sess.pause_on_spend, sess.max_usd = True, 0.20
        return sess, calls

    def wait_for(self, test, seconds=5.0):
        import time
        deadline = time.time() + seconds
        while time.time() < deadline:
            if test():
                return True
            time.sleep(0.01)
        return False

    def test_the_build_waits_and_goes_on_when_the_user_allows_more(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            sess, calls = self.paused_session(tmp)
            sess._ask("p", "probe")                               # $0.25 spent: at the limit now
            done = []
            thread = threading.Thread(target=lambda: done.append(sess._ask("p", "probe")), daemon=True)
            thread.start()
            self.assertTrue(self.wait_for(lambda: sess.paused is not None))
            self.assertEqual(len(calls), 1)                        # nothing is spent while it waits
            self.assertEqual(sess.snapshot()["paused"]["limit_usd"], 0.2)
            self.assertEqual(sess.state, "running")
            self.assertTrue(sess.allow_more_spend())
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(calls), 2)
            self.assertIsNone(sess.paused)
            self.assertAlmostEqual(sess.max_usd, 0.25 + ag.MAX_SESSION_USD)
            kinds = [e["kind"] for e in sess.events]
            self.assertLess(kinds.index("spend_pause"), kinds.index("spend_resumed"))
            self.assertFalse(sess.allow_more_spend())               # nothing is waiting any more

    def test_a_paused_build_can_be_stopped_and_keeps_what_it_saved(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            sess, calls = self.paused_session(tmp)
            sess._ask("p", "probe")
            out = []

            def blocked():
                try:
                    sess._ask("p", "probe")
                except ag.Cancelled:
                    out.append("cancelled")
            thread = threading.Thread(target=blocked, daemon=True)
            thread.start()
            self.assertTrue(self.wait_for(lambda: sess.paused is not None))
            self.assertTrue(sess.cancel())
            thread.join(5)
            self.assertEqual(out, ["cancelled"])
            self.assertEqual(len(calls), 1)

    def test_the_wait_does_not_count_against_the_time_limit(self):
        import threading
        import time
        with tempfile.TemporaryDirectory() as tmp:
            sess, _ = self.paused_session(tmp)
            sess._ask("p", "probe")
            started_at = sess._t0
            thread = threading.Thread(target=lambda: sess._ask("p", "probe"), daemon=True)
            thread.start()
            self.assertTrue(self.wait_for(lambda: sess.paused is not None))
            time.sleep(0.3)
            sess.allow_more_spend()
            thread.join(5)
            self.assertGreaterEqual(sess._t0 - started_at, 0.25)

    def test_a_build_with_nobody_to_ask_still_stops_at_its_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess, _ = self.paused_session(tmp)
            sess.pause_on_spend = False
            sess._ask("p", "probe")
            with self.assertRaises(ag.BudgetExhausted):
                sess._ask("p", "probe")

    def test_the_manager_resumes_the_waiting_build_and_raises_the_run_limit_to_fit(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ag, "AGENT_DIR", Path(tmp)):
            mgr = ag.SessionManager(registry=ag.ToolRegistry(Path(tmp) / "tools.json"))
            sess, _ = self.paused_session(tmp)
            sess.mode, sess.cost_usd = "live", 0.25
            sess.paused, mgr.live_spend = {"spent_usd": 0.25, "limit_usd": 0.2, "since": 0}, 0.9
            mgr._sessions[sess.id] = sess
            status = mgr.spend_status()
            self.assertEqual((status["spent_usd"], status["session_usd"], status["paused"]), (1.15, 0.25, True))
            resumed, status = mgr.acknowledge_spend()
            self.assertTrue(resumed)
            self.assertEqual(mgr.live_cap, round(0.9 + 0.25 + ag.MAX_SESSION_USD, 2))
            self.assertFalse(status["paused"])
            self.assertEqual(mgr.acknowledge_spend()[0], False)     # nothing is waiting now


if __name__ == "__main__":
    unittest.main()
