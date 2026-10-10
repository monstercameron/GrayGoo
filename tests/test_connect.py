"""Each function of an app is tried on the others the moment it is built (real SBCL, scripted model).

Every function has to bring its own tests, but those run on data the tests made
up. As soon as a function is saved the harness runs it on what the functions
built before it really produce, and runs the integration tests that already
can be run. A function that does not fit is rebuilt once, with what was seen,
instead of being found out when the whole build is over.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import agent_session as ag  # noqa: E402
import qualify  # noqa: E402
import test_integration_proof as ti  # noqa: E402

MISFIT = "DOES NOT FIT THE OTHER FUNCTIONS OF THE APP"
COUNT_LINE = 'run "put kite" then run "count" works'


class FitScript(ti.Script):
    """Like the notebook script, but the reader is put right when told it does not fit."""

    def __call__(self, system, user):
        if MISFIT in user and "GOAL: (cmd-show " in user:
            self.prompts.append(user)
            return ag._fake(ti.show(True))
        return super().__call__(system, user)


class CoverScript(ti.Script):
    """Answers the second request for integration tests with a line for the count command."""

    def __call__(self, system, user):
        if system == ag.INTEGRATION_SYSTEM and self.asked:
            self.asked.append(user)
            return ag._fake({"action": "tests", "lines": [COUNT_LINE]})
        return super().__call__(system, user)


def kinds(sess, kind):
    return [e for e in sess.events if e["kind"] == kind]


class AsItIsBuiltTests(unittest.TestCase):
    def test_a_misfit_is_found_when_the_function_is_built_not_when_the_build_is_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = ti.run(ti.Script(), tmp)
            order = [(e["kind"], e.get("name")) for e in sess.events if e["kind"] in ("connection", "step")]
            found = next(e for e in kinds(sess, "connection") if e["name"] == "cmd-show")
            self.assertLess(order.index(("connection", "cmd-show")), order.index(("step", "handle-command")))
            self.assertIn('its tests use "notes" rows of 2 columns (text, text) such as ("1" "kite"), but cmd-put '
                          'is tested with rows of 1 column (text) such as ("kite")', found["faults"])
            self.assertIn('cmd-show prints NIL when given the data cmd-put really stores: it answered '
                          '"notes: NIL"', found["faults"])
            self.assertTrue(found["rebuilding"])

    def test_it_is_rebuilt_at_once_with_what_was_seen_and_no_round_of_fixes_is_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = FitScript()
            sess = ti.run(script, tmp)
            told = next(p for p in script.prompts if MISFIT in p)
            self.assertIn("cmd-show prints NIL when given the data cmd-put really stores", told)
            self.assertIn("Rebuild it, tests included", told)
            after = [e for e in kinds(sess, "connection") if e["name"] == "cmd-show"][-1]
            self.assertEqual((after["faults"], after["second"]), ([], True))
            self.assertEqual(kinds(sess, "behaviour_fix"), [])
            self.assertEqual([e["verdict"] for e in kinds(sess, "qualification")], ["proven"])
            self.assertEqual(sess.state, "done")
            self.assertEqual(sess._summary()["verification"]["connections"],
                             {"checked": 3, "rebuilt": 1, "left": []})

    def test_a_function_is_rebuilt_to_fit_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = ti.Script(fixes=False)
            sess = ti.run(script, tmp)
            first = next(i for i, e in enumerate(sess.events) if e["kind"] == "qualification")
            built = [e for e in sess.events[:first] if e["kind"] == "decision" and
                     (e.get("plan") or {}).get("name") == "cmd-show"]
            self.assertEqual(len(built), 2)                             # the draft and one rebuild
            self.assertEqual(len([p for p in script.prompts if MISFIT in p]), 1)
            self.assertEqual(sess._summary()["verification"]["connections"]["left"], ["cmd-show"])

    def test_functions_that_fit_cost_no_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = ti.Script()
            script.fixing = True                                        # the reader is right from the start
            sess = ti.run(script, tmp)
            self.assertEqual([p for p in script.prompts if MISFIT in p], [])
            self.assertTrue(all(e["faults"] == [] for e in kinds(sess, "connection")))
            self.assertEqual(sess.model_calls, 5)                       # plan, integration tests, three functions
            self.assertEqual(sess._summary()["verification"]["connections"],
                             {"checked": 3, "rebuilt": 0, "left": []})

    def test_the_integration_tests_are_run_the_moment_the_entry_point_connects_the_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = ti.Script()
            script.fixing = True
            sess = ti.run(script, tmp)
            entry = next(e for e in kinds(sess, "connection") if e["name"] == "handle-command")
            self.assertEqual((entry["lines"], entry["faults"]), (2, []))
            self.assertLess(sess.events.index(entry), sess.events.index(kinds(sess, "qualification")[0]))

    def test_tests_that_fail_behind_the_entry_point_are_told_and_the_entry_point_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = ti.Script(fixes=False)
            sess = ti.run(script, tmp)
            entry = next(e for e in kinds(sess, "connection") if e["name"] == "handle-command")
            self.assertTrue(entry["entry"])
            self.assertIn(ti.CHAIN, entry["faults"][0])
            self.assertEqual([p for p in script.prompts if MISFIT in p and "GOAL: (handle-command " in p], [])

    def test_a_function_changed_later_is_run_through_the_integration_tests_that_use_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = ti.run(ti.Script(), tmp)                             # put right by a round of fixes
            last = [e for e in kinds(sess, "connection") if e["name"] == "cmd-show"][-1]
            self.assertEqual((last["lines"], last["faults"]), (1, []))

    def test_the_next_function_is_shown_the_rows_the_ones_before_it_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = ti.Script()
            ti.run(script, tmp)
            put = next(p for p in script.prompts if "GOAL: (cmd-put " in p)
            show = next(p for p in script.prompts if "GOAL: (cmd-show " in p)
            self.assertNotIn("ROW LAYOUTS THE FUNCTIONS BUILT SO FAR USE", put)
            self.assertIn('ROW LAYOUTS THE FUNCTIONS BUILT SO FAR USE: "notes" rows such as ("kite") (cmd-put)',
                          show)

    def test_the_checks_can_be_turned_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = ti.run(ti.Script(), tmp, connect_checks=False)
            self.assertEqual(kinds(sess, "connection"), [])
            self.assertIsNone(sess._summary()["verification"]["connections"])
            self.assertEqual(sess.state, "done")                        # the proof at the end still holds

    def test_a_function_without_tests_is_never_saved(self):
        self.assertEqual(ag.validate_build(dict(ti.PUT, tests=[])), "at least one test required")
        self.assertEqual(ag.validate_build({k: v for k, v in ti.PUT.items() if k != "tests"}),
                         "at least one test required")
        self.assertIsNone(ag.validate_build(dict(ti.PUT)))


class EveryCommandTests(unittest.TestCase):
    def test_a_command_no_integration_test_types_gets_one_once_the_app_has_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, kept = CoverScript(names=("cmd-put", "cmd-count", "cmd-show", "handle-command")), []
            script.fixing = True
            sess = ti.run(script, tmp, save_integration=kept.append)
            self.assertEqual(len(script.asked), 2)
            self.assertIn("NO TEST TYPES THESE COMMANDS YET:\ncount: Prints how many notes there are",
                          script.asked[1])
            self.assertIn("THE APP ALREADY HAS THESE TESTS, which stay as they are:\n" + ti.CHAIN, script.asked[1])
            self.assertEqual(kept[-1].splitlines(), ti.LINES + [COUNT_LINE])
            ran = kinds(sess, "integration")[-1]
            self.assertEqual((ran["met"], ran["unmet"]), (3, 0))
            self.assertEqual(sess.state, "done")

    def test_an_app_whose_commands_are_all_typed_is_not_asked_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = CoverScript()
            script.fixing = True
            ti.run(script, tmp)
            self.assertEqual(len(script.asked), 1)


class ChainTests(unittest.TestCase):
    TOOLS = [ti.PUT, ti.show(False), ti.OTHER, ti.ENTRY_COUNT,
             {"name": "note-count", "definition": "(defun note-count (rows) (length rows))",
              "tests": [{"call": "(note-count '(1 2))", "expect": "2"}]}]

    def test_a_handler_is_paired_with_itself_and_with_every_other_handler_both_ways(self):
        pairs = [(c["writer"], c["reader"]) for c in qualify.chain_checks(self.TOOLS, "cmd-show")]
        self.assertEqual(pairs, [("cmd-show", "cmd-show"), ("cmd-put", "cmd-show"), ("cmd-count", "cmd-show"),
                                 ("cmd-show", "cmd-put"), ("cmd-show", "cmd-count")])

    def test_the_entry_point_helpers_and_unknown_names_get_no_chain(self):
        for name in ("handle-command", "note-count", "no-such-function", ""):
            self.assertEqual(qualify.chain_checks(self.TOOLS, name), [])

    def test_the_second_handler_is_called_with_its_own_arguments_and_the_real_state(self):
        form = next(c["form"] for c in qualify.chain_checks(self.TOOLS, "cmd-show") if c["writer"] == "cmd-put")
        self.assertIn("(gg-w (handler-case (cmd-put '(\"kite\") '() 0) (error () nil)))", form)
        self.assertIn("(cmd-show '() gg-st 0)", form)
        self.assertIn("(getf gg-r :output)", form)

    def test_a_real_run_tells_a_reader_that_fits_from_one_that_does_not(self):
        import mount  # noqa: F401 - the worker the app is run with
        for right, want in ((False, False), (True, True)):
            tools = [ti.PUT, ti.show(right)]
            prelude = "\n".join(t["definition"] for t in tools)
            checks = [c for c in qualify.chain_checks(tools, "cmd-show") if c["writer"] == "cmd-put"]
            (ok, detail), = qualify.run_chains(checks, prelude, ag._worker_fn)
            self.assertIs(ok, want)
            self.assertEqual("prints NIL" in detail, not right)

    def test_results_are_read_from_one_run(self):
        checks = [{"writer": "a", "reader": "b", "form": "x"}] * 3 + [{"writer": "b", "reader": "b", "form": "x"}]
        reply = '((:same) (:ran "notes: kite") (:raised "The value NIL is not of type NUMBER") (:ran "total NIL"))'
        got = qualify.run_chains(checks, "", lambda src: {"ok": True, "return_value": reply})
        self.assertEqual([ok for ok, _ in got], [None, True, False, False])
        self.assertEqual(got[2][1], "b raises an error when given the data a really stores "
                                    "(The value NIL is not of type NUMBER)")
        self.assertEqual(got[3][1], 'b prints NIL when given its own data: it answered "total NIL"')

    def test_a_run_that_cannot_be_read_concludes_nothing(self):
        checks = [{"writer": "a", "reader": "b", "form": "x"}]
        for evaluate in (lambda src: {"ok": False}, lambda src: {"ok": True, "return_value": "((:ran 1) (:ran 2))"},
                         lambda src: 1 / 0):
            self.assertEqual(qualify.run_chains(checks, "", evaluate), [(None, "the check could not be run")])
        self.assertEqual(qualify.run_chains([], "", None), [])

    def test_only_a_printed_nil_counts_not_a_word_that_holds_the_letters(self):
        for text, hit in (("notes: NIL", True), ("NIL", True), ("(NIL 3)", True), ("VANILLA", False),
                          ("nil desperandum", False), ("NIL-SAFE", False), ("total 0", False)):
            self.assertEqual(bool(qualify.PRINTED_NIL.search(text)), hit, text)


class LayoutTests(unittest.TestCase):
    def test_a_function_whose_rows_differ_from_the_ones_built_before_it_is_told_which(self):
        self.assertEqual(qualify.layout_faults([ti.PUT, ti.show(False)], "cmd-show"), [
            'its tests use "notes" rows of 2 columns (text, text) such as ("1" "kite"), but cmd-put is tested '
            'with rows of 1 column (text) such as ("kite")'])
        self.assertEqual(qualify.layout_faults([ti.PUT, ti.show(True)], "cmd-show"), [])

    def test_the_rows_the_app_really_stores_outrank_the_other_tests(self):
        real = qualify.real_shapes('(("notes" (("1" "kite"))))')
        self.assertEqual(qualify.layout_faults([ti.PUT, ti.show(False)], "cmd-show", real), [])
        self.assertEqual(qualify.layout_faults([ti.PUT, ti.show(False)], "cmd-put", real), [
            'its tests use "notes" rows of 1 column (text) such as ("kite"), but the app really stores rows of '
            '2 columns (text, text) such as ("1" "kite")'])

    def test_the_first_function_to_use_a_table_has_nothing_to_differ_from(self):
        self.assertEqual(qualify.layout_faults([ti.PUT], "cmd-put"), [])
        self.assertEqual(qualify.layout_faults([ti.PUT, ti.ENTRY], "handle-command"), [])

    def test_the_layouts_in_use_are_said_in_a_sentence(self):
        self.assertEqual(qualify.layout_note([ti.PUT, ti.show(True)]),
                         '"notes" rows such as ("kite") (cmd-put, cmd-show)')
        self.assertEqual(qualify.layout_note([ti.ENTRY]), "")

    def test_the_commands_typed_in_test_lines_are_read(self):
        self.assertEqual(qualify.typed_commands(ti.LINES + ['GET /notes shows "x"', 'run "Count" prints "1"']),
                         {"put", "show", "help", "count"})


if __name__ == "__main__":
    unittest.main()
