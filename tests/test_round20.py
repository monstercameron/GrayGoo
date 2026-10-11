"""A reviewer's list: evidence a builder wrote for itself, plans that end at a failed step, advice.

- A test the builder wrote and then passed shows that the code does what its
  author thought of. One line in three of the first integration tests is now
  kept back and never shown in a prompt, and a verdict says how far it reaches:
  the user's requirements, checks the builder never saw, or its own tests only.
- A plan that is not an app used to end at the first step that failed. It is
  now recovered like an app's.
- The user's requirements can call a function, so a project that is not an app
  can be checked against them.
- Advice on how to cut an app into functions can be switched off.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import agent_session as ag  # noqa: E402
import qualify  # noqa: E402
import test_integration_proof as ti  # noqa: E402
import test_round18 as r18  # noqa: E402

SIX = [ti.CHAIN, 'run "help" prints "put"', 'run "put owl" then run "show" prints "owl"',
       'run "help" prints "show"', 'run "show" does not print "kite"', 'run "put a b" then run "show" works']
WRONG = 'run "put owl" then run "show" does not print "owl"'      # fails on a correct notebook


def build(name, body, call, expect, params="(x)"):
    return {"action": "build", "name": name, "description": name,
            "definition": '(defun %s %s "%s." %s)' % (name, params, name, body),
            "call": call, "tests": [{"call": call, "expect": expect}]}


DBL = build("dbl", "(* 2 x)", "(dbl 2)", "4")
BAD = build("quad", '(error "boom")', "(quad 2)", "8")
QUAD = build("quad", "(* 2 (dbl x))", "(quad 2)", "8")


def plan(*steps):
    return {"action": "plan", "steps": [{"name": n, "spec": spec} for n, spec in steps]}


class PlanScript:
    """Plans dbl and quad; quad is wrong until a recovery round asks again (or always)."""

    def __init__(self, recovers=True, spec="(quad x) -> four times x; calls dbl twice"):
        self.recovers, self.spec, self.asked, self.recovering = recovers, spec, [], False

    def __call__(self, system, user):
        self.asked.append(user)
        if "RECOVERY - these planned functions could not be built" in user:
            self.recovering = True
            return ag._fake(plan(("quad", self.spec)))
        if "GOAL: (quad " in user or "GOAL: quad" in user:
            return ag._fake(QUAD if self.recovering and self.recovers else BAD)
        if "GOAL: (dbl " in user:
            return ag._fake(DBL)
        if "finish the original goal" in user:
            return ag._fake({"action": "use", "call": "(quad 2)"})
        return ag._fake(plan(("dbl", "(dbl x) -> twice x"), ("quad", self.spec)))


def session(tmp, model, prompt="four times a number", **attrs):
    sess = ag.Session(prompt, model, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                      log_path=Path(tmp) / "l.jsonl")
    sess.visual = False
    for key, value in attrs.items():
        setattr(sess, key, value)
    return sess


def kinds(sess, kind):
    return [e for e in sess.events if e["kind"] == kind]


class EveryPlanRecoversTests(unittest.TestCase):
    def test_a_failed_step_of_a_plan_that_is_not_an_app_is_tried_again_with_what_was_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = PlanScript()
            sess = session(tmp, script)
            sess.run()
            self.assertEqual(sess.state, "done")
            self.assertEqual([e["name"] for e in kinds(sess, "step_failed")], ["quad"])
            self.assertEqual(kinds(sess, "recovery")[0]["failed"], ["quad"])
            told = next(p for p in script.asked if "RECOVERY - these planned functions" in p)
            self.assertIn("quad:", told)
            self.assertIn("boom", told)
            self.assertEqual([e["value"] for e in kinds(sess, "result")], ["8"])

    def test_when_recovery_cannot_build_it_either_the_build_says_which_function_and_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, PlanScript(recovers=False))
            sess.run()
            self.assertEqual(sess.state, "failed")
            gave = kinds(sess, "gave_up")[-1]
            self.assertTrue(gave["step"])
            self.assertEqual((gave["name"], gave["left"]), ("quad", ["quad"]))
            self.assertIn("boom", gave["detail"])
            self.assertIn("dbl", [t["name"] for t in sess.registry.load()])      # what worked stays saved

    def test_a_step_that_calls_a_failed_one_is_not_attempted_but_is_planned_again(self):
        steps = plan(("quad", "(quad x) -> four times x"), ("oct", "(oct x) -> twice (quad x)"))
        oct_fn = build("oct", "(* 2 (quad x))", "(oct 1)", "8")

        class Script(PlanScript):
            def __call__(self, system, user):
                self.asked.append(user)
                if "RECOVERY - these planned functions could not be built" in user:
                    self.recovering = True
                    return ag._fake(steps)
                if "GOAL: (oct " in user:
                    return ag._fake(oct_fn)
                if "GOAL: (quad " in user:
                    return ag._fake(build("quad", "(* 4 x)", "(quad 2)", "8") if self.recovering else BAD)
                if "finish the original goal" in user:
                    return ag._fake({"action": "use", "call": "(oct 1)"})
                return ag._fake(steps)
        with tempfile.TemporaryDirectory() as tmp:
            script = Script()
            sess = session(tmp, script, parallel=1)
            sess.run()
            failed = kinds(sess, "step_failed")
            self.assertEqual([e["name"] for e in failed], ["quad", "oct"])
            self.assertEqual(failed[1]["detail"], "not built: it calls quad, which could not be built")
            before = next(i for i, p in enumerate(script.asked) if "RECOVERY" in p)
            self.assertFalse(any("GOAL: (oct " in p for p in script.asked[:before]))     # no call was spent on it
            self.assertEqual(sess.state, "done")
            self.assertEqual(kinds(sess, "result")[-1]["value"], "8")

    def test_the_same_holds_when_the_steps_are_built_at_the_same_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, PlanScript(), parallel=4)
            sess.run()
            self.assertEqual(sess.state, "done")
            self.assertTrue(kinds(sess, "recovery"))


class HeldOutTests(unittest.TestCase):
    def run_app(self, lines, **attrs):
        script = ti.Script(lines=lines)
        script.fixing = True                                          # the notebook is right from the start
        kept, held = [], []
        with tempfile.TemporaryDirectory() as tmp:
            sess = ti.run(script, tmp, hold_out=True, save_integration=kept.append,
                          save_held_out=held.append, **attrs)
        return sess, script, kept, held

    def test_one_line_in_three_of_the_first_tests_is_kept_back(self):
        sess, _, kept, held = self.run_app(SIX)
        written = kinds(sess, "integration_written")[0]
        self.assertEqual(written["held_out"], 2)
        self.assertEqual(held[0].splitlines(), [SIX[2], SIX[5]])
        self.assertEqual(kept[0].splitlines(), [SIX[0], SIX[1], SIX[3], SIX[4]])
        self.assertEqual(written["lines"], kept[0].splitlines())

    def test_no_prompt_ever_shows_a_held_out_line_to_the_model(self):
        _, script, _, _ = self.run_app(SIX)
        for prompt in script.prompts:
            self.assertNotIn('put owl', prompt)
            self.assertNotIn('put a b', prompt)
        self.assertTrue(any(SIX[0] in p for p in script.prompts))     # the shown ones are shown

    def test_they_are_run_with_the_rest_and_a_pass_makes_the_verdict_reach_further(self):
        sess, _, _, _ = self.run_app(SIX)
        ran = kinds(sess, "held_out")[-1]
        self.assertEqual((ran["met"], ran["unmet"]), (2, 0))
        summary = sess._summary()
        self.assertEqual(summary["verification"]["held_out"]["met"], 2)
        self.assertEqual((sess.state, summary["qualification"]["verdict"],
                          summary["qualification"]["strength"]), ("done", "proven", "independent"))

    def test_too_few_lines_are_all_shown(self):
        sess, _, kept, held = self.run_app(SIX[:3])
        self.assertEqual((held, len(kept[0].splitlines())), ([], 3))
        self.assertEqual(sess._summary()["qualification"]["strength"], "self")

    def test_it_is_off_unless_the_owner_of_the_session_turns_it_on(self):
        script = ti.Script(lines=SIX)
        script.fixing = True
        with tempfile.TemporaryDirectory() as tmp:
            sess = ti.run(script, tmp)
        self.assertEqual((sess.held_out_text, kinds(sess, "held_out")), ("", []))
        self.assertEqual(len(sess.integration_text.splitlines()), 6)

    def test_one_that_fails_blocks_done_and_the_round_of_fixes_learns_only_what_it_types(self):
        lines = SIX[:2] + [WRONG] + SIX[3:5]
        sess, script, _, _ = self.run_app(lines)
        first = kinds(sess, "qualification")[0]
        held = next(p for p in first["proofs"] if p["id"] == "heldout")
        self.assertEqual((held["ok"], held["detail"], held["source"]), (False, "1 of 1 fail", "held-out"))
        self.assertEqual(sess.state, "failed")
        told = next(p for p in script.prompts if "THE FINISHED APP WAS TRIED" in p)
        self.assertIn("1 check that the builder is not shown fails; these are what is typed there: put, show", told)
        self.assertNotIn(WRONG, told)
        self.assertNotIn("put owl", told)

    def test_one_that_still_fails_at_the_end_becomes_a_shown_test_for_the_next_build(self):
        lines = SIX[:2] + [WRONG] + SIX[3:5]
        sess, _, kept, held = self.run_app(lines)
        self.assertEqual(kinds(sess, "heldout_revealed")[-1]["lines"], [WRONG])
        self.assertIn(WRONG, kept[-1].splitlines())
        self.assertEqual(held[-1], "")
        self.assertIn(WRONG, sess.integration_text)


class StrengthTests(unittest.TestCase):
    def proofs(self, **ok):
        return [{"id": k, "ok": v} for k, v in ok.items()]

    def test_a_verdict_says_how_far_it_reaches(self):
        self.assertEqual(qualify.strength_of(self.proofs(replay=True, integration=True), "proven"), "self")
        self.assertEqual(qualify.strength_of(self.proofs(replay=True, heldout=True), "proven"), "independent")
        self.assertEqual(qualify.strength_of(self.proofs(visitor=True), "proven"), "independent")
        self.assertEqual(qualify.strength_of(self.proofs(heldout=True, requirements=True), "proven"), "user")
        self.assertEqual(qualify.strength_of(self.proofs(heldout=None, replay=True), "proven"), "self")
        self.assertIsNone(qualify.strength_of(self.proofs(requirements=True), "disproven"))
        self.assertIsNone(qualify.strength_of(self.proofs(replay=True), "unproven"))

    def test_every_proof_says_whose_evidence_it_rests_on(self):
        tools = [{"name": "handle-command", "definition": ti.ENTRY["definition"], "tests": ti.ENTRY["tests"]}]
        out = qualify.qualify(tools, smoke_ok=True, integ={"total": 2, "met": 2, "unmet": 0, "unchecked": 0},
                              heldout={"total": 1, "met": 1, "unmet": 0, "unchecked": 0},
                              reqs={"total": 1, "met": 1, "unmet": 0, "unchecked": 0})
        sources = {p["id"]: p["source"] for p in out["proofs"]}
        self.assertEqual(sources["integration"], "agent")
        self.assertEqual(sources["heldout"], "held-out")
        self.assertEqual(sources["requirements"], "user")
        self.assertEqual(sources["answers"], "harness")
        self.assertEqual(out["strength"], "user")

    def test_a_build_proven_only_by_its_own_tests_says_so(self):
        script = ti.Script()
        script.fixing = True
        with tempfile.TemporaryDirectory() as tmp:
            sess = ti.run(script, tmp)
        self.assertEqual(sess._summary()["qualification"]["strength"], "self")


class RequirementsForAnyProjectTests(unittest.TestCase):
    def test_a_line_that_calls_a_function_is_checked_in_a_project_that_is_not_an_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, PlanScript(), requirements_text="call (quad 2) gives 8\ncall (dbl 5) gives 10\n")
            sess.run()
            self.assertEqual(sess.state, "done")
            summary = sess._summary()
            self.assertEqual(summary["verification"]["requirements"]["met"], 2)
            self.assertEqual((summary["qualification"]["verdict"], summary["qualification"]["strength"]),
                             ("proven", "user"))
            proof = kinds(sess, "qualification")[-1]["proofs"][0]
            self.assertEqual((proof["id"], proof["ok"], proof["source"]), ("requirements", True, "user"))

    def test_an_unmet_one_is_worked_on_and_keeps_the_build_from_done_if_it_stays_unmet(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = PlanScript()
            sess = session(tmp, script, requirements_text="call (quad 2) gives 9\n")
            sess.run()
            self.assertEqual(sess.state, "failed")
            self.assertEqual(sess._summary()["qualification"]["failed"], ["requirements"])
            gave = kinds(sess, "gave_up")[-1]
            self.assertIn("the user's requirement 'call (quad 2) gives 9' is not met", gave["detail"])
            self.assertTrue(any("FOUND THIS MISSING: the user's requirement 'call (quad 2) gives 9'" in p
                                for p in script.asked))

    def test_only_saved_functions_can_be_called_from_a_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, PlanScript(), requirements_text="call (delete-file \"x\") works\n")
            sess.run()
            row = sess._reqs["results"][0]
            self.assertIs(row["ok"], False)
            self.assertIn("not a call of a saved function", row["detail"])

    def test_a_call_line_also_works_for_an_app(self):
        script = ti.Script()
        script.fixing = True
        with tempfile.TemporaryDirectory() as tmp:
            sess = ti.run(script, tmp, requirements_text=(
                "call (cmd-show '() '((\"notes\" ((\"kite\")))) 0) shows \"kite\"\n"))
        self.assertEqual(sess._summary()["verification"]["requirements"]["met"], 1)
        self.assertEqual(sess._summary()["qualification"]["strength"], "user")


class AdviceSwitchTests(unittest.TestCase):
    def started(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.object(ag, "AGENT_DIR", tmp):
                agent = ag.SessionManager(registry=ag.ToolRegistry(tmp / "tools.json"))
                agent.generators["demo"] = lambda s, u: ag._fake({"action": "use", "call": "(+ 1 2)"})
                sid, err = agent.start("add one and two", "demo", **kwargs)
                self.assertIsNone(err)
                sess = agent._sessions[sid]
                for _ in range(200):
                    if sess.state != "running":
                        break
                    time.sleep(0.05)
                return sess

    def test_advice_is_on_by_default_and_can_be_turned_off_for_one_build(self):
        self.assertTrue(self.started().use_advice)
        self.assertFalse(self.started(advice=False).use_advice)

    def test_it_can_be_turned_off_for_the_whole_process(self):
        with mock.patch.dict(os.environ, {"GRAYGOO_ADVICE": "0"}):
            self.assertFalse(self.started().use_advice)

    def test_the_reminder_of_a_command_line_app_prescribes_no_structure_without_advice(self):
        self.assertNotIn("cmd-", ag.CLI_REMINDER)
        self.assertIn("cmd-<word>", ag.CLI_REMINDER_ADVICE)
        script = ti.Script()
        script.fixing = True
        with tempfile.TemporaryDirectory() as tmp:
            ti.run(script, tmp, use_advice=False)
        steps = [p for p in script.prompts if "GOAL: (cmd-" in p]
        self.assertTrue(steps)
        for prompt in steps:
            self.assertIn("COMMAND-LINE APP: the harness calls (handle-command args state now)", prompt)
            self.assertNotIn("cmd-<word>", prompt)


if __name__ == "__main__":
    unittest.main()
