"""A build ends as done only when the qualifier's proofs hold (real SBCL, scripted model).

Live build 6e1624d0c4 ended "done" while its main command printed "Invalid input":
every function had passed its own tests, and nothing had tried the app as a whole.
"""
import sys
import tempfile
import unittest
from pathlib import Path

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
EXTRA = {"action": "build", "name": "cmd-extra", "description": "Prints a fixed line",
         "definition": '(defun cmd-extra (args state now) "Prints a fixed line." '
                       '(declare (ignore args state now)) (list :output "extra"))',
         "call": "(cmd-extra '() '() 0)",
         "tests": [{"call": "(cmd-extra '() '() 0)", "expect": '(:output "extra")'}]}


def entry(passes):
    """handle-command that hands cmd-double PASSES: ``args`` (wrong) or ``(rest args)`` (right)."""
    return {"action": "build", "name": "handle-command", "description": "Runs one command",
            "definition": '(defun handle-command (args state now) "Runs one command." '
                          '(cond ((equal (first args) "help") (list :output "commands: double, help")) '
                          '((equal (first args) "double") (if (rest args) (cmd-double %s state now) '
                          '(list :output "usage: double N"))) '
                          '(t (list :output "unknown command, try help"))))' % passes,
            "call": "(handle-command '(\"help\") '() 0)",
            "tests": [{"call": "(handle-command '(\"help\") '() 0)",
                       "expect": '(:output "commands: double, help")'}]}


class Script:
    """Builds the app with the dispatcher bug; fixes it when shown the failed proof (or never)."""

    def __init__(self, fixes=True, steps=("cmd-double", "handle-command")):
        self.fixes, self.steps, self.prompts, self.fixing = fixes, steps, [], False

    def plan(self, names):
        return ag._fake({"action": "plan", "steps": [
            {"name": n, "spec": "(%s args state now) -> plist" % n} for n in names]})

    def __call__(self, system, user):
        self.prompts.append(user)
        if "THE FINISHED APP WAS TRIED AND THESE CHECKS FAILED" in user:
            self.fixing = True
            return self.plan(["handle-command"])
        if "GOAL: (handle-command " in user:
            return ag._fake(entry("(rest args)" if self.fixing and self.fixes else "args"))
        if "GOAL: (cmd-double " in user:
            return ag._fake(DOUBLE)
        if "GOAL: (cmd-extra " in user:
            return ag._fake(EXTRA)
        return self.plan(self.steps)


def run(script, tmp, **attrs):
    sess = ag.Session(PROMPT, script, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                      log_path=Path(tmp) / "l.jsonl")
    sess.visual = False
    for key, value in attrs.items():
        setattr(sess, key, value)
    sess.run()
    return sess


class DonenessTests(unittest.TestCase):
    def test_a_build_whose_command_fails_when_typed_is_fixed_before_it_is_called_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Script()
            sess = run(script, tmp)
            verdicts = [e["verdict"] for e in sess.events if e["kind"] == "qualification"]
            self.assertEqual(verdicts[0], "disproven")              # every unit test had passed
            self.assertEqual(verdicts[-1], "proven")
            first = next(e for e in sess.events if e["kind"] == "qualification")
            replay = next(p for p in first["proofs"] if p["id"] == "replay")
            self.assertIs(replay["ok"], False)
            self.assertIn("typing 'double 4'", replay["detail"])
            self.assertIn("passes the command word on to cmd-double", replay["detail"])
            fix = next(p for p in script.prompts if "THE FINISHED APP WAS TRIED" in p)
            self.assertIn("typing 'double 4'", fix)                  # the planner is told exactly this
            self.assertEqual(sess.state, "done")
            summary = sess._summary()
            self.assertEqual(summary["qualification"]["verdict"], "proven")
            self.assertEqual(summary["verification"]["qualification"]["failed"], [])
            typed = {row["typed"]: row["printed"] for row in summary["qualification"]["transcript"]}
            self.assertEqual(typed["double 4"], "8")

    def test_a_build_that_stays_broken_does_not_end_as_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = run(Script(fixes=False), tmp)
            self.assertEqual(sess.state, "failed")
            gave = [e for e in sess.events if e["kind"] == "gave_up"][-1]
            self.assertTrue(gave["detail"].startswith("Built, but not proven done."))
            self.assertIn("typing 'double 4'", gave["detail"])
            summary = sess._summary()
            self.assertEqual((summary["outcome"], summary["qualification"]["verdict"]), ("failed", "disproven"))
            self.assertIn("replay", summary["qualification"]["failed"])
            # it kept trying while it had budget, and stopped once two rounds left the same failures
            rounds = [e for e in sess.events if e["kind"] == "behaviour_fix"]
            self.assertEqual([(e["round"], e["stuck"]) for e in rounds], [(1, False), (2, True)])
            self.assertEqual(sorted(set(n for n, _ in sess._built)), ["cmd-double", "handle-command"])

    def test_a_round_that_changed_nothing_is_told_so_before_the_next_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Script(fixes=False)
            run(script, tmp)
            asks = [p for p in script.prompts if "THE FINISHED APP WAS TRIED" in p]
            self.assertEqual(len(asks), 2)
            self.assertNotIn("THE LAST ROUND OF FIXES LEFT EXACTLY THESE FAILURES", asks[0])
            self.assertIn("THE LAST ROUND OF FIXES LEFT EXACTLY THESE FAILURES IN PLACE", asks[1])

    def test_a_planned_function_that_nothing_calls_keeps_the_build_from_being_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Script(steps=("cmd-extra", "cmd-double", "handle-command"))
            script.fixing = True                                      # the dispatcher is right from the start
            sess = run(script, tmp)
            last = [e for e in sess.events if e["kind"] == "qualification"][-1]
            wired = next(p for p in last["proofs"] if p["id"] == "wired")
            self.assertIs(wired["ok"], False)
            self.assertIn("cmd-extra", wired["detail"])
            self.assertEqual(sess.state, "failed")
            self.assertIn("cmd-extra", [e for e in sess.events if e["kind"] == "gave_up"][-1]["detail"])

    def test_the_next_prompt_is_told_what_the_saved_app_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(Script(fixes=False), tmp)                             # leaves the broken app saved
            script = Script()
            sess = run(script, tmp, prior_goals=[PROMPT])
            kinds = [e["kind"] for e in sess.events]
            self.assertLess(kinds.index("precheck"), kinds.index("model_call"))
            self.assertIn("PROOF FACTS - the saved app was tried just now and fails these", script.prompts[0])
            self.assertIn("typing 'double 4'", script.prompts[0])

    def test_a_working_app_is_proven_without_any_fix_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Script()
            script.fixing = True
            sess = run(script, tmp)
            self.assertEqual(sess.state, "done")
            self.assertEqual([e["verdict"] for e in sess.events if e["kind"] == "qualification"][-1], "proven")
            self.assertNotIn("behaviour_fix", [e["kind"] for e in sess.events])
            self.assertIn("Proven by:", sess._summary()["qualification"]["basis"])

    def test_a_single_function_build_has_no_app_to_prove_and_is_done_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            reply = {"action": "build", "name": "square", "description": "Squares a number",
                     "definition": '(defun square (x) "Squares a number." (* x x))', "call": "(square 12)",
                     "tests": [{"call": "(square 12)", "expect": "144"}]}
            sess = ag.Session("square 12", lambda s, u: ag._fake(reply),
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"), log_path=Path(tmp) / "l.jsonl")
            sess.run()
            self.assertEqual(sess.state, "done")
            self.assertIsNone(sess._summary()["qualification"])


if __name__ == "__main__":
    unittest.main()
