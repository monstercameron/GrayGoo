"""From five live builds of "review your answers and architecture and try again" (real SBCL, scripted model).

The goal review of round 18 found what was missing and had it built, but no
build could end: each round met the point it was given and was handed a
stricter one. A value that had passed in one round could not be changed on
purpose in the next. And the fifth identical prompt was answered from the
prompt cache as a success, with the call of the fourth build, which had failed.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import agent_session as ag  # noqa: E402
import test_round18 as r18  # noqa: E402

TABLE = "a table of battery types cannot hold every battery there is"


class ContractTests(unittest.TestCase):
    def build(self, reviews):
        script = r18.Script(reviews)
        with tempfile.TemporaryDirectory() as tmp:
            sess = r18.session(tmp, script, goal_check=True)
            sess.run()
        return sess, script

    def test_what_a_build_owes_is_what_its_first_review_found(self):
        sess, script = self.build([[r18.MISSING], [TABLE], [TABLE]])
        second = script.asked[1]
        self.assertIn("AN EARLIER REVIEW OF THIS BUILD FOUND THE POINTS BELOW MISSING", second)
        self.assertIn("Check ONLY these points", second)
        self.assertIn("- " + r18.MISSING, second)
        self.assertNotIn("AN EARLIER REVIEW OF THIS BUILD", script.asked[0])
        third = script.asked[2]
        self.assertIn("- " + r18.MISSING, third)                    # still the first list, not the newer demand
        self.assertNotIn("- " + TABLE, third)

    def test_a_later_review_cannot_raise_more_points_than_the_first_one_found(self):
        sess, _ = self.build([[r18.MISSING], [TABLE, "it has no colours", "it is not fast"]])
        reviews = [e for e in sess.events if e["kind"] == "goal_review"]
        self.assertEqual(len(reviews[1]["missing"]), 1)

    def test_the_reviewers_own_verdict_outranks_its_remarks(self):
        calls = []

        def model(system, user):
            if system == ag.GOAL_REVIEW_SYSTEM:
                calls.append(user)
                return ag._fake({"action": "review", "met": True,
                                 "missing": ["battery-calc now handles the flashlight example"], "why": "fine"})
            return script(system, user)
        script = r18.Script([])
        with tempfile.TemporaryDirectory() as tmp:
            sess = r18.session(tmp, model, goal_check=True)
            sess.run()
        self.assertEqual((sess.state, len(calls)), ("done", 1))
        self.assertEqual(sess._summary()["verification"]["goal"], {"met": True, "missing": [], "rounds": 0})

    def test_the_reviewer_is_told_that_any_is_not_every(self):
        for said in ("not every input that exists", "do not call a table missing or hardcoded",
                     "What works is not listed", "\"met\" is true when"):
            self.assertIn(said, ag.GOAL_REVIEW_SYSTEM)


class CarriedOverTests(unittest.TestCase):
    def test_the_next_build_is_told_what_the_last_one_left_missing(self):
        prompts = []

        def model(system, user):
            prompts.append(user)
            return ag._fake({"action": "use", "call": "(+ 1 2)"})
        with tempfile.TemporaryDirectory() as tmp:
            sess = r18.session(tmp, model, prompt="try again", prior_missing=[r18.MISSING])
            sess.run()
        self.assertIn("THE LAST BUILD OF THIS PROJECT DID NOT END AS DONE. A review of it against the user's "
                      "words found this still missing, and this build has to deliver it: " + r18.MISSING,
                      prompts[0])

    def test_the_manager_reads_how_the_last_build_of_the_project_ended(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.object(ag, "AGENT_DIR", tmp):
                agent = ag.SessionManager(registry=ag.ToolRegistry(tmp / "tools.json"))
                proj, _ = agent.projects.create("Battery")
                (tmp / "sessions").mkdir(exist_ok=True)
                self.assertIsNone(agent._last_summary(proj["id"], "live"))
                rows = [{"kind": "goal", "t": 9e9, "prompt": "try again", "mode": "live", "arm": "main",
                         "project": proj["id"]},
                        {"kind": "summary", "outcome": "failed",
                         "verification": {"goal": {"met": False, "missing": [r18.MISSING], "rounds": 2}}}]
                (tmp / "sessions" / "aaaa000001.jsonl").write_text(
                    "\n".join(json.dumps(r) for r in rows), encoding="utf-8")
                last = agent._last_summary(proj["id"], "live")
                self.assertEqual(last["outcome"], "failed")
                self.assertEqual(last["verification"]["goal"]["missing"], [r18.MISSING])
                self.assertIsNone(agent._last_summary(proj["id"], "demo"))


class CacheTests(unittest.TestCase):
    PROMPT = "review your answers and architecture and try again"

    def cached_session(self, tmp, **attrs):
        calls = []

        def model(system, user):
            calls.append(user)
            return ag._fake({"action": "use", "call": r18.calc(False)["call"]})
        sess = r18.session(tmp, model, prompt=self.PROMPT,
                           tools=(r18.CAPACITY, r18.current(False), r18.calc(False)), **attrs)
        sess.registry.note_use("battery-calc", self.PROMPT, r18.calc(False)["call"])
        return sess, calls

    def test_a_prompt_is_not_answered_from_the_cache_after_a_build_that_did_not_end_as_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess, calls = self.cached_session(tmp, prior_unfinished=True)
            sess.run()
            self.assertFalse(any(e["kind"] == "decision" and e.get("action") == "cache" for e in sess.events))
            self.assertEqual(len(calls), 1)

    def test_after_a_build_that_ended_as_done_the_cache_answers_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess, calls = self.cached_session(tmp)
            sess.run()
            self.assertTrue(any(e["kind"] == "decision" and e.get("action") == "cache" for e in sess.events))
            self.assertEqual(calls, [])

    def test_a_build_that_does_not_end_as_done_is_not_remembered_as_the_answer_to_its_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = r18.Script([[r18.MISSING]] * 5)
            sess = r18.session(tmp, script, goal_check=True)
            sess.run()
            self.assertEqual(sess.state, "failed")
            self.assertIsNone(sess.registry.find_cached(r18.GOAL))
            for tool in sess.registry.load():
                self.assertEqual(tool.get("prompts") or [], [], tool["name"])

    def test_a_build_that_ends_as_done_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = r18.session(tmp, r18.Script([[]]), goal_check=True)
            sess.run()
            self.assertEqual(sess.state, "done")
            self.assertIsNotNone(sess.registry.find_cached(r18.GOAL))

    def test_forgetting_a_prompt_leaves_the_other_prompts_of_a_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            reg.add({k: v for k, v in r18.calc(False).items() if k != "action"})
            reg.note_use("battery-calc", "how long does it last", r18.calc(False)["call"])
            reg.note_use("battery-calc", "try again", r18.calc(False)["call"])
            self.assertTrue(reg.forget_prompt("try again"))
            self.assertFalse(reg.forget_prompt("try again"))
            self.assertIsNone(reg.find_cached("try again"))
            self.assertIsNotNone(reg.find_cached("how long does it last"))


class FrozenTests(unittest.TestCase):
    TABLE = {"action": "build", "name": "battery-capacity", "description": "Nominal capacity in mAh",
             "definition": '(defun battery-capacity (type) "Nominal capacity in mAh." '
                           '(case type (:aa 2400) (:li-ion 2000) (t nil)))',
             "call": "(battery-capacity :li-ion)",
             "tests": [{"call": "(battery-capacity :li-ion)", "expect": "2000"},
                       {"call": "(battery-capacity :aa)", "expect": "2400"}]}

    def test_a_plan_that_changes_a_function_may_change_a_value_that_passed_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = r18.session(tmp, lambda s, u: ag._fake(self.TABLE))
            sess._frozen = {"(battery-capacity :li-ion)": "NIL", "(battery-voltage :aa)": "1.5"}
            built = sess._run_steps({"action": "plan", "steps": [
                {"name": "battery-capacity", "spec": "(battery-capacity type) -> mAh, 2000 for :li-ion"}]})
            self.assertTrue(built)
            self.assertIn("battery-capacity", [t["name"] for t in sess.registry.load()])
            self.assertEqual(sess._frozen.get("(battery-voltage :aa)"), "1.5")      # not planned: still owed

    def test_a_repair_still_cannot_bend_a_value_that_passed(self):
        self.assertIn("SPEC_INCONSISTENT", ag.validate_build(
            {k: v for k, v in self.TABLE.items()}, {"(battery-capacity :li-ion)": "NIL"}) or "")


class OwnExampleTests(unittest.TestCase):
    def test_the_call_that_answers_is_handed_the_users_earlier_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = r18.session(tmp, lambda s, u: None, prompt="try again", prior_goals=[r18.GOAL, r18.QUESTION])
            said = sess._own_example()
            self.assertIn("use the latest one in the user's earlier words, with their numbers", said)
            self.assertIn("200mw for 2 AA batteries", said)
            self.assertEqual(r18.session(tmp, lambda s, u: None)._own_example(), "")


if __name__ == "__main__":
    unittest.main()
