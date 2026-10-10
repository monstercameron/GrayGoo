"""A build tries its failed functions again by itself instead of waiting for Continue.

Live builds 44bdfb6d97 and 950fccf453 each ended with planned functions unbuilt and
had to be continued by hand, which starts over with no memory of what went wrong.
These tests run real SBCL with a scripted model.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402

PROMPT = "a command line rate tool: the command rate 6.5 prints the monthly rate"
PLAN = {"action": "plan", "steps": [
    {"name": "parse-rate", "spec": "(parse-rate \"6.5\") => 6.5 ; reads a decimal number from a string"},
    {"name": "handle-command", "spec": "(handle-command args state now) -> plist ; help prints the commands"}]}
WRONG = ('(defun parse-rate (s) "Reads a number from a string." '
         '(parse-integer s :junk-allowed t))')
RIGHT = ('(defun parse-rate (s) "Reads a decimal number from a string." '
         '(let ((dot (position #\\. s))) (if dot (+ (parse-integer s :end dot) '
         '(/ (parse-integer s :start (1+ dot)) (expt 10.0 (- (length s) dot 1)))) '
         '(parse-integer s))))')
TESTS = [{"call": '(parse-rate "6.5")', "expect": "6.5"}, {"call": '(parse-rate "7")', "expect": "7"}]
COMMAND = {"action": "build", "name": "handle-command", "description": "Runs one command",
           "definition": '(defun handle-command (args state now) "Runs one command." '
                         '(declare (ignore state now)) (list :output (cond '
                         '((equal (first args) "help") "commands: rate, help") '
                         '((equal (first args) "rate") (if (second args) '
                         '(format nil "monthly ~a" (/ (parse-rate (second args)) 12)) "usage: rate NUMBER")) '
                         '(t "unknown command, try help"))))',
           "call": "(getf (handle-command '(\"help\") '() 0) :output)",
           "tests": [{"call": "(getf (handle-command '(\"help\") '() 0) :output)",
                      "expect": '"commands: rate, help"'}]}


class Script:
    """Answers by what the prompt asks for; parse-rate is wrong until the recovery plan."""

    def __init__(self, recovers=True):
        self.recovers, self.phase, self.prompts = recovers, 1, []

    def __call__(self, system, user):
        self.prompts.append(user)
        if "RECOVERY - these planned functions" in user:
            self.phase = 2
            return ag._fake({"action": "plan", "steps": [PLAN["steps"][0]]})
        if "GOAL: (handle-command " in user:
            return ag._fake(COMMAND)
        if "GOAL: (parse-rate " in user:
            good = self.phase == 2 and self.recovers
            return ag._fake({"action": "build", "name": "parse-rate", "description": "Reads a number",
                             "definition": RIGHT if good else WRONG, "tests": TESTS,
                             "call": '(parse-rate "6.5")'})
        return ag._fake(PLAN)


def run(script, **limits):
    tmp = tempfile.TemporaryDirectory()
    sess = ag.Session(PROMPT, script, registry=ag.ToolRegistry(Path(tmp.name) / "t.json"),
                      log_path=Path(tmp.name) / "l.jsonl")
    sess.visual = False
    for key, value in limits.items():
        setattr(sess, key, value)
    sess.run()
    return sess, tmp


class RecoveryTests(unittest.TestCase):
    def test_a_failed_function_is_built_in_a_recovery_round_without_continue(self):
        script = Script()
        sess, tmp = run(script)
        self.addCleanup(tmp.cleanup)
        kinds = [e["kind"] for e in sess.events]
        self.assertIn("step_failed", kinds)                        # it did fail first
        start = next(e for e in sess.events if e["kind"] == "recovery")
        self.assertEqual((start["round"], start["failed"]), (1, ["parse-rate"]))
        done = next(e for e in sess.events if e["kind"] == "recovery_done")
        self.assertEqual((done["rebuilt"], done["still_failed"]), (["parse-rate"], []))
        self.assertEqual(sess.state, "done")
        saved = {t["name"]: t["definition"] for t in sess.registry.load()}
        self.assertIn("(position #\\. s)", saved["parse-rate"])
        self.assertIn("handle-command", saved)
        self.assertFalse([e for e in sess.events if e["kind"] == "gave_up"
                          and "could not be built" in (e.get("detail") or "")])
        self.assertEqual(sess._summary()["missing_features"], [])

    def test_the_planner_is_shown_what_went_wrong(self):
        script = Script()
        sess, tmp = run(script)
        self.addCleanup(tmp.cleanup)
        asked = next(p for p in script.prompts if "RECOVERY - these planned functions" in p)
        self.assertIn('parse-rate: (parse-rate "6.5"): got 6, expected 6.5', asked)
        for phrase in ("worked out by hand", "FIXES that saved function under its SAME name",
                       "simpler approach or in smaller parts"):
            self.assertIn(phrase, asked)

    def test_a_round_that_builds_nothing_ends_the_recovery(self):
        script = Script(recovers=False)
        sess, tmp = run(script)
        self.addCleanup(tmp.cleanup)
        rounds = [e for e in sess.events if e["kind"] == "recovery"]
        self.assertEqual(len(rounds), 1)                           # no second round after no progress
        done = next(e for e in sess.events if e["kind"] == "recovery_done")
        self.assertEqual((done["rebuilt"], done["still_failed"]), ([], ["parse-rate"]))
        self.assertEqual(sess.state, "failed")
        self.assertTrue([e for e in sess.events if e["kind"] == "gave_up"
                         and "could not be built: parse-rate" in (e.get("detail") or "")])
        self.assertIn("handle-command", {t["name"] for t in sess.registry.load()})   # what worked is kept

    def test_no_recovery_is_started_without_budget_left(self):
        for limits in ({"max_usd": 0.0}, {"max_seconds": 0}):
            script = Script()
            sess = ag.Session(PROMPT, script)
            for key, value in limits.items():
                setattr(sess, key, value)
            self.assertFalse(sess._budget_left(), limits)
        sess = ag.Session(PROMPT, Script())
        sess.model_calls, sess.max_calls = 78, 80
        self.assertFalse(sess._budget_left())
        sess.model_calls = 10
        self.assertTrue(sess._budget_left())

    def test_nothing_failed_means_no_recovery_call(self):
        class AllGood(Script):
            def __init__(self):
                super().__init__()
                self.phase = 2
        script = AllGood()
        sess, tmp = run(script)
        self.addCleanup(tmp.cleanup)
        self.assertNotIn("recovery", [e["kind"] for e in sess.events])
        self.assertEqual(sess.state, "done")


if __name__ == "__main__":
    unittest.main()
