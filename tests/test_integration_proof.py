"""Integration tests are part of the proof of doneness (real SBCL, scripted model).

Live project mortgage-calc: ``save`` stored a loan as three texts, every reader
was tested on rows with an id in front of four, and each function passed its
own tests. A saved loan could never be found again and the build said done.
The model now writes whole paths through the entry point from the goal, before
the code; the harness keeps them with the project and runs them in every
qualification.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import projects  # noqa: E402
import qualify  # noqa: E402

PROMPT = "a command line notebook: put TEXT keeps a note, show prints the notes"
CHAIN = 'run "put kite" then run "show" prints "kite"'
LINES = [CHAIN, 'run "help" prints "put"']

PUT = {"action": "build", "name": "cmd-put", "description": "Keeps a note",
       "definition": '(defun cmd-put (args state now) "Keeps a note." (declare (ignore now)) '
                     '(list :output (format nil "kept ~a" (first args)) '
                     ':state (list (list "notes" (append (second (first state)) (list (list (first args))))))))',
       "call": "(cmd-put '(\"kite\") '() 0)",
       "tests": [{"call": "(cmd-put '(\"kite\") '() 0)",
                  "expect": '(:output "kept kite" :state (("notes" (("kite")))))'}]}
ENTRY = {"action": "build", "name": "handle-command", "description": "Runs one command",
         "definition": '(defun handle-command (args state now) "Runs one command." '
                       '(cond ((equal (first args) "help") (list :output "commands: put, show, help")) '
                       '((equal (first args) "put") (if (rest args) (cmd-put (rest args) state now) '
                       '(list :output "usage: put TEXT"))) '
                       '((equal (first args) "show") (cmd-show (rest args) state now)) '
                       '(t (list :output "unknown command, try help"))))',
         "call": "(handle-command '(\"help\") '() 0)",
         "tests": [{"call": "(handle-command '(\"help\") '() 0)",
                    "expect": '(:output "commands: put, show, help")'}]}
ENTRY_COUNT = dict(ENTRY, definition=ENTRY["definition"].replace(
    '((equal (first args) "show")', '((equal (first args) "count") (cmd-count (rest args) state now)) '
                                    '((equal (first args) "show")').replace("put, show, help", "put, show, count, help"),
    tests=[{"call": "(handle-command '(\"help\") '() 0)", "expect": '(:output "commands: put, show, count, help")'}])
OTHER = {"action": "build", "name": "cmd-count", "description": "Prints how many notes there are",
         "definition": '(defun cmd-count (args state now) "Prints how many notes there are." '
                       '(declare (ignore args now)) '
                       '(list :output (format nil "~a" (length (second (first state))))))',
         "call": "(cmd-count '() '((\"notes\" ((\"kite\")))) 0)",
         "tests": [{"call": "(cmd-count '() '((\"notes\" ((\"kite\")))) 0)", "expect": '(:output "1")'}]}


def show(right):
    """cmd-show; the wrong one reads the note behind an id that cmd-put never writes."""
    row = '("kite")' if right else '("1" "kite")'
    return {"action": "build", "name": "cmd-show", "description": "Prints the notes",
            "definition": '(defun cmd-show (args state now) "Prints the notes." (declare (ignore args now)) '
                          '(list :output (format nil "notes:~{ ~a~}" (mapcar (function %s) '
                          '(second (first state))))))' % ("first" if right else "second"),
            "call": "(cmd-show '() '((\"notes\" (%s))) 0)" % row,
            "tests": [{"call": "(cmd-show '() '((\"notes\" (%s))) 0)" % row,
                       "expect": '(:output "notes: kite")'}]}


class Script:
    """Builds the notebook with a reader that disagrees with the writer; fixes it when told (or never)."""

    def __init__(self, fixes=True, lines=LINES, fix_plan=("cmd-show",),
                 names=("cmd-put", "cmd-show", "handle-command")):
        self.fixes, self.lines, self.fix_plan, self.names = fixes, lines, fix_plan, list(names)
        self.prompts, self.asked, self.fixing = [], [], False
        self.count = "cmd-count" in names        # the entry point then knows the count command

    def plan(self, names):
        return ag._fake({"action": "plan", "steps": [
            {"name": n, "spec": "(%s args state now) -> plist" % n} for n in names]})

    def __call__(self, system, user):
        self.prompts.append(user)
        if system == ag.INTEGRATION_SYSTEM:
            self.asked.append(user)
            return ag._fake({"action": "tests", "lines": list(self.lines)})
        if "THE FINISHED APP WAS TRIED AND THESE CHECKS FAILED" in user:
            self.fixing = True
            return self.plan(self.fix_plan)
        if "GOAL: (cmd-show " in user:
            return ag._fake(show(self.fixing and self.fixes))
        for reply in (PUT, ENTRY_COUNT if self.count else ENTRY, OTHER):
            if "GOAL: (%s " % reply["name"] in user:
                return ag._fake(reply)
        return self.plan(self.names)


def run(script, tmp, prompt=PROMPT, **attrs):
    sess = ag.Session(prompt, script, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                      log_path=Path(tmp) / "l.jsonl")
    sess.visual = False
    sess.write_integration = True
    for key, value in attrs.items():
        setattr(sess, key, value)
    sess.run()
    return sess


def proof(event, pid):
    return next((p for p in event["proofs"] if p["id"] == pid), None)


class WrittenOnceTests(unittest.TestCase):
    def test_they_are_written_from_the_goal_before_the_code_and_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, kept = Script(), []
            sess = run(script, tmp, save_integration=kept.append)
            self.assertEqual(len(script.asked), 1)
            self.assertIn("APP KIND: command-line app", script.asked[0])
            self.assertIn(PROMPT, script.asked[0])
            self.assertIn("planned: (cmd-put args state now)", script.asked[0])
            self.assertEqual(kept, ["\n".join(LINES) + "\n"])
            kinds = [e["kind"] for e in sess.events]
            self.assertLess(kinds.index("integration_written"), kinds.index("step"))

    def test_lines_that_cannot_be_run_as_written_are_dropped_not_guessed_at(self):
        lines = [CHAIN, "the app should be nice", 'GET /notes shows "kite"', "scenario: all of it",
                 CHAIN, 'run "help" prints "put"'] + ['run "put a%d" prints "a%d"' % (i, i) for i in range(12)]
        with tempfile.TemporaryDirectory() as tmp:
            sess = run(Script(lines=lines), tmp)
            written = next(e for e in sess.events if e["kind"] == "integration_written")
            self.assertEqual(written["lines"][:2], [CHAIN, 'run "help" prints "put"'])
            self.assertEqual(len(written["lines"]), ag.MAX_INTEGRATION_TESTS)
            self.assertEqual(written["dropped"], len(lines) - ag.MAX_INTEGRATION_TESTS)
            self.assertNotIn("/notes", sess.integration_text)

    def test_a_project_that_has_them_is_not_asked_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(Script(), tmp)
            script = Script()
            script.fixing = True
            sess = run(script, tmp, prior_goals=[PROMPT], integration_text=CHAIN + "\n")
            self.assertEqual(script.asked, [])
            self.assertEqual(sess.integration_text, CHAIN + "\n")
            self.assertTrue(any(e["kind"] == "integration" for e in sess.events))

    def test_nothing_is_asked_unless_the_owner_of_the_session_turned_it_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Script()
            sess = run(script, tmp, write_integration=False)
            self.assertEqual(script.asked, [])
            self.assertFalse(any(e["kind"] == "integration" for e in sess.events))
            self.assertIsNone(sess._summary()["verification"]["integration"])

    def test_a_reply_that_is_no_list_of_lines_leaves_the_build_going(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = run(Script(lines=()), tmp)
            self.assertEqual(sess.integration_text, "")
            self.assertEqual(next(e for e in sess.events if e["kind"] == "integration_written")["lines"], [])
            self.assertTrue(sess._built)


class GroundedTests(unittest.TestCase):
    """The tests are written before the code: their author has not seen what the app prints."""

    def written(self, lines, **attrs):
        with tempfile.TemporaryDirectory() as tmp:
            script = Script(lines=lines)
            script.fixing = True
            sess = run(script, tmp, **attrs)
            return next(e for e in sess.events if e["kind"] == "integration_written"), sess

    def test_text_typed_in_the_line_and_names_of_commands_may_be_expected(self):
        lines = [CHAIN, 'run "help" prints "put"', 'run "help" prints "show"',
                 'run "put Kite" then run "show" prints "kite"']
        event, _ = self.written(lines)
        self.assertEqual((event["lines"], event["eased"]), (lines, []))

    def test_a_heading_a_format_or_a_figure_nobody_has_seen_becomes_a_works_check(self):
        event, sess = self.written([
            'run "put kite" then run "show" prints "Notes:"',
            'run "put kite" then run "show" prints "1. kite"',
            'run "help" prints "Usage:"',
            'run "show" does not print "kite"'])
        self.assertEqual(event["lines"], ['run "put kite" then run "show" works', 'run "help" works',
                                          'run "show" does not print "kite"'])
        self.assertEqual(len(event["eased"]), 3)
        self.assertEqual(sess.state, "done")
        ran = [e for e in sess.events if e["kind"] == "integration"][-1]
        self.assertEqual((ran["met"], ran["unmet"]), (3, 0))

    def test_text_the_goal_itself_puts_in_quotes_may_be_expected(self):
        goal = PROMPT + '; with no notes show prints "nothing yet"'
        event, _ = self.written(['run "show" prints "nothing yet"', 'run "show" prints "empty"'], prompt=goal)
        self.assertEqual(event["lines"], ['run "show" prints "nothing yet"', 'run "show" works'])

    def test_a_works_check_fails_when_a_command_ends_in_an_error(self):
        import requirements
        reqs, errors = requirements.parse('run "put kite" then run "boom" works\nrun "show" works')
        self.assertEqual(errors, [])

        def command():
            def run_words(words):
                if words[0] == "boom":
                    raise RuntimeError("The value NIL is not of type STRING")
                return "ok"
            return run_words
        first, second = requirements.run(reqs, command=command)
        self.assertIs(first["ok"], False)
        self.assertIn("The value NIL is not of type STRING", first["detail"])
        self.assertEqual((second["ok"], second["detail"]),
                         (True, 'run "show" ran without an error; it printed "ok".'))


class ProofTests(unittest.TestCase):
    def test_parts_that_pass_alone_but_not_together_are_fixed_before_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Script()
            sess = run(script, tmp)
            quals = [e for e in sess.events if e["kind"] == "qualification"]
            first, last = quals[0], quals[-1]
            self.assertEqual(first["verdict"], "disproven")
            self.assertIs(proof(first, "integration")["ok"], False)
            self.assertIn("1 of 2 fail", proof(first, "integration")["detail"])
            self.assertIs(proof(first, "state")["ok"], False)          # the stored-data proof names the cause
            self.assertIn("cmd-show", proof(first, "state")["detail"])
            fix = next(p for p in script.prompts if "THE FINISHED APP WAS TRIED" in p)
            self.assertIn("the integration test '%s' fails" % CHAIN, fix)
            self.assertEqual(last["verdict"], "proven")
            self.assertIs(proof(last, "integration")["ok"], True)
            self.assertEqual(sess.state, "done")
            summary = sess._summary()
            self.assertEqual(summary["verification"]["integration"]["unmet"], 0)
            self.assertEqual(summary["verification"]["integration"]["met"], 2)

    def test_the_function_being_fixed_is_shown_the_real_rows_and_the_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Script()
            run(script, tmp)
            steps = [p for p in script.prompts if "GOAL: (cmd-show " in p]
            self.assertGreaterEqual(len(steps), 2)
            self.assertNotIn("THE APP'S STORED DATA", steps[0])        # nothing was stored yet
            self.assertIn("THE FINISHED APP MUST PASS THESE INTEGRATION TESTS", steps[0])
            self.assertIn(CHAIN, steps[0])
            self.assertIn('THE APP\'S STORED DATA, read from the app itself: "notes" rows look like ("kite")',
                          steps[-1])

    def test_an_app_that_keeps_failing_one_is_not_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = run(Script(fixes=False), tmp)
            self.assertEqual(sess.state, "failed")
            summary = sess._summary()
            self.assertIn("integration", summary["qualification"]["failed"])
            self.assertEqual(summary["verification"]["integration"]["unmet_texts"], [CHAIN])
            failed = next(e for e in sess.events if e["kind"] == "integration")
            row = next(r for r in failed["results"] if r["ok"] is False)
            self.assertEqual(row["text"], CHAIN)

    def test_integration_counts_as_evidence_that_the_app_does_its_job(self):
        self.assertIn("integration", qualify.BEHAVIOUR)
        self.assertIn("integration", ag.Session.DECIDING_PROOFS)
        self.assertIn("state", ag.Session.DECIDING_PROOFS)


class QualifierTests(unittest.TestCase):
    TOOLS = [{"name": "handle-command", "definition": ENTRY["definition"], "tests": ENTRY["tests"]}]

    def integ(self, **counts):
        base = {"total": 0, "met": 0, "unmet": 0, "unchecked": 0, "unmet_texts": []}
        base.update(counts)
        out = qualify.qualify(self.TOOLS, integ=base)
        return next((p for p in out["proofs"] if p["id"] == "integration"), None), out

    def test_one_failing_test_disproves(self):
        found, out = self.integ(total=3, met=2, unmet=1, unmet_texts=[CHAIN])
        self.assertIs(found["ok"], False)
        self.assertIn(CHAIN, found["detail"])
        self.assertIn("integration", out["failed"])

    def test_all_passing_proves_it(self):
        found, out = self.integ(total=3, met=3)
        self.assertIs(found["ok"], True)
        self.assertEqual(found["detail"], "all 3 pass")
        self.assertNotIn("integration", out["failed"])

    def test_tests_that_could_not_be_run_prove_nothing(self):
        found, _ = self.integ(total=2, unchecked=2)
        self.assertIsNone(found["ok"])
        partly, _ = self.integ(total=3, met=2, unchecked=1)
        self.assertIs(partly["ok"], True)
        self.assertIn("1 could not be run", partly["detail"])

    def test_no_tests_means_no_proof_row(self):
        self.assertIsNone(self.integ()[0])
        self.assertIsNone(next((p for p in qualify.qualify(self.TOOLS)["proofs"] if p["id"] == "integration"),
                               None))


class LaterPromptTests(unittest.TestCase):
    MORE = 'run "put kite" then run "count" prints "1"'
    WORKS = 'run "put kite" then run "count" works'

    def later(self, tmp, plan, lines):
        run(Script(), tmp)                                               # the notebook, proven
        script, kept = Script(lines=lines, names=plan), []
        script.fixing = script.count = True
        sess = run(script, tmp, prompt="also add a count command that prints how many notes there are",
                   prior_goals=[PROMPT], integration_text="\n".join(LINES) + "\n", save_integration=kept.append)
        return script, sess, kept

    def test_a_prompt_that_adds_a_function_adds_tests_for_it_and_keeps_the_old_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, sess, kept = self.later(tmp, ["cmd-count", "handle-command"],
                                            [self.MORE, CHAIN] + ['run "put a%d" then run "count" prints "1"' % i
                                                                 for i in range(9)])
            self.assertEqual(len(script.asked), 1)
            self.assertIn("THE APP IS BEING EXTENDED. NEW GOAL: also add a count command", script.asked[0])
            self.assertIn("THE APP ALREADY HAS THESE TESTS, which stay as they are:\n" + CHAIN, script.asked[0])
            written = next(e for e in sess.events if e["kind"] == "integration_written")
            self.assertTrue(written["added"])
            # "1" was not typed in the line: a figure the author worked out, kept as a works check
            self.assertEqual(written["lines"][0], self.WORKS)
            self.assertEqual(written["eased"][0], self.MORE)
            self.assertEqual(len(written["lines"]), ag.MAX_INTEGRATION_MORE)
            self.assertNotIn(CHAIN, written["lines"])                   # a repeated test is not added twice
            self.assertEqual(kept[-1].splitlines()[:3], LINES + [self.WORKS])
            self.assertEqual(sess.state, "done")

    def test_a_prompt_that_only_changes_saved_functions_adds_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, sess, kept = self.later(tmp, ["cmd-show"], [self.MORE])
            self.assertEqual(script.asked, [])
            self.assertEqual(kept, [])
            self.assertEqual(sess.integration_text, "\n".join(LINES) + "\n")

    def test_a_project_never_keeps_more_than_its_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(Script(), tmp)
            full = ['run "put a%d" prints "a%d"' % (i, i) for i in range(ag.MAX_INTEGRATION_TOTAL)]
            script = Script(lines=[self.MORE], names=("cmd-count", "handle-command"))
            script.fixing = True
            run(script, tmp, prompt="also add a count command", prior_goals=[PROMPT],
                integration_text="\n".join(full) + "\n")
            self.assertEqual(script.asked, [])


class GiveUpTests(unittest.TestCase):
    def test_the_user_is_told_a_wrong_integration_test_can_be_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = run(Script(fixes=False), tmp)
            gave = [e for e in sess.events if e["kind"] == "gave_up"][-1]
            self.assertIn("change or delete it under 'Your requirements'", gave["hint"])
            self.assertIn(CHAIN, gave["detail"])


class RouteTests(unittest.TestCase):
    def test_the_user_can_read_change_and_clear_them(self):
        import json
        from types import SimpleNamespace
        sys.path.insert(0, str(ROOT / "dashboard"))
        import server
        with tempfile.TemporaryDirectory() as tmp:
            agent = ag.SessionManager(registry=ag.ToolRegistry(Path(tmp) / "tools.json"))
            proj, _ = agent.projects.create("Notebook")
            ctx = SimpleNamespace(root=ROOT, manager=None, agent=agent)
            path = "/api/agent/projects/%s/integration" % proj["id"]
            self.assertEqual(server.dispatch("GET", path, b"", ctx), (200, {"text": "", "count": 0, "errors": []}))
            status, data = server.dispatch("POST", path, json.dumps({"text": CHAIN + "\nbe nice\n"}).encode(), ctx)
            self.assertEqual((status, data["text"], len(data["errors"])), (200, CHAIN + "\nbe nice\n", 1))
            self.assertEqual(agent.projects.integration(proj["id"]), CHAIN + "\nbe nice\n")
            reqs = server.dispatch("GET", path.replace("/integration", "/requirements"), b"", ctx)[1]
            self.assertEqual((reqs["text"], reqs["integration"]), ("", CHAIN + "\nbe nice\n"))
            self.assertEqual(server.dispatch("POST", path, json.dumps({"text": ""}).encode(), ctx)[1]["text"], "")
            self.assertEqual(server.dispatch("GET", "/api/agent/projects/none-0000/integration", b"", ctx)[0], 404)
            self.assertEqual(server.dispatch("DELETE", path, b"", ctx)[0], 404)

    def test_a_live_build_of_a_project_is_given_its_tests_and_a_place_to_keep_new_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ag.SessionManager(registry=ag.ToolRegistry(Path(tmp) / "tools.json"))
            proj, _ = agent.projects.create("Notebook")
            agent.projects.set_integration(proj["id"], CHAIN + "\n")
            self.assertEqual(agent.projects.integration(proj["id"]), CHAIN + "\n")
            self.assertTrue(agent.projects.integration_path(proj["id"]).name.endswith("integration.txt"))


class FixRoundTests(unittest.TestCase):
    def test_a_round_of_fixes_leaves_working_saved_functions_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Script(fix_plan=("cmd-count", "cmd-show"),
                            names=("cmd-put", "cmd-count", "cmd-show", "handle-command"))
            sess = run(script, tmp)
            trimmed = [e for e in sess.events if e["kind"] == "plan_trimmed" and e.get("fix")]
            self.assertEqual(trimmed[0]["kept_as_saved"], ["cmd-count"])
            self.assertEqual(trimmed[0]["planned"], ["cmd-show"])
            self.assertEqual(len([p for p in script.prompts if "GOAL: (cmd-count " in p]), 1)
            self.assertEqual(sess.state, "done")

    def test_the_commands_a_failing_integration_test_types_may_be_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Script(fix_plan=("cmd-put", "cmd-show"))
            sess = run(script, tmp)
            self.assertEqual([e for e in sess.events if e["kind"] == "plan_trimmed" and e.get("fix")], [])
            self.assertEqual(len([p for p in script.prompts if "GOAL: (cmd-put " in p]), 2)
            self.assertEqual(sess.state, "done")

    def test_a_new_function_in_a_round_of_fixes_is_still_built(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = run(Script(fix_plan=("cmd-count", "cmd-show")), tmp)
            self.assertIn("cmd-count", [n for n, _ in sess._built])

    def test_fix_rounds_have_a_plans_budget_of_calls_after_a_small_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(Script(), tmp)                                           # the app, with its fault fixed
            sess = run(Script(fixes=False), tmp, prompt="make show print the notes again",
                       prior_goals=[PROMPT], integration_text=CHAIN + "\n")
            self.assertGreaterEqual(sess.max_calls, ag.MAX_MODEL_CALLS_PLAN)


class CacheTests(unittest.TestCase):
    def test_a_prompt_seen_before_is_not_answered_by_one_saved_function_of_an_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = run(Script(), tmp)
            first.registry.note_use("cmd-show", PROMPT, "(cmd-show '() '() 0)")
            self.assertEqual(first.registry.find_cached(PROMPT)["name"], "cmd-show")
            script = Script()
            script.fixing = True
            sess = run(script, tmp, prior_goals=[PROMPT], integration_text=CHAIN + "\n")
            self.assertFalse(any(e["kind"] == "decision" and e.get("action") == "cache" for e in sess.events))
            self.assertTrue(any(e["kind"] == "qualification" for e in sess.events))


class StoreTests(unittest.TestCase):
    def test_a_project_keeps_its_integration_tests_beside_its_requirements(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = projects.ProjectStore(Path(tmp))
            pid = store.create("notebook")[0]["id"]
            self.assertEqual(store.integration(pid), "")
            self.assertEqual(store.set_integration(pid, CHAIN + "\n"), (True, None))
            self.assertEqual(store.integration(pid), CHAIN + "\n")
            self.assertEqual(store.requirements(pid), "")
            self.assertNotEqual(store.integration_path(pid), store.requirements_path(pid))
            self.assertEqual(store.set_integration("no-such-project", "x")[0], False)


if __name__ == "__main__":
    unittest.main()
