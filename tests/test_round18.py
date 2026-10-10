"""Two live builds of a project that is not an app, replayed (real SBCL, scripted model).

abba36314e was asked for a battery life calculator where "the user can give you
any battery specs and you try to fill in the variables", built one that needs
every value spelled out, and ended as done in 3.4 seconds: for a build that is
neither a web nor a command-line app nothing read the goal again.

8485f34590 was asked how long a flashlight runs. The model changed the
calculator, a test failed inside a helper, the repair fixed the helper, and the
build saved the helper, dropped the calculator's change and answered "200/3".
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402

GOAL = ("write a simple battery life calculator, the only challenge is teh user can give you any "
        "battery specs and you try to fill in the variables")
QUESTION = "so a flashlight, drawing 200mw for 2 AA batteries, can the calc answer this?"

CAPACITY = {"action": "build", "name": "normalize-capacity", "description": "Converts capacity to mAh",
            "definition": '(defun normalize-capacity (value unit &optional voltage) "Converts capacity to mAh." '
                          '(case unit (:mah value) (:ah (* value 1000)) (:wh (/ (* value 1000) voltage))))',
            "call": "(normalize-capacity 1000 :mah)",
            "tests": [{"call": "(normalize-capacity 1000 :mah)", "expect": "1000"},
                      {"call": "(normalize-capacity 1.5 :ah)", "expect": "1500"}]}


def current(milliwatts):
    extra = "((eq unit :mw) (/ value voltage)) " if milliwatts else ""
    tests = [{"call": "(normalize-current 200 :ma)", "expect": "200"}]
    if milliwatts:
        tests.append({"call": "(normalize-current 300 :mw 3)", "expect": "100"})
    return {"action": "build", "name": "normalize-current", "description": "Converts current or power to mA",
            "definition": '(defun normalize-current (value unit &optional voltage) "Converts to mA." '
                          '(cond ((eq unit :ma) value) ((eq unit :a) (* value 1000)) %s'
                          '(t (error "Unknown unit: ~a" unit))))' % extra,
            "call": "(normalize-current 300 :mw 3)" if milliwatts else "(normalize-current 200 :ma)",
            "tests": tests}


def calc(milliwatts):
    tests = [{"call": "(battery-calc '(:capacity 1000 :capacity-unit :mah :current 200 :current-unit :ma))",
              "expect": '"Battery life: 5.00 hours"'}]
    call = tests[0]["call"]
    if milliwatts:
        call = "(battery-calc '(:capacity 2000 :capacity-unit :mah :current 200 :current-unit :mw :voltage 3))"
        tests.append({"call": call, "expect": '"Battery life: 30.00 hours"'})
    return {"action": "build", "name": "battery-calc", "description": "Battery life in hours from a plist of specs",
            "definition": '(defun battery-calc (specs) "Battery life in hours." (let* ((v (getf specs :voltage)) '
                          '(cap (normalize-capacity (getf specs :capacity) (getf specs :capacity-unit) v)) '
                          '(cur (normalize-current (getf specs :current) (getf specs :current-unit) v))) '
                          '(format nil "Battery life: ~,2f hours" (/ cap cur))))',
            "call": call, "tests": tests}


GUESS = {"action": "build", "name": "battery-life", "description": "Battery life from whatever specs are given",
         "definition": '(defun battery-life (specs) "Fills in what is not given, then calculates." '
                       '(battery-calc (list :capacity (or (getf specs :capacity) 2000) :capacity-unit :mah '
                       ':current (or (getf specs :current) 100) :current-unit (or (getf specs :current-unit) :ma) '
                       ':voltage (or (getf specs :voltage) 3))))',
         "call": "(battery-life '(:current 200))",
         "tests": [{"call": "(battery-life '(:current 200))", "expect": '"Battery life: 10.00 hours"'},
                   {"call": "(battery-life '())", "expect": '"Battery life: 20.00 hours"'}]}
MISSING = "given only '2 AA batteries and a 200 mW load', it should fill in the capacity and voltage itself"


def plan(*names):
    return {"action": "plan", "steps": [{"name": n, "spec": "(%s ...) -> value" % n} for n in names]}


def session(tmp, model, prompt=GOAL, tools=(), **attrs):
    sess = ag.Session(prompt, model, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                      log_path=Path(tmp) / "l.jsonl")
    for tool in tools:
        sess.registry.add({k: v for k, v in tool.items() if k != "action"})
    sess.visual = False
    for key, value in attrs.items():
        setattr(sess, key, value)
    return sess


class DetourTests(unittest.TestCase):
    def run_question(self, repair_reply):
        asked = []

        def model(system, user):
            asked.append(user)
            if "PREVIOUS ATTEMPT FAILED" in user:
                return ag._fake(repair_reply)
            return ag._fake(calc(True))                       # the planner changes the calculator
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, model, prompt=QUESTION, tools=(CAPACITY, current(False), calc(False)),
                           prior_goals=[GOAL])
            sess.run()
            saved = {t["name"]: t for t in sess.registry.load()}
            return sess, saved, asked

    def test_a_repair_that_fixes_a_helper_is_saved_and_then_the_function_gets_its_turn_again(self):
        sess, saved, asked = self.run_question(current(True))
        self.assertEqual(sess.state, "done")
        self.assertIn(":mw", saved["normalize-current"]["definition"])
        self.assertEqual(len(saved["battery-calc"]["tests"]), 2)             # its new test was not dropped
        detour = next(e for e in sess.events if e["kind"] == "detour")
        self.assertEqual((detour["fixed"], detour["back_to"]), ("normalize-current", "battery-calc"))
        self.assertEqual(len(asked), 2)                                       # going back cost no model call

    def test_the_user_is_answered_with_the_function_they_asked_about_not_with_the_helper(self):
        sess, _, _ = self.run_question(current(True))
        results = [e for e in sess.events if e["kind"] == "result"]
        self.assertEqual(len(results), 1)
        self.assertIn("(battery-calc ", results[0]["call"])
        self.assertEqual(results[0]["value"], '"Battery life: 30.00 hours"')
        self.assertEqual(sess._summary()["answer"]["tool"], "battery-calc")

    def test_a_reply_that_merely_renames_the_function_is_not_a_detour(self):
        renamed = dict(calc(False), name="battery-hours",
                       definition=calc(False)["definition"].replace("battery-calc", "battery-hours"),
                       call="(battery-hours '(:capacity 1000 :capacity-unit :mah :current 200 :current-unit :ma))",
                       tests=[{"call": "(battery-hours '(:capacity 1000 :capacity-unit :mah :current 200 "
                                       ":current-unit :ma))", "expect": '"Battery life: 5.00 hours"'}])
        sess, saved, _ = self.run_question(renamed)
        self.assertEqual([e for e in sess.events if e["kind"] == "detour"], [])
        self.assertIn("battery-hours", saved)


class Script:
    """Builds the three functions; when a review finds the goal unmet, adds battery-life (or never does)."""

    def __init__(self, reviews):
        self.reviews, self.asked, self.labels = list(reviews), [], []

    def __call__(self, system, user):
        if system == ag.GOAL_REVIEW_SYSTEM:
            self.asked.append(user)
            return ag._fake({"action": "review", "missing": self.reviews.pop(0) if self.reviews else [],
                             "why": "read against the goal"})
        if "A REVIEW OF WHAT WAS BUILT AGAINST THE USER'S WORDS FOUND THIS MISSING" in user and \
                "Plan ONLY the functions" in user:
            self.labels.append("goal-fix")
            return ag._fake(plan("battery-life"))
        for reply in (CAPACITY, current(True), calc(True), GUESS):
            if "GOAL: (%s " % reply["name"] in user:
                self.labels.append(reply["name"])
                return ag._fake(reply)
        if "answer the user's goal" in user:
            return ag._fake({"action": "use", "call": "(battery-life '(:current 200))"})
        if "finish the original goal" in user or "All planned helper tools" in user:
            return ag._fake({"action": "use", "call": calc(False)["call"]})
        return ag._fake(plan("normalize-capacity", "normalize-current", "battery-calc"))


class GoalReviewTests(unittest.TestCase):
    def build(self, reviews, **attrs):
        script = Script(reviews)
        attrs.setdefault("goal_check", True)
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, script, **attrs)
            sess.run()
            names = [t["name"] for t in sess.registry.load()]
        return sess, script, names

    def test_the_reviewer_is_shown_the_goal_the_functions_their_tests_and_the_answer(self):
        _, script, _ = self.build([[]])
        shown = script.asked[0]
        self.assertIn("WHAT THE USER ASKED (latest last):\n- " + GOAL, shown)
        self.assertIn("TOOL battery-calc (specs): Battery life in hours from a plist of specs", shown)
        self.assertIn("   tested: (battery-calc '(:capacity 1000 :capacity-unit :mah :current 200 "
                      ":current-unit :ma)) => \"Battery life: 5.00 hours\"", shown)
        self.assertIn('THE ANSWER SHOWN TO THE USER: (battery-calc ', shown)
        self.assertIn('=> "Battery life: 5.00 hours"', shown)

    def test_a_build_the_review_finds_nothing_missing_in_is_done_and_says_why(self):
        sess, script, _ = self.build([[]])
        self.assertEqual((sess.state, len(script.asked)), ("done", 1))
        summary = sess._summary()
        self.assertEqual(summary["verification"]["goal"], {"met": True, "missing": [], "rounds": 0})
        self.assertEqual(summary["qualification"]["verdict"], "proven")
        proof = [e for e in sess.events if e["kind"] == "qualification"][-1]["proofs"][0]
        self.assertEqual((proof["id"], proof["ok"]), ("goal", True))

    def test_what_the_review_finds_missing_is_built_without_the_user_asking_again(self):
        sess, script, names = self.build([[MISSING], []])
        self.assertEqual(sess.state, "done")
        self.assertIn("battery-life", names)
        self.assertEqual(script.labels[-2:], ["goal-fix", "battery-life"])
        reviews = [e for e in sess.events if e["kind"] == "goal_review"]
        self.assertEqual([(e["met"], e["fixing"]) for e in reviews], [(False, True), (True, False)])
        self.assertEqual(sess._summary()["verification"]["goal"], {"met": True, "missing": [], "rounds": 1})
        # the answer shown last is the new function on the user's side of things
        self.assertIn("(battery-life ", [e for e in sess.events if e["kind"] == "result"][-1]["call"])
        self.assertIn('=> "Battery life: 10.00 hours"', script.asked[1])

    def test_the_function_being_added_is_told_what_the_review_found(self):
        import unittest.mock as mock
        seen = []
        real = ag.Session._step_prompt

        def spy(self, step):
            text = real(self, step)
            seen.append((step.get("name"), text))
            return text
        with mock.patch.object(ag.Session, "_step_prompt", spy):
            self.build([[MISSING], []])
        told = dict(seen)["battery-life"]
        self.assertIn("A REVIEW OF WHAT WAS BUILT AGAINST THE USER'S WORDS FOUND THIS MISSING: " + MISSING, told)
        self.assertNotIn("FOUND THIS MISSING", dict(seen)["battery-calc"])

    def test_a_build_that_still_lacks_it_after_the_rounds_does_not_end_as_done(self):
        sess, script, _ = self.build([[MISSING]] * 5)
        self.assertEqual(sess.state, "failed")
        self.assertEqual(len(script.asked), ag.MAX_GOAL_ROUNDS + 1)
        gave = [e for e in sess.events if e["kind"] == "gave_up"][-1]
        self.assertEqual(gave["detail"], "Built, but it does not yet do what you asked: " + MISSING)
        summary = sess._summary()
        self.assertEqual(summary["qualification"]["verdict"], "disproven")
        self.assertEqual(summary["verification"]["goal"]["rounds"], ag.MAX_GOAL_ROUNDS)

    def test_a_review_that_cannot_be_read_concludes_nothing(self):
        script = Script([])
        script.reviews = None

        def model(system, user):
            if system == ag.GOAL_REVIEW_SYSTEM:
                return ag._fake({"action": "review", "missing": "all of it"})
            return script(system, user)
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, model, goal_check=True)
            sess.run()
        self.assertEqual(sess.state, "done")
        self.assertIsNone(sess._summary()["verification"]["goal"])

    def test_nothing_is_reviewed_unless_the_owner_of_the_session_turned_it_on(self):
        sess, script, _ = self.build([[MISSING]], goal_check=False)
        self.assertEqual((sess.state, script.asked), ("done", []))

    def test_an_answer_from_saved_functions_alone_is_not_reviewed(self):
        script = Script([[MISSING]])

        def model(system, user):
            if system == ag.GOAL_REVIEW_SYSTEM:
                return script(system, user)
            return ag._fake({"action": "use", "call": calc(False)["call"]})
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, model, prompt="what was the answer?", goal_check=True,
                           tools=(CAPACITY, current(False), calc(False)))
            sess.run()
        self.assertEqual((sess.state, script.asked), ("done", []))

    def test_the_review_is_asked_as_a_reviewer_and_about_the_users_words(self):
        for said in ("You did not write it", "Judge only against the user's own words",
                     "a bare fraction such as 200/3", "Do not ask for more than the user did"):
            self.assertIn(said, ag.GOAL_REVIEW_SYSTEM)


if __name__ == "__main__":
    unittest.main()
