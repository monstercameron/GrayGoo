"""Refinements from live build 5ddd668f84 (2026-10-10, 52 calls, ended failed with budget left).

Its last planned step failed, which ended the build before any recovery or fix round;
the split of that step re-planned functions other lanes were building; three empty
replies in a row were answered by asking the same words again; reasoning that ran
out was followed by a plain call that almost never works; and a bare call expecting
T failed without the model being told why.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402

PROMPT = "a command line number tool: the command double N prints twice N"
DOUBLE = {"action": "build", "name": "cmd-double", "description": "Prints twice the number typed",
          "definition": '(defun cmd-double (args state now) "Prints twice the number typed." '
                        '(declare (ignore state now)) '
                        '(list :output (format nil "~a" (* 2 (parse-integer (first args))))))',
          "call": "(cmd-double '(\"4\") '() 0)",
          "tests": [{"call": "(cmd-double '(\"4\") '() 0)", "expect": '(:output "8")'}]}
ENTRY_TESTS = [{"call": "(handle-command '(\"help\") '() 0)", "expect": '(:output "commands: double, help")'},
               {"call": "(handle-command '(\"double\" \"4\") '() 0)", "expect": '(:output "8")'}]
GOOD_ENTRY = {"action": "build", "name": "handle-command", "description": "Runs one command",
              "definition": '(defun handle-command (args state now) "Runs one command." '
                            '(cond ((equal (first args) "help") (list :output "commands: double, help")) '
                            '((equal (first args) "double") (if (rest args) (cmd-double (rest args) state now) '
                            '(list :output "usage: double N"))) '
                            '(t (list :output "unknown command, try help"))))',
              "call": ENTRY_TESTS[0]["call"], "tests": ENTRY_TESTS}
BAD_ENTRY = dict(GOOD_ENTRY, definition='(defun handle-command (args state now) "Runs one command." '
                                        '(declare (ignore args state now)) (list :output "nothing works"))')
PLAN = {"action": "plan", "steps": [
    {"name": "cmd-double", "spec": "(cmd-double args state now) -> plist"},
    {"name": "handle-command", "spec": "(handle-command args state now) -> plist"}]}


class Script:
    """The entry point (the LAST step) is wrong until a recovery round asks for it again."""

    def __init__(self, recovers=True):
        self.recovers, self.prompts, self.recovering = recovers, [], False

    def __call__(self, system, user):
        self.prompts.append(user)
        if "RECOVERY - these planned functions" in user:
            self.recovering = True
            return ag._fake({"action": "plan", "steps": [PLAN["steps"][1]]})
        if "GOAL: (handle-command " in user:
            return ag._fake(GOOD_ENTRY if self.recovering and self.recovers else BAD_ENTRY)
        if "GOAL: (cmd-double " in user:
            return ag._fake(DOUBLE)
        return ag._fake(PLAN)


def run(script, tmp):
    sess = ag.Session(PROMPT, script, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                      log_path=Path(tmp) / "l.jsonl")
    sess.visual = False
    sess.run()
    return sess


class LastStepTests(unittest.TestCase):
    def test_a_failed_last_step_is_tried_again_instead_of_ending_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = run(Script(), tmp)
            kinds = [e["kind"] for e in sess.events]
            self.assertIn("recovery", kinds)                       # it used to stop before any recovery
            done = next(e for e in sess.events if e["kind"] == "recovery_done")
            self.assertEqual(done["rebuilt"], ["handle-command"])
            self.assertEqual(sess.state, "done")
            self.assertEqual(sess._summary()["qualification"]["verdict"], "proven")

    def test_an_app_whose_entry_point_never_gets_built_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = run(Script(recovers=False), tmp)
            self.assertEqual(sess.state, "failed")
            gave = [e for e in sess.events if e["kind"] == "gave_up"][-1]
            self.assertIn("The app has no entry point yet: handle-command could not be built", gave["detail"])
            self.assertIn("cmd-double", {t["name"] for t in sess.registry.load()})    # what worked is kept


class SplitTests(unittest.TestCase):
    def test_a_split_does_not_plan_the_functions_of_other_steps(self):
        asked = []

        def gen(system, user):
            asked.append(user)
            if "Split it into at most 3 SMALLER" in user:
                return ag._fake({"action": "plan", "steps": [
                    {"name": "cmd-double", "spec": "(cmd-double args state now)"},
                    {"name": "route-word", "spec": "(route-word word) -> symbol"},
                    {"name": "handle-command", "spec": "(handle-command args state now)"}]})
            return ag._fake(BAD_ENTRY if "handle-command" in user else
                            {"action": "build", "name": "route-word", "description": "d",
                             "definition": '(defun route-word (word) "d" word)', "call": '(route-word "a")',
                             "tests": [{"call": '(route-word "a")', "expect": '"b"'}]})

        def failing(code):
            return {"ok": True, "stdout": "", "error": "", "timed_out": False, "elapsed_ms": 1.0,
                    "return_value": '(:GOT "no")'}
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session(PROMPT, gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              worker_fn=failing, log_path=Path(tmp) / "l.jsonl")
            sess.max_calls = 30
            sess._lane_index = {"cmd-double": 0, "handle-command": 1}       # the plan's own steps
            sess._build_one(PLAN["steps"][1], 0)
            sub = next(e for e in sess.events if e["kind"] == "plan" and e.get("sub"))
            self.assertEqual([x["name"] for x in sub["steps"]], ["route-word", "handle-command"])
            self.assertFalse([p for p in asked if "GOAL: (cmd-double " in p])   # another lane's work


class BareCallTests(unittest.TestCase):
    def build(self, body, expect="T"):
        return {"action": "build", "name": "show-total", "description": "Shows a total",
                "definition": '(defun show-total (n) "Shows a total." %s)' % body,
                "call": "(show-total 3)", "tests": [{"call": "(show-total 3)", "expect": expect}]}

    def test_a_bare_call_that_expects_t_fails_and_is_told_why(self):
        replies = iter([self.build('(format nil "Total: ~a" n)'),
                        self.build('(format nil "Total: ~a" n)', '"Total: 3"')])
        prompts = []

        def gen(system, user):
            prompts.append(user)
            return ag._fake(next(replies))
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("show the total of 3", gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              log_path=Path(tmp) / "l.jsonl")
            sess.run()
            first = next(e for e in sess.events if e["kind"] == "verdict")
            self.assertFalse(first["ok"])                              # "Total: 3" is not T
            self.assertIn('got "Total: 3", expected T (this call returns that value, not T', first["detail"])
            self.assertIn("compare the result with something", prompts[1])
            self.assertEqual(sess.state, "done")                       # the corrected test passes

    def test_a_function_that_returns_t_still_passes_a_bare_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("is 3 small", lambda s, u: ag._fake(self.build("(< n 10)")),
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"), log_path=Path(tmp) / "l.jsonl")
            sess.run()
            self.assertEqual(sess.state, "done")
            self.assertTrue(next(e for e in sess.events if e["kind"] == "verdict")["ok"])

    def test_the_rescue_prompt_says_what_a_property_test_is(self):
        import inspect
        source = inspect.getsource(ag.Session._build_loop)
        self.assertIn("A property test WRAPS the call in a", source)
        self.assertIn("is NOT a property test", source)


class ThinkingRanOutTests(unittest.TestCase):
    def test_a_repair_whose_reasoning_ran_out_is_given_up_without_a_plain_call(self):
        calls = []

        def fake_generate(user, system=None, max_tokens=None, temperature=None, reasoning_effort=None, **kw):
            calls.append(reasoning_effort)
            return {"text": "", "finish_reason": "length", "input_tokens": 900, "output_tokens": 5000,
                    "reasoning_tokens": 5000, "cost_usd": 0.0084, "latency_ms": 4000.0, "model": "m"}
        import cerebras_client
        with mock.patch.object(ag, "live_status", return_value={"available": True, "reason": ""}), \
                mock.patch.object(cerebras_client, "generate", fake_generate):
            ag._TEMP.deep, ag._TEMP.think_tokens, ag._TEMP.no_fallback = True, ag.THINK_TOKENS_REPAIR, True
            try:
                res = ag.live_generate("system", "user")
            finally:
                ag._TEMP.deep, ag._TEMP.think_tokens, ag._TEMP.no_fallback = False, None, False
        self.assertEqual(calls, ["low"])                               # no second, plain call
        self.assertTrue(res["thinking_exhausted"])
        self.assertEqual((res["text"], res["reasoning_tokens"]), ("", 5000))

    def test_a_split_still_gets_its_plain_answer(self):
        calls = []

        def fake_generate(user, system=None, max_tokens=None, temperature=None, reasoning_effort=None, **kw):
            calls.append(reasoning_effort)
            if reasoning_effort != "none":
                return {"text": "", "finish_reason": "length", "output_tokens": 5000, "cost_usd": 0.008}
            return {"text": '{"action":"stop"}', "finish_reason": "stop", "output_tokens": 5, "cost_usd": 0.001}
        import cerebras_client
        with mock.patch.object(ag, "live_status", return_value={"available": True, "reason": ""}), \
                mock.patch.object(cerebras_client, "generate", fake_generate):
            ag._TEMP.deep, ag._TEMP.think_tokens, ag._TEMP.no_fallback = True, ag.THINK_TOKENS_REPAIR, False
            try:
                res = ag.live_generate("system", "user")
            finally:
                ag._TEMP.deep, ag._TEMP.think_tokens = False, None
        self.assertEqual(calls, ["low", "none"])
        self.assertTrue(res["thinking_fallback"])
        self.assertNotIn("thinking_exhausted", res)

    def test_the_session_counts_it_and_moves_on(self):
        def gen(system, user):
            return dict(ag._fake({"action": "stop"}), text="", thinking_exhausted=True, thinking_fallback=True,
                        reasoning_tokens=5000, cost_usd=0.008)
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"), log_path=Path(tmp) / "l.jsonl")
            with self.assertRaises(ag.BadReply):
                sess._ask("p", "rewrite", deep=True, think_tokens=ag.THINK_TOKENS_REPAIR)
            self.assertEqual(sess.model_calls, 1)                      # not asked again in the same words
            reply = next(e for e in sess.events if e["kind"] == "model_reply")
            self.assertEqual((reply["finish_reason"], reply["thinking_fallback"]), ("thinking ran out", True))
            self.assertAlmostEqual(sess.cost_usd, 0.008)               # what it cost is still counted
            self.assertEqual(sess._think["fallbacks"], 1)

    def test_the_thinking_budgets_are_unchanged(self):
        self.assertEqual((ag.MAX_DEEP_CALLS, ag.THINK_TOKENS["low"], ag.THINK_TOKENS_REPAIR), (8, 8000, 5000))


if __name__ == "__main__":
    unittest.main()
