"""Tests for replay_evidence: collection, the strict reply reader, both sides of a case, the
replay counts, the saved file, and one real SBCL check of a repair the harness makes today."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import replay_evidence as rep  # noqa: E402
import webkit  # noqa: E402

ADD_TWO = '(defun add-two (x) "Add two to X." (+ x 2))'
PICK = '(defun pick (xs) "The first element of XS." (first xs))'
FLIP = '(defun flip (x) "Return X unchanged." x)'
HOPELESS = '(defun hopeless (x) "Never right." x)'
ADD_TWO_TESTS = [{"call": "(add-two 1)", "expect": "3"}]


def build_reply(name, definition, tests, action="build"):
    return json.dumps({"action": action, "name": name, "description": "Test tool %s." % name,
                       "definition": definition, "tests": tests, "call": tests[0]["call"]})


def write_session(directory, name, replies, mode="live", arm="main", project=None, t0=1000.0):
    """One JSONL session: a goal, then a model_call and a model_reply for each (label, text, cost)."""
    events = [{"i": 0, "t": t0, "kind": "goal", "prompt": "p", "mode": mode, "arm": arm,
               "project": project}]
    for label, text, cost in replies:
        events.append({"i": len(events), "t": t0 + len(events), "kind": "model_call",
                       "label": label, "prompt": "x"})
        events.append({"i": len(events), "t": t0 + len(events), "kind": "model_reply",
                       "text": text, "cost_usd": cost, "input_tokens": 1, "output_tokens": 1})
    path = Path(directory) / (name + ".jsonl")
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def make_case(name="add-two", definition=ADD_TWO, tests=None, kind="first drafts", project=None):
    tests = ADD_TWO_TESTS if tests is None else tests
    plan = {"action": "build", "name": name, "description": "Test tool %s." % name,
            "definition": definition, "tests": [dict(t) for t in tests], "call": tests[0]["call"]}
    return {"session": "s-" + name, "project": project or "scratch", "label": "step 1",
            "kind": kind, "name": name, "plan": plan, "cost_usd": 0.0}


class FakeWorker:
    """Stands in for the SBCL worker. VERDICT is "pass", "tests", "run", "undefined" or a
    function of the code that returns one of those. Prelude checks always load."""

    def __init__(self, verdict="pass"):
        self.verdict = verdict
        self.calls = []

    def __call__(self, code):
        self.calls.append(code)
        if "(handler-case (gg-check" not in code:            # a prelude load check
            return {"ok": True, "return_value": "T", "error": "", "stdout": "", "timed_out": False}
        verdict = self.verdict(code) if callable(self.verdict) else self.verdict
        n = code.count("(handler-case (gg-check")
        if verdict == "run":
            return {"ok": False, "return_value": "", "error": "READ error during evaluation",
                    "timed_out": False}
        if verdict == "tests":
            items = ["(:GOT 0)"] + ["T"] * (n - 1)
        elif verdict == "undefined":
            items = ["(:ERROR UNDEFINED-FUNCTION)"] * n
        else:
            items = ["T"] * n
        return {"ok": True, "return_value": "(%s)" % " ".join(items), "error": "",
                "stdout": "", "timed_out": False}


class CollectTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_keeps_only_live_main_build_replies(self):
        build = build_reply("add-two", ADD_TWO, ADD_TWO_TESTS)
        write_session(self.dir, "a-live", [("step 1", build, 0.01)], mode="live")
        write_session(self.dir, "b-demo", [("step 1", build, 0.0)], mode="demo")
        write_session(self.dir, "c-nomem", [("step 1", build, 0.01)], mode="live", arm="nomem")
        write_session(self.dir, "d-use", [("plan", build_reply("x", ADD_TWO, ADD_TWO_TESTS,
                                                              action="use"), 0.01)])
        write_session(self.dir, "e-prose", [("step 1", "Here is the function, no JSON.", 0.01)])
        write_session(self.dir, "f-empty-tests", [("step 1", json.dumps(
            {"action": "build", "name": "x", "definition": ADD_TWO, "tests": []}), 0.01)])
        cases = rep.collect(self.dir)
        self.assertEqual([c["session"] for c in cases], ["a-live"])
        case = cases[0]
        self.assertEqual((case["label"], case["kind"], case["name"], case["project"]),
                         ("step 1", "first drafts", "add-two", "scratch"))
        self.assertEqual(case["cost_usd"], 0.01)
        self.assertEqual(case["plan"]["tests"], ADD_TWO_TESTS)

    def test_mode_none_includes_demo_sessions(self):
        build = build_reply("add-two", ADD_TWO, ADD_TWO_TESTS)
        write_session(self.dir, "a-live", [("step 1", build, 0.01)], mode="live")
        write_session(self.dir, "b-demo", [("step 1", build, 0.0)], mode="demo")
        self.assertEqual(len(rep.collect(self.dir, mode=None)), 2)

    def test_repairs_are_repairs_and_project_filters(self):
        build = build_reply("add-two", ADD_TWO, ADD_TWO_TESTS)
        write_session(self.dir, "p-one", [("repair", build, 0.02)], project="pcrm-a82b")
        cases = rep.collect(self.dir)
        self.assertEqual(cases[0]["kind"], "repairs")
        self.assertEqual(cases[0]["project"], "pcrm-a82b")
        self.assertEqual(len(rep.collect(self.dir, project="pcrm-a82b")), 1)
        self.assertEqual(rep.collect(self.dir, project="other-project"), [])

    def test_limit_takes_the_first_cases_in_session_order(self):
        build = build_reply("add-two", ADD_TWO, ADD_TWO_TESTS)
        for i, name in enumerate(("c", "a", "b")):
            write_session(self.dir, name, [("step 1", build, 0.01)], t0=1000.0 + i)
        self.assertEqual([c["session"] for c in rep.collect(self.dir, limit=2)], ["c", "a"])


class ExtractTests(unittest.TestCase):
    def test_fenced_reply_is_read(self):
        text = "Here is the tool:\n```json\n%s\n```\nThanks." % build_reply(
            "add-two", ADD_TWO, ADD_TWO_TESTS)
        plan = rep.parse_reply(text)
        self.assertEqual(plan["name"], "add-two")
        self.assertEqual(plan["tests"], ADD_TWO_TESTS)

    def test_braces_inside_strings_do_not_end_the_object(self):
        css = '(defun css () "a { b }" "p { c: d }")'
        plan = rep.parse_reply("prefix " + build_reply("css", css, ADD_TWO_TESTS) + " suffix }")
        self.assertEqual(plan["definition"], css)

    def test_text_without_a_clean_object_is_none(self):
        self.assertIsNone(rep.parse_reply(None))
        self.assertIsNone(rep.parse_reply("no braces at all"))
        self.assertIsNone(rep.parse_reply('{"action": "build", '))            # cut off
        self.assertIsNone(rep.parse_reply('{"action": "build", "name": "x", "tests": "a}'))

    def test_no_repair_is_applied(self):
        # a closing quote the model forgot: the harness repairs it, this reader does not
        self.assertIsNone(rep.parse_reply('{"action": "build", "expect": "(1 2)}'))


class EvaluateTests(unittest.TestCase):
    def test_pass_on_both_sides(self):
        worker = FakeWorker("pass")
        res = rep.evaluate(make_case(), "", run=worker)
        self.assertTrue(res["raw"]["ok"])
        self.assertIsNone(res["raw"]["stage"])
        self.assertTrue(res["fixed"]["ok"])
        self.assertEqual(res["raw"]["fixes"], [])
        self.assertEqual(len(worker.calls), 2)          # one evaluation per side

    def test_fail_at_validate_never_runs_sbcl(self):
        worker = FakeWorker("pass")
        res = rep.evaluate(make_case(name="Bad_Name"), "", run=worker)
        for side in ("raw", "fixed"):
            self.assertFalse(res[side]["ok"])
            self.assertEqual(res[side]["stage"], "validate")
            self.assertIn("lowercase kebab-case", res[side]["error"])
        self.assertEqual(worker.calls, [])

    def test_fail_at_run(self):
        res = rep.evaluate(make_case(), "", run=FakeWorker("run"))
        for side in ("raw", "fixed"):
            self.assertFalse(res[side]["ok"])
            self.assertEqual(res[side]["stage"], "run")
            self.assertIn("READ error", res[side]["error"])

    def test_fail_at_tests_says_which_check_and_what_came_back(self):
        res = rep.evaluate(make_case(), "", run=FakeWorker("tests"))
        self.assertEqual(res["raw"]["stage"], "tests")
        self.assertIn("(add-two 1) gave 0, expected 3", res["raw"]["error"])
        self.assertFalse(res["raw"]["undefined"])

    def test_undefined_function_is_flagged(self):
        res = rep.evaluate(make_case(), "", run=FakeWorker("undefined"))
        self.assertEqual(res["raw"]["stage"], "tests")
        self.assertTrue(res["raw"]["undefined"])
        self.assertTrue(res["fixed"]["undefined"])

    def test_unquoted_test_literal_fails_raw_and_is_rescued_by_fixes(self):
        def verdict(code):
            return "tests" if "(pick (3 4 5))" in code else "pass"
        case = make_case(name="pick", definition=PICK,
                         tests=[{"call": "(pick (3 4 5))", "expect": "3"}])
        before = json.dumps(case["plan"], sort_keys=True)
        res = rep.evaluate(case, "", run=FakeWorker(verdict))
        self.assertFalse(res["raw"]["ok"])
        self.assertTrue(res["fixed"]["ok"])
        self.assertIn("quoted-data-list", res["fixed"]["fixes"])
        self.assertEqual(json.dumps(case["plan"], sort_keys=True), before)   # the case is untouched

    def test_prelude_is_passed_into_the_evaluation(self):
        worker = FakeWorker("pass")
        rep.evaluate(make_case(), "(defun helper-x () 1)", run=worker)
        self.assertIn("(defun helper-x () 1)", worker.calls[0])


class PreludeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.agent = Path(tmp.name)

    def _tools(self, path, tools):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(tools), encoding="utf-8")

    def test_kit_and_live_saved_tools_without_the_case_name(self):
        self._tools(self.agent / "tools.json", [
            {"name": "helper-live", "definition": "(defun helper-live (x) x)", "mode": "live"},
            {"name": "add-two", "definition": "(defun add-two (x) x)", "mode": "live"},
            {"name": "helper-demo", "definition": "(defun helper-demo (x) x)"}])
        notes = []
        text = rep._Preludes(self.agent, FakeWorker("pass"), notes).get(None, "add-two")
        self.assertIn("(defun helper-live (x) x)", text)
        self.assertNotIn("(defun add-two", text)
        self.assertNotIn("helper-demo", text)
        self.assertIn(webkit.KIT[0]["definition"], text)
        self.assertEqual(notes, [])

    def test_project_tools_come_from_the_project_folder(self):
        folder = self.agent / "projects" / "demo-1"
        folder.mkdir(parents=True)
        (folder / "project.json").write_text("{}", encoding="utf-8")
        self._tools(folder / "tools.json", [
            {"name": "proj-tool", "definition": "(defun proj-tool (x) x)", "mode": "live"}])
        text = rep._Preludes(self.agent, FakeWorker("pass"), []).get("demo-1", "add-two")
        self.assertIn("(defun proj-tool", text)

    def test_saved_tools_that_do_not_load_fall_back_to_the_kit(self):
        self._tools(self.agent / "tools.json", [
            {"name": "broken-tool", "definition": "(defun broken-tool (x", "mode": "live"}])

        def run(code):
            bad = "(defun broken-tool" in code
            return {"ok": not bad, "return_value": "T", "stdout": "", "timed_out": False,
                    "error": "end of file in broken-tool" if bad else ""}

        notes = []
        pre = rep._Preludes(self.agent, run, notes)
        text = pre.get(None, "add-two")
        self.assertNotIn("broken-tool", text)
        self.assertIn(webkit.KIT[0]["definition"], text)
        pre.get(None, "flip")
        self.assertEqual(len(notes), 1)
        self.assertIn("do not load", notes[0])


class ReplayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.agent = self.root / "agent"
        self.agent.mkdir()
        write_session(self.sessions, "a-pass", [("step 1", build_reply(
            "add-two", ADD_TWO, ADD_TWO_TESTS), 0.01)], t0=1000.0)
        write_session(self.sessions, "b-rescued", [("step 2", build_reply(
            "pick", PICK, [{"call": "(pick (3 4 5))", "expect": "3"}]), 0.01)], t0=1100.0)
        write_session(self.sessions, "c-broken", [("step 3", build_reply(
            "flip", FLIP, [{"call": "(flip 2)", "expect": "2"}]), 0.01)], t0=1200.0)
        write_session(self.sessions, "d-repair", [("repair", build_reply(
            "hopeless", HOPELESS, [{"call": "(hopeless 1)", "expect": "1"}]), 0.02)], t0=1300.0)

    def _verdict(self, code):
        if "(pick (3 4 5))" in code or "'99" in code or "(hopeless 1)" in code:
            return "tests"
        return "pass"

    def _patched_normalize(self):
        real = ag.normalize_plan

        def normalize(plan):
            out = real(plan)
            if plan.get("name") == "flip":                 # a normaliser regression, simulated
                out["tests"][0]["expect"] = "99"
            return out
        return mock.patch.object(ag, "normalize_plan", normalize)

    def test_counts_rates_and_the_rescued_and_broken_cases(self):
        seen = []
        with self._patched_normalize():
            result = rep.replay(sessions_dir=self.sessions, agent_dir=self.agent,
                                run=FakeWorker(self._verdict),
                                progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(result["cases"], 4)
        self.assertEqual(result["sessions"], 4)
        self.assertEqual((result["raw_pass"], result["fixed_pass"]), (2, 2))
        self.assertEqual((result["raw_rate"], result["fixed_rate"]), (0.5, 0.5))
        self.assertEqual((result["rescued"], result["broken"]), (1, 1))
        self.assertEqual(result["rescued_usd"], 0.02)                 # one rescue x the repair median
        self.assertEqual(result["by_kind"]["first drafts"],
                         {"cases": 3, "raw_pass": 2, "fixed_pass": 2})
        self.assertEqual(result["by_kind"]["repairs"],
                         {"cases": 1, "raw_pass": 0, "fixed_pass": 0})
        self.assertEqual(result["raw_failures"], {"validate": 0, "run": 0, "tests": 2})
        self.assertEqual(result["fixed_failures"], {"validate": 0, "run": 0, "tests": 2})
        self.assertEqual(result["by_fix"], [{"fix": "quoted-data-list", "cases": 1, "rescued": 1}])
        self.assertEqual(result["undefined_callee"], {"raw": 0, "fixed": 0})
        self.assertEqual(len(result["broken_cases"]), 1)
        broken = result["broken_cases"][0]
        self.assertEqual((broken["session"], broken["name"], broken["label"]),
                         ("c-broken", "flip", "step 3"))
        self.assertIn("expected 99", broken["error"])
        self.assertEqual(seen[-1], (4, 4))
        self.assertEqual(len(seen), 4)
        self.assertTrue(result["notes"])

    def test_limit_replays_a_prefix(self):
        result = rep.replay(sessions_dir=self.sessions, agent_dir=self.agent,
                            run=FakeWorker("pass"), limit=2)
        self.assertEqual(result["cases"], 2)

    def test_empty_sessions_give_zero_rates(self):
        empty = self.root / "empty"
        empty.mkdir()
        result = rep.replay(sessions_dir=empty, agent_dir=self.agent, run=FakeWorker("pass"))
        self.assertEqual((result["cases"], result["raw_rate"], result["rescued_usd"]), (0, 0.0, 0.0))

    def test_summary_names_the_numbers(self):
        with self._patched_normalize():
            result = rep.replay(sessions_dir=self.sessions, agent_dir=self.agent,
                                run=FakeWorker(self._verdict))
        text = "\n".join(rep.summary_lines(result))
        self.assertIn("Raw (as the model wrote it): 2 of 4 pass (50.0%)", text)
        self.assertIn("Broken cases (raw passes, fixed fails):", text)


class SavedFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_load_of_a_missing_file_is_none(self):
        self.assertIsNone(rep.load(self.dir / "nope.json"))

    def test_load_of_an_unreadable_or_non_object_file_is_none(self):
        (self.dir / "bad.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(rep.load(self.dir / "bad.json"))
        (self.dir / "list.json").write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(rep.load(self.dir / "list.json"))

    def test_save_then_load_round_trips_and_leaves_no_temp_file(self):
        result = {"cases": 2, "notes": ["x"], "rescued_usd": 0.0123}
        path = rep.save(result, self.dir / "evidence" / "replay.json")
        self.assertEqual(rep.load(path), result)
        leftover = [p.name for p in path.parent.iterdir() if p.name != "replay.json"]
        self.assertEqual(leftover, [])

    def test_save_replaces_an_existing_file(self):
        target = self.dir / "replay.json"
        target.write_text('{"cases": 1}', encoding="utf-8")
        rep.save({"cases": 9}, target)
        self.assertEqual(rep.load(target), {"cases": 9})


class RealSbclTests(unittest.TestCase):
    """One check in real SBCL. Skipped when the worker cannot run."""

    @classmethod
    def setUpClass(cls):
        try:
            env = ag._worker_fn("(list 1)")
        except Exception as exc:
            raise unittest.SkipTest("SBCL worker unavailable: %s" % exc)
        if not env.get("ok"):
            raise unittest.SkipTest("SBCL worker unavailable: %s" % env.get("error"))

    def test_unquoted_test_literal_fails_raw_and_passes_fixed(self):
        # From the log of session 136ab9d7ac: the model wrote the test argument as a bare
        # list, which SBCL reads as a call; normalize_plan quotes it today.
        definition = ('(defun initial-state ()\n'
                      '  "Return the initial STATE plist with an empty tasks table."\n'
                      "  '((\"tasks\" ())))")
        tests = [{"call": "(initial-state)", "expect": '(("tasks" ()))'},
                 {"call": '(equal (initial-state) (("tasks" ())))', "expect": "T"}]
        case = make_case(name="initial-state", definition=definition, tests=tests)
        res = rep.evaluate(case, "")
        self.assertFalse(res["raw"]["ok"], res["raw"])
        self.assertEqual(res["raw"]["stage"], "tests")
        self.assertTrue(res["fixed"]["ok"], res["fixed"])
        self.assertIn("quoted-data-list", res["fixed"]["fixes"])


if __name__ == "__main__":
    unittest.main()
