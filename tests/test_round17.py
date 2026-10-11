"""From seven live builds of one project that each used all 80 model calls and none of which got done.

What the logs showed: a command-line app was sent the reminder written for web
apps, and 64% of 1,025 test runs failed; a plan that only repaired saved
functions was built without being judged; on a prompt sent again the handlers
the proofs pointed at were dropped from the plan and the entry point, which was
fine, was rewritten instead; a round cut short by the call limit left a broken
entry point saved.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import agent_session as ag  # noqa: E402
import oracle  # noqa: E402
import test_integration_proof as ti  # noqa: E402


def step(name):
    return {"name": name, "spec": "(%s args state now) -> plist" % name}


def plan(*names):
    return {"action": "plan", "steps": [step(n) for n in names]}


def session(tmp, tools=(), prompt=ti.PROMPT, **attrs):
    sess = ag.Session(prompt, lambda s, u: ag._fake({"action": "use", "call": "1"}),
                      registry=ag.ToolRegistry(Path(tmp) / "t.json"), log_path=Path(tmp) / "l.jsonl")
    for tool in tools:
        sess.registry.add({k: v for k, v in tool.items() if k != "action"})
    sess._app = True
    for key, value in attrs.items():
        setattr(sess, key, value)
    return sess


NOTEBOOK = (ti.PUT, ti.show(True), ti.OTHER, ti.ENTRY_COUNT)


class ReminderTests(unittest.TestCase):
    def test_a_command_line_app_is_reminded_of_commands_not_of_web_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = ti.Script()
            script.fixing = True
            ti.run(script, tmp)
            for prompt in (p for p in script.prompts if "GOAL: (cmd-" in p):
                self.assertIn("COMMAND-LINE APP: A command tool is (cmd-<word> args state now)", prompt)
                self.assertNotIn("WEB APP: REQUEST is a plist", prompt)
                self.assertNotIn("html-page", prompt)

    def test_the_reminder_names_what_went_wrong_in_the_logs(self):
        for said in ("(number-from-string word)", "(table-rows state \"entries\")", "built with LIST",
                     "... matches any text", "never name a parameter t",
                     "never a figure you did not work out digit by digit"):
            self.assertIn(said, ag.CLI_REMINDER)

    def test_a_web_app_keeps_its_own_reminder(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, [{"name": "handle-request", "definition":
                                  "(defun handle-request (request state) (list :status 200 :body \"x\"))"}])
            text = sess._user_prompt("", goal="(page x) -> page")
            self.assertIn("WEB APP: REQUEST is a plist", text)
            self.assertNotIn("COMMAND-LINE APP:", text)


class CommandPlistTests(unittest.TestCase):
    """A command's expected plist is compared by meaning, as a web response is."""

    def check(self, got, want):
        env = ag._worker_fn("%s\n(let ((*print-pretty* nil)) (gg-check %s '%s))" % (ag.GG_CHECK, got, want))
        return env["return_value"].strip().upper()

    def test_dots_stand_for_any_text_and_state_is_compared_only_when_expected(self):
        got = '(list :output (format nil "Report for loan 1~%Total: 5") :state (list 1 2))'
        self.assertEqual(self.check(got, '(:output "...loan 1...Total: 5")'), "T")
        self.assertEqual(self.check(got, '(:output "Report for loan 1~%Total: 5")'), "T")
        self.assertEqual(self.check(got, '(:output "...Total: 5" :state (1 2))'), "T")

    def test_a_wrong_output_or_a_wrong_state_still_fails(self):
        got = '(list :output "Report for loan 1" :state (list 1 2))'
        self.assertTrue(self.check(got, '(:output "...loan 2...")').startswith("(:GOT"))
        self.assertTrue(self.check(got, '(:output "Report for loan 2")').startswith("(:GOT"))
        self.assertTrue(self.check(got, '(:output "...loan 1..." :state (9))').startswith("(:GOT"))
        self.assertTrue(self.check('"Report for loan 1"', '(:output "...loan 1...")').startswith("(:GOT"))

    def test_other_values_are_compared_as_before(self):
        self.assertEqual(self.check("(list 1 2.00001)", "(1 2)"), "T")
        self.assertTrue(self.check("(list 1 3)", "(1 2)").startswith("(:GOT"))
        self.assertEqual(self.check('(list :status 200 :body "<p>Hello</p>")', '(:status 200 :body "...Hello...")'),
                         "T")
        self.assertEqual(self.check("(list :output 5)", "(:output 5)"), "T")       # not text: compared as data


class TrimTests(unittest.TestCase):
    def blamed(self, sess, *names):
        sess._qual = {"implicated": list(names), "proofs": [], "counts": {}}

    def test_on_a_prompt_sent_again_the_functions_the_proofs_point_at_stay_in_the_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, NOTEBOOK, prior_goals=[ti.PROMPT])
            self.blamed(sess, "cmd-show")
            out = sess._trim_repeat(plan("cmd-put", "cmd-show", "cmd-count", "handle-command"))
            self.assertEqual([x["name"] for x in out["steps"]], ["cmd-show"])
            trimmed = [e for e in sess.events if e["kind"] == "plan_trimmed"][-1]
            self.assertEqual(trimmed["kept_as_saved"], ["cmd-put", "cmd-count", "handle-command"])

    def test_the_entry_point_is_rebuilt_when_something_new_has_to_be_wired_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, NOTEBOOK, prior_goals=[ti.PROMPT])
            self.blamed(sess, "cmd-show")
            out = sess._trim_repeat(plan("cmd-drop", "cmd-put", "handle-command"))
            self.assertEqual([x["name"] for x in out["steps"]], ["cmd-drop", "handle-command"])

    def test_a_prompt_sent_again_with_nothing_blamed_is_trimmed_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, NOTEBOOK, prior_goals=[ti.PROMPT])
            out = sess._trim_repeat(plan("cmd-put", "cmd-drop", "handle-command"))
            self.assertEqual([x["name"] for x in out["steps"]], ["cmd-drop", "handle-command"])

    def test_a_round_of_fixes_rebuilds_at_most_four_saved_functions(self):
        names = ["cmd-a", "cmd-b", "cmd-c", "cmd-d", "cmd-e", "cmd-f"]
        tools = [dict(ti.OTHER, name=n, definition=ti.OTHER["definition"].replace("cmd-count", n)) for n in names]
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, list(tools) + [ti.ENTRY])
            self.blamed(sess, *names)
            out = sess._trim_fix(plan(*(names + ["handle-command"])))
            self.assertEqual([x["name"] for x in out["steps"]], names[:ag.MAX_FIX_STEPS])
            trimmed = [e for e in sess.events if e["kind"] == "plan_trimmed"][-1]
            self.assertEqual(trimmed["later"], names[ag.MAX_FIX_STEPS:])
            self.assertEqual(trimmed["kept_as_saved"], ["handle-command"])

    def test_a_round_that_adds_a_function_keeps_the_entry_point_to_wire_it_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, NOTEBOOK)
            self.blamed(sess, "cmd-show")
            out = sess._trim_fix(plan("cmd-drop", "cmd-show", "cmd-put", "handle-command"))
            self.assertEqual([x["name"] for x in out["steps"]], ["cmd-drop", "cmd-show", "handle-command"])

    def test_a_function_is_told_the_failed_checks_that_name_it_or_its_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, NOTEBOOK)
            sess._look_problems = ["the integration test 'run \"put kite\" then run \"show\" prints \"kite\"' fails",
                                   "typing 'count': the test of cmd-count expects 1",
                                   "the tests of cmd-show use other rows"]
            self.assertEqual(sess._problems_of(step("cmd-count")), sess._look_problems[1:2])
            self.assertEqual(sess._problems_of(step("cmd-show")), [sess._look_problems[0], sess._look_problems[2]])
            self.assertEqual(sess._problems_of(step("note-total")), sess._look_problems[:2])


class RepairPlanTests(unittest.TestCase):
    def test_a_plan_that_only_repairs_a_failing_app_is_built_as_a_judged_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            ti.run(ti.Script(fixes=False), tmp, connect_checks=False)        # leaves the reader broken
            script = ti.Script(names=("cmd-show",))
            script.fixing = True
            sess = ti.run(script, tmp, prior_goals=[ti.PROMPT], integration_text="\n".join(ti.LINES) + "\n")
            told = next(p for p in script.prompts if "GOAL: (cmd-show " in p)
            self.assertIn("THE APP WAS TRIED AND THESE CHECKS FAILED:", told)
            self.assertIn(ti.CHAIN, told)
            self.assertNotIn("SCREENSHOTS", told)
            self.assertEqual([e for e in sess.events if e["kind"] == "behaviour_fix"], [])   # the plan was the round
            self.assertEqual(sess.state, "done")

    def test_a_plan_with_a_new_function_is_built_as_a_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, NOTEBOOK)
            sess._qual = {"proofs": [{"id": "state", "ok": False, "label": "x", "detail": "y"}]}
            self.assertTrue(sess._repair_only(plan("cmd-show", "cmd-put")))
            self.assertFalse(sess._repair_only(plan("cmd-show", "cmd-drop")))
            sess._qual = {"proofs": []}
            self.assertFalse(sess._repair_only(plan("cmd-show")))

    def test_a_round_cut_short_by_a_limit_is_undone_when_it_had_made_the_app_worse(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, NOTEBOOK)
            before = {t["name"]: t["definition"] for t in sess.registry.load()}
            scores = iter([(True, 0), (True, 3)])

            def cut_short(plan):
                sess.registry.add(dict(ti.show(False), action=None))          # a worse reader was saved...
                raise ag.BudgetExhausted("model call budget (80) exhausted")  # ...and then the calls ran out
            with mock.patch.object(sess, "_run_steps", cut_short), mock.patch.object(sess, "_smoke", lambda: True), \
                    mock.patch.object(sess, "_app_score", lambda: next(scores)):
                with self.assertRaises(ag.BudgetExhausted):
                    sess._guarded_fix(plan("cmd-show"), "fixes")
            self.assertEqual({t["name"]: t["definition"] for t in sess.registry.load()}, before)
            undone = [e for e in sess.events if e["kind"] == "visual_rollback"][-1]
            self.assertIn("cut short", undone["reason"])
            self.assertFalse(sess._fixing)

    def test_a_round_cut_short_that_did_no_harm_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, (ti.PUT, ti.show(False), ti.ENTRY))
            scores = iter([(True, 2), (True, 1)])

            def cut_short(plan):
                sess.registry.add(dict(ti.show(True), action=None))
                raise ag.BudgetExhausted("model call budget (80) exhausted")
            with mock.patch.object(sess, "_run_steps", cut_short), mock.patch.object(sess, "_smoke", lambda: True), \
                    mock.patch.object(sess, "_app_score", lambda: next(scores)):
                with self.assertRaises(ag.BudgetExhausted):
                    sess._guarded_fix(plan("cmd-show"), "fixes")
            saved = next(t for t in sess.registry.load() if t["name"] == "cmd-show")
            self.assertEqual(saved["definition"], ti.show(True)["definition"])

    def test_a_failing_integration_test_counts_against_a_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, NOTEBOOK)
            sess._smoke_ok = True
            base = sess._app_score()[1]
            sess._integ = {"results": [], "summary": {"unmet": 2}}
            self.assertEqual(sess._app_score(), (True, base + 2))


class OrphanTests(unittest.TestCase):
    LEFTOVER = {"name": "show-lookup", "definition": "(defun show-lookup (rows) (first rows))",
                "tests": [{"call": "(show-lookup '(1 2))", "expect": "1"}]}

    def test_functions_nothing_reaches_and_this_build_did_not_make_are_retired_at_the_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = ti.Script()
            script.fixing = True
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            reg.add(self.LEFTOVER)                                      # left behind by an earlier build
            sess = ti.run(script, tmp)
            self.assertEqual(sess.state, "done")
            self.assertNotIn("show-lookup", [t["name"] for t in sess.registry.load()])
            self.assertIn("show-lookup", [t["name"] for t in sess.registry.load(retired=True)])   # not deleted
            retired = [e for e in sess.events if e["kind"] == "retired" and e.get("orphans")][-1]
            self.assertEqual(retired["tools"], [{"name": "show-lookup", "reason": "nothing in the app calls it"}])

    def test_a_function_this_build_made_is_kept_for_the_next_build_to_wire_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = ti.Script(names=("cmd-put", "cmd-count", "cmd-show", "handle-command"))
            script.fixing, script.count = True, False                   # the entry point never calls cmd-count
            sess = ti.run(script, tmp)
            self.assertIn("cmd-count", [t["name"] for t in sess.registry.load()])

    def test_the_rows_of_leftovers_are_not_held_against_the_app(self):
        import qualify
        stale = {"name": "old-reader", "definition": "(defun old-reader (args state now) (list :output \"x\"))",
                 "tests": [{"call": "(old-reader '() '((\"notes\" ((\"1\" \"kite\" 9)))) 0)", "expect": "T"}]}
        tools = [ti.PUT, ti.show(True), ti.ENTRY, stale]
        out = qualify.qualify(tools, state='(("notes" (("kite"))))')
        state = next(p for p in out["proofs"] if p["id"] == "state")
        self.assertIs(state["ok"], True)
        self.assertNotIn("old-reader", out["implicated"])

    def test_the_entry_point_goes_first_in_a_round_and_garbled_rows_are_not_passed_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session(tmp, NOTEBOOK)
            sess._qual = {"implicated": ["cmd-show", "handle-command"], "proofs": [], "counts": {},
                          "failed": ["replay"]}
            sess._state_note = '"notes" rows look like ("put" "kite")'
            out = sess._trim_fix(plan("cmd-show", "handle-command"))
            self.assertEqual([x["name"] for x in out["steps"]], ["handle-command", "cmd-show"])
            self.assertNotIn("THE APP'S STORED DATA", sess._shared_facts())
            sess._qual["failed"] = ["state"]
            self.assertIn('THE APP\'S STORED DATA, read from the app itself: "notes" rows look like ("put" "kite")',
                          sess._shared_facts())


class HintTests(unittest.TestCase):
    def keys(self, detail):
        return [k for k, _ in oracle.lisp_hints(detail)]

    def test_the_slips_of_the_last_builds_each_get_a_hint(self):
        self.assertIn("constant-param", self.keys(
            "Execution of a form compiled with errors.\nForm:\n  (SB-C::%FUNCALL (LAMBDA (T) (CONS (FIRST T) 1))"))
        self.assertIn("table-as-string", self.keys(
            '("loans" (("1" "150000" "6.5" "30"))) is not a string designator.'))
        self.assertIn("text-as-number", self.keys('The value\n  "150000"\nis not of type\n  REAL'))
        self.assertIn("number-as-text", self.keys("The value\n  6.5d0\nis not of type\n  STRING\nwhen binding STRING"))

    def test_they_do_not_fire_on_other_errors(self):
        for detail in ("The value NIL is not of type NUMBER", "got 5, expected 6",
                       "FOO is not a string designator.", "compiled with errors (LAMBDA (TOTAL) 1)"):
            found = self.keys(detail)
            for key in ("constant-param", "table-as-string", "text-as-number", "number-as-text"):
                self.assertNotIn(key, found, detail)


if __name__ == "__main__":
    unittest.main()
