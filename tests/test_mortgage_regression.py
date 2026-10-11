"""A command-line mortgage calculator that failed builds left damaged, kept as an offline regression target.

The fixtures are copies of artifacts/agent/projects/mortgage-calc-9880 (tools.json and
integration.txt). The app's entry point hands the command word on to its handlers, so
``amortize`` and ``report`` end in an error and ``save`` stores the command word as the
principal. Most of its 82 saved functions are reached from no command. These tests pin
what the harness detects and does about that. No model is called, and the artifacts
folder is never read or written: every session works on a copy in a temporary folder.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures"
TOOLS = FIXTURES / "mortgage_damaged_tools.json"
INTEGRATION = FIXTURES / "mortgage_damaged_integration.txt"
PROMPT = "a command-line mortgage calculator"

import agent_session as ag  # noqa: E402
import mount  # noqa: E402
import qualify  # noqa: E402

# handle-command with the command word removed before each handler is called: the fault the
# damaged app has, repaired in the same style. Its tests are the ones the fixture already has.
ENTRY_REPAIRED = (
    '(defun handle-command (args state now) "Dispatches a command list to the matching cmd-<word> tool, '
    'returning a plist with :output." (let ((word (if (consp args) (string-downcase (first args)) ""))) '
    '(cond ((string= word "help") (list :output "Usage: help, clear, calc, save, list, export, amortize, report")) '
    '((string= word "clear") (cmd-clear (rest args) state now)) '
    '((string= word "calc") (cmd-calc (rest args) state now)) '
    '((string= word "save") (cmd-save (rest args) state now)) '
    '((string= word "list") (cmd-list (rest args) state now)) '
    '((string= word "export") (cmd-export (rest args) state now)) '
    '((string= word "amortize") (cmd-amortize (rest args) state now)) '
    '((string= word "report") (cmd-report (rest args) state now)) '
    '(t (list :output "Usage: help, clear, calc, save, list, export, amortize, report")))))'
)

# a plan of ten saved functions for a round of fixes, the entry point among them
TEN_PLANNED = ["cmd-save", "cmd-amortize", "cmd-report", "cmd-calc", "cmd-clear", "cmd-list", "cmd-export",
               "cmd-list-fancy", "loan-lookup", "handle-command"]


def copy_of_fixture(folder):
    """A copy of the fixture tools.json in FOLDER: the session writes to it, the fixture never."""
    path = Path(folder) / "tools.json"
    shutil.copy(TOOLS, path)
    return path


def open_session(path):
    """A live-mode session on the registry at PATH. Any model call is recorded and fails the call."""
    seen = []

    def generate(system, user):
        seen.append(user)
        raise AssertionError("a model was called, which these tests must never do")
    registry = ag.ToolRegistry(path).for_mode("live")
    sess = ag.Session(PROMPT, generate, registry=registry, mode="live",
                      log_path=Path(path).with_name("log.jsonl"))
    sess.visual = False
    sess._app = sess._cli = True
    sess.integration_text = INTEGRATION.read_text(encoding="utf-8")
    sess.model_calls_seen = seen
    return sess


def checked(sess):
    """Run the zero-token checks the way a finished build does: the smoke check, then the app checks."""
    sess._smoke()
    sess._check_app()
    return sess


def proof(sess, proof_id):
    return next(p for p in sess._qual["proofs"] if p["id"] == proof_id)


def own(tools):
    """The functions the harness counts as the app's own: kit helpers are not put away."""
    return [t for t in tools if not t.get("kit")]


class DamagedAppTests(unittest.TestCase):
    """Facts measured once on the damaged app. These tests only read them."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.mkdtemp(prefix="mortgage-damaged-")
        cls.sess = checked(open_session(copy_of_fixture(cls.folder)))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)

    def test_the_damaged_app_is_disproven_by_its_own_checks(self):
        self.assertEqual(self.sess._qual["verdict"], "disproven")

    def test_the_failed_proofs_are_commands_replay_state_and_integration(self):
        self.assertEqual(set(self.sess._qual["failed"]), {"commands", "replay", "state", "integration"})

    def test_the_commands_proof_names_amortize_and_report_as_ending_in_an_error(self):
        detail = proof(self.sess, "commands")["detail"]
        self.assertIn("amortize", detail)
        self.assertIn("report", detail)
        self.assertIn("junk in string", detail)

    def test_the_replay_proof_counts_nine_failing_tests_and_names_amortize(self):
        detail = proof(self.sess, "replay")["detail"]
        self.assertIn("9 of 18 tests of command handlers fail", detail)
        self.assertIn("typing 'amortize'", detail)

    def test_the_state_proof_names_the_four_column_tests_against_the_three_column_rows_save_stores(self):
        detail = proof(self.sess, "state")["detail"]
        self.assertIn('("save" "150000" "6.5")', detail)
        self.assertIn("cmd-clear", detail)

    def test_the_amortize_and_report_integration_lines_fail_with_an_error_the_command_raised(self):
        results = {r["text"]: r for r in self.sess._integ["results"]}
        failing = [text for text, r in results.items() if r["ok"] is False]
        self.assertEqual(len(failing), 2)
        for text in failing:
            self.assertTrue("amortize 1" in text or "report 1" in text, text)
            self.assertIn("the step raised", results[text]["detail"])
            self.assertIn("junk in string", results[text]["detail"])

    def test_the_help_list_and_export_integration_lines_pass(self):
        results = self.sess._integ["results"]
        passing = [r["text"] for r in results if r["ok"] is True]
        self.assertEqual(len(passing), 3)
        self.assertTrue(any(t == 'run "help" works' for t in passing), passing)
        self.assertTrue(any('then run "list" prints "150000"' in t for t in passing), passing)
        self.assertTrue(any('then run "export" works' in t for t in passing), passing)

    def test_the_entry_point_is_among_the_blamed_functions(self):
        self.assertIn("handle-command", self.sess._blamed())

    def test_the_handlers_behind_the_failing_commands_are_blamed(self):
        self.assertTrue({"cmd-amortize", "cmd-report"} <= self.sess._blamed(), sorted(self.sess._blamed()))

    def test_functions_nothing_in_the_app_reaches_are_not_blamed(self):
        tools = self.sess.registry.load()
        reach = qualify.reachable(tools)
        unreached = {t["name"].lower() for t in own(tools) if t["name"].lower() not in reach}
        self.assertEqual(self.sess._blamed() & unreached, set())
        for name in ("cmd-list-fancy", "cmd-report-fancy", "cmd-help"):
            self.assertIn(name, unreached)
            self.assertNotIn(name, self.sess._blamed())

    def test_most_of_the_saved_functions_are_unreachable_from_the_entry_point(self):
        tools = self.sess.registry.load()
        reach = qualify.reachable(tools)
        functions = own(tools)
        reached = [t for t in functions if t["name"].lower() in reach]
        self.assertEqual(len(functions), 82)
        self.assertEqual(len(reached), 26)
        self.assertGreater(len(functions) - len(reached), len(functions) / 2)

    def test_a_round_of_fixes_is_kept_to_the_limit_and_starts_at_the_entry_point(self):
        plan = {"action": "plan", "steps": [{"name": n, "spec": "(%s args state now) -> plist" % n}
                                            for n in TEN_PLANNED]}
        out = self.sess._trim_fix(plan)
        names = [x["name"] for x in out["steps"]]
        self.assertLessEqual(len(names), ag.MAX_FIX_STEPS)
        self.assertEqual(names, ["handle-command", "cmd-save", "cmd-amortize", "cmd-report"])
        trimmed = [e for e in self.sess.events if e["kind"] == "plan_trimmed"][-1]
        self.assertEqual(trimmed["later"], ["cmd-calc", "cmd-clear", "loan-lookup"])

    def test_an_unreachable_function_in_a_round_of_fixes_is_dropped(self):
        plan = {"action": "plan", "steps": [{"name": n, "spec": "x"} for n in TEN_PLANNED]}
        out = self.sess._trim_fix(plan)
        self.assertNotIn("cmd-list-fancy", [x["name"] for x in out["steps"]])
        trimmed = [e for e in self.sess.events if e["kind"] == "plan_trimmed"][-1]
        self.assertIn("cmd-list-fancy", trimmed["kept_as_saved"])

    def test_stored_data_read_through_a_broken_entry_point_is_not_passed_on_as_fact(self):
        self.assertIn("replay", self.sess._qual["failed"])
        self.assertIn('"loans" rows look like ("save" "150000" "6.5")', self.sess._state_note)
        self.assertNotIn("THE APP'S STORED DATA", self.sess._shared_facts())

    def test_the_quick_checks_find_the_two_readers_that_raise_on_the_data_save_stores(self):
        tools = self.sess.registry.load()
        checks = qualify.chain_checks(tools, "cmd-save")
        results = qualify.run_chains(checks, self.sess.registry.prelude(), ag._worker_fn)
        raised = {c["reader"]: detail for c, (ok, detail) in zip(checks, results)
                  if c["writer"] == "cmd-save" and ok is False}
        self.assertEqual(set(raised), {"cmd-list-fancy", "cmd-report-fancy"})
        self.assertIn("raises an error when given the data cmd-save really stores", raised["cmd-list-fancy"])

    def test_the_readers_the_app_reaches_do_not_raise_on_the_data_save_stores(self):
        tools = self.sess.registry.load()
        reach = qualify.reachable(tools)
        checks = qualify.chain_checks(tools, "cmd-save")
        results = qualify.run_chains(checks, self.sess.registry.prelude(), ag._worker_fn)
        for c, (ok, _) in zip(checks, results):
            if c["writer"] == "cmd-save" and c["reader"] in reach:
                self.assertIsNot(ok, False, c["reader"])

    def test_a_corrected_entry_point_lowers_the_failing_replays_and_the_app_score(self):
        old = self.sess
        folder = tempfile.mkdtemp(prefix="mortgage-repaired-")
        try:
            path = copy_of_fixture(folder)
            registry = ag.ToolRegistry(path).for_mode("live")
            entry = next(t for t in registry.load() if t["name"] == "handle-command")
            registry.add(dict(entry, definition=ENTRY_REPAIRED))
            fixed = checked(open_session(path))
            self.assertLess(fixed._qual["counts"]["replay_failed"], old._qual["counts"]["replay_failed"])
            self.assertLess(fixed._app_score()[1], old._app_score()[1])
            self.assertTrue(fixed._app_score()[0])
            self.assertEqual(fixed._integ["summary"]["unmet"], 0)
            self.assertEqual(fixed._qual["failed"], ["state"])          # the row layout is still wrong
            self.assertEqual(fixed.model_calls_seen, [])
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_a_corrected_entry_point_stores_the_loan_row_without_the_command_word(self):
        folder = tempfile.mkdtemp(prefix="mortgage-repaired-")
        try:
            path = copy_of_fixture(folder)
            registry = ag.ToolRegistry(path).for_mode("live")
            entry = next(t for t in registry.load() if t["name"] == "handle-command")
            registry.add(dict(entry, definition=ENTRY_REPAIRED))
            sess = open_session(path)
            app = mount.MountedApp(sess.registry, mount.StateStore(Path(folder) / "state.sqlite"),
                                   run_lisp=sess.worker_fn)
            self.assertEqual(app.run_command(["save", "150000", "6.5", "30"])["output"], "Saved loan 1")
            stored = app._state()
            self.assertIn('("150000" "6.5" "30")', stored)
            self.assertNotIn('"save"', stored)
            self.assertEqual(app.run_command(["list"])["output"], "150000 6.5 30\n")
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class RetiringTests(unittest.TestCase):
    """Tidying up on a copy of the damaged app."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="mortgage-tidy-")
        self.sess = checked(open_session(copy_of_fixture(self.folder)))

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_retiring_puts_away_only_the_unreachable_functions_and_keeps_them_on_disk(self):
        reg = self.sess.registry
        before = reg.load()
        reach = qualify.reachable(before)
        unreached = {t["name"] for t in own(before) if t["name"].lower() not in reach}
        kit = {t["name"] for t in before if t.get("kit")}
        self.sess._retire_orphans()
        live = {t["name"] for t in reg.load()}
        kept_on_disk = {t["name"] for t in reg.load(retired=True)} - live
        self.assertEqual(len(unreached), 56)
        self.assertEqual(kept_on_disk, unreached)
        self.assertFalse(unreached & live)
        self.assertTrue(kit <= live)

    def test_every_function_reachable_before_is_still_loaded_after(self):
        reg = self.sess.registry
        reach = qualify.reachable(reg.load())
        self.sess._retire_orphans()
        live = {t["name"].lower() for t in reg.load()}
        self.assertEqual(reach - live, set())

    def test_the_app_score_is_not_worse_after_retiring(self):
        before = self.sess._app_score()
        self.sess._retire_orphans()
        after = self.sess._app_score()
        self.assertTrue(after[0] >= before[0])
        self.assertLessEqual(after[1], before[1])

    def test_help_still_answers_through_a_mounted_app_after_retiring(self):
        self.sess._retire_orphans()
        app = mount.MountedApp(self.sess.registry, mount.StateStore(Path(self.folder) / "state.sqlite"),
                               run_lisp=self.sess.worker_fn)
        out = app.run_command(["help"])
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["output"].startswith("Usage: help"), out["output"])


if __name__ == "__main__":
    unittest.main()
