"""Tests for reviewer_eval: case file, message layout, reading replies, scoring, resume,
spend cap and the command line.

Nothing here calls a paid model. The dry run uses the scripted reviewer, and the live
generator is patched to fail wherever a test could reach it.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import reviewer_eval as rv  # noqa: E402

CASES = ROOT / "reviewer_cases.json"


def _snapshot(root):
    """Every file under ROOT/artifacts with its mtime and size, to prove a run wrote nothing there."""
    out = {}
    if root.exists():
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                p = os.path.join(dirpath, name)
                st = os.stat(p)
                out[p] = (st.st_mtime_ns, st.st_size)
    return out


def _boom(*args, **kwargs):
    raise AssertionError("the live model must not be reached in this test")


def _case(cid="t-case", label="good", trap=None, description="Adds two numbers."):
    case = {"id": cid, "label": label, "goals": ["Add two numbers."],
            "tools": [{"name": "add2", "params": ["a", "b"], "description": description,
                       "tests": [{"call": "(add2 1 2)", "expect": "3"}]}],
            "answer": {"call": "(add2 1 2)", "value": "3"}}
    if label == "incomplete":
        case["missing_truth"] = "Subtraction is asked for and absent."
    if trap:
        case["trap"] = trap
    return case


def _record(cid, label, verdict, trap=None, repeat=0, why="", cost=0.0):
    return {"id": cid, "label": label, "trap": trap, "repeat": repeat, "verdict": verdict,
            "why": why, "missing": [], "cost_usd": cost, "input_tokens": 0,
            "output_tokens": 0, "latency_ms": 0.0, "raw": "", "error": None}


def _reply(obj):
    return json.dumps(obj)


class CaseFileTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rv-cases-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, data):
        path = self.tmp / "cases.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_the_case_file_loads_and_is_balanced(self):
        cases = rv.load_cases(CASES)
        self.assertGreaterEqual(len(cases), 24)
        labels = [c["label"] for c in cases]
        self.assertEqual(labels.count("good"), labels.count("incomplete"))
        self.assertEqual(len({c["id"] for c in cases}), len(cases))

    def test_every_incomplete_case_says_in_one_sentence_what_is_missing(self):
        for case in rv.load_cases(CASES):
            if case["label"] == "incomplete":
                with self.subTest(case=case["id"]):
                    self.assertTrue(case["missing_truth"].strip())
                    self.assertEqual(case["missing_truth"].count(". "), 0,
                                     "one sentence, not two")

    def test_every_case_names_the_answer_and_at_least_one_tested_call(self):
        for case in rv.load_cases(CASES):
            with self.subTest(case=case["id"]):
                self.assertTrue(case["answer"]["call"].startswith("("))
                self.assertTrue(all(t["tests"] for t in case["tools"]))

    def test_most_cases_name_the_trap_they_are_built_to_catch(self):
        traps = [c.get("trap") for c in rv.load_cases(CASES)]
        self.assertTrue(sum(1 for t in traps if t) >= 12)

    def test_a_case_with_an_unknown_label_is_refused_with_its_id(self):
        bad = _case("bad-label")
        bad["label"] = "maybe"
        with self.assertRaisesRegex(ValueError, "bad-label.*label"):
            rv.load_cases(self._write([bad]))

    def test_an_incomplete_case_without_missing_truth_is_refused(self):
        bad = _case("no-truth", label="incomplete")
        del bad["missing_truth"]
        with self.assertRaisesRegex(ValueError, "missing_truth"):
            rv.load_cases(self._write([bad]))

    def test_a_case_missing_its_answer_is_refused(self):
        bad = _case("no-answer")
        del bad["answer"]
        with self.assertRaisesRegex(ValueError, "answer"):
            rv.load_cases(self._write([bad]))

    def test_a_test_without_an_expectation_is_refused(self):
        bad = _case("no-expect")
        bad["tools"][0]["tests"] = [{"call": "(add2 1 2)"}]
        with self.assertRaisesRegex(ValueError, "no-expect"):
            rv.load_cases(self._write([bad]))

    def test_duplicate_case_ids_are_refused(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            rv.load_cases(self._write([_case("same"), _case("same")]))

    def test_a_file_that_is_not_a_list_is_refused(self):
        with self.assertRaises(ValueError):
            rv.load_cases(self._write({"id": "x"}))


class MessageTests(unittest.TestCase):

    def test_message_has_each_section_in_the_order_the_reviewer_expects(self):
        text = rv.build_message(_case())
        marks = ["WHAT THE USER ASKED (latest last):", "\nWHAT WAS BUILT:\n",
                 "TOOL add2 (a b): ", "   tested: (add2 1 2) => 3",
                 "THE ANSWER SHOWN TO THE USER: (add2 1 2) => 3"]
        positions = [text.index(m) for m in marks]
        self.assertEqual(positions, sorted(positions))

    def test_message_lists_the_goal_as_a_dash_line_under_the_first_heading(self):
        text = rv.build_message(_case())
        self.assertTrue(text.startswith("WHAT THE USER ASKED (latest last):\n- Add two numbers."))

    def test_a_tool_without_params_shows_empty_parentheses(self):
        case = _case()
        case["tools"][0]["params"] = []
        self.assertIn("TOOL add2 (): ", rv.build_message(case))

    def test_long_descriptions_and_calls_are_cut_as_the_real_message_cuts_them(self):
        case = _case(description="word " * 100)
        case["tools"][0]["tests"][0]["call"] = "(add2 " + "1 " * 200 + ")"
        text = rv.build_message(case)
        desc = [ln for ln in text.splitlines() if ln.startswith("TOOL add2")][0]
        self.assertLessEqual(len(desc.split("): ", 1)[1]), 160)
        call = [ln for ln in text.splitlines() if ln.startswith("   tested:")][0]
        self.assertLessEqual(len(call.split(" => ")[0]) - len("   tested: "), 220)

    def test_only_three_tests_per_tool_are_shown(self):
        case = _case()
        case["tools"][0]["tests"] = [{"call": "(add2 %d 0)" % i, "expect": str(i)} for i in range(5)]
        self.assertEqual(rv.build_message(case).count("   tested: "), 3)

    def test_message_is_the_same_for_the_same_case(self):
        self.assertEqual(rv.build_message(_case()), rv.build_message(_case()))

    def test_worst_case_cost_grows_with_the_message_and_is_bounded_by_the_reply_cap(self):
        system = rv.review_system()
        small = rv.worst_call_usd(system, "x")
        large = rv.worst_call_usd(system, "x" * 30000)
        self.assertGreater(large, small)
        floor = rv.REPLY_CAP_TOKENS * rv.COST_OUTPUT_USD_PER_MTOK / 1_000_000
        self.assertGreaterEqual(small, floor)

    def test_the_system_prompt_is_the_reviewers_own(self):
        self.assertEqual(rv.review_system(), ag.GOAL_REVIEW_SYSTEM)


class ReplyTests(unittest.TestCase):

    def test_met_true_is_accept(self):
        self.assertEqual(rv.judge(_reply({"action": "review", "met": True, "missing": [],
                                          "why": "all there"})), "accept")

    def test_missing_items_are_reject(self):
        self.assertEqual(rv.judge(_reply({"action": "review", "met": False,
                                          "missing": ["the total"], "why": "no total"})), "reject")

    def test_an_empty_missing_list_is_accept(self):
        self.assertEqual(rv.judge(_reply({"action": "review", "met": False,
                                          "missing": [], "why": ""})), "accept")

    def test_missing_items_that_are_all_blank_are_accept_as_the_reviewer_reads_them(self):
        self.assertEqual(rv.judge(_reply({"action": "review", "met": False,
                                          "missing": ["  "], "why": ""})), "accept")

    def test_missing_not_a_list_is_unreadable(self):
        self.assertEqual(rv.judge(_reply({"action": "review", "met": False,
                                          "missing": "the total"})), "unreadable")

    def test_met_true_with_missing_not_a_list_is_unreadable(self):
        self.assertEqual(rv.judge(_reply({"action": "review", "met": True,
                                          "missing": None})), "unreadable")

    def test_reply_that_is_not_json_is_unreadable(self):
        self.assertEqual(rv.judge("I think it is fine."), "unreadable")

    def test_empty_reply_is_unreadable(self):
        self.assertEqual(rv.judge(""), "unreadable")
        self.assertEqual(rv.judge({"text": "", "cost_usd": 0.0}), "unreadable")

    def test_generator_result_is_read_through_its_text(self):
        reply = {"text": _reply({"action": "review", "met": False, "missing": ["x"], "why": "y"}),
                 "cost_usd": 0.1}
        self.assertEqual(rv.judge(reply), "reject")

    def test_json_with_prose_around_it_is_still_read(self):
        text = 'Here is my review: {"action":"review","met":true,"missing":[],"why":"ok"} Thanks.'
        self.assertEqual(rv.judge(text), "accept")

    def test_judge_does_not_change_the_dict_it_is_given(self):
        obj = {"action": "review", "met": True, "missing": [], "auto_fixes": ["x"]}
        rv.judge(obj)
        self.assertIn("auto_fixes", obj)

    def test_the_reason_is_read_back_for_the_report(self):
        _verdict, parsed = rv._read(_reply({"action": "review", "met": False,
                                            "missing": ["a"], "why": "no answer for 37"}))
        self.assertEqual(parsed["why"], "no answer for 37")


class WilsonTests(unittest.TestCase):

    def test_wilson_interval_for_no_successes_in_ten_matches_the_known_value(self):
        low, high = rv.wilson(0, 10)
        self.assertAlmostEqual(low, 0.0, places=4)
        self.assertAlmostEqual(high, 0.2775, places=3)

    def test_wilson_interval_for_half_in_ten_matches_the_known_value(self):
        low, high = rv.wilson(5, 10)
        self.assertAlmostEqual(low, 0.2366, places=3)
        self.assertAlmostEqual(high, 0.7634, places=3)

    def test_wilson_interval_for_two_in_five_matches_the_known_value(self):
        low, high = rv.wilson(2, 5)
        self.assertAlmostEqual(low, 0.1176, places=3)
        self.assertAlmostEqual(high, 0.7690, places=3)

    def test_wilson_interval_is_undefined_without_trials(self):
        self.assertIsNone(rv.wilson(0, 0))


class ScoreTests(unittest.TestCase):

    def _records(self):
        return [
            _record("g1", "good", "accept"), _record("g2", "good", "accept"),
            _record("g3", "good", "accept"), _record("g4", "good", "reject",
                                                    trap="plain", why="wanted a table"),
            _record("g5", "good", "unreadable"),
            _record("i1", "incomplete", "accept", trap="helper", why="looks complete"),
            _record("i2", "incomplete", "accept", trap="hard part"),
            _record("i3", "incomplete", "reject", trap="helper", why="no total"),
            _record("i4", "incomplete", "reject", trap="hard part"),
            _record("i5", "incomplete", "reject"),
            _record("i6", "incomplete", "unreadable"),
        ]

    def test_score_counts_the_confusion_matrix(self):
        s = rv.score(self._records())
        self.assertEqual(s["confusion"], {"good": {"accept": 3, "reject": 1},
                                          "incomplete": {"accept": 2, "reject": 3}})

    def test_false_acceptance_and_rejection_rates_are_exact(self):
        s = rv.score(self._records())
        self.assertEqual((s["false_acceptance"]["count"], s["false_acceptance"]["of"]), (2, 5))
        self.assertAlmostEqual(s["false_acceptance"]["rate"], 0.4)
        self.assertEqual((s["false_rejection"]["count"], s["false_rejection"]["of"]), (1, 4))
        self.assertAlmostEqual(s["false_rejection"]["rate"], 0.25)
        low, high = s["false_acceptance"]["interval"]
        self.assertAlmostEqual(low, 0.1176, places=3)
        self.assertAlmostEqual(high, 0.7690, places=3)

    def test_unreadable_replies_are_counted_apart_and_not_judged(self):
        s = rv.score(self._records())
        self.assertEqual(s["unreadable"], 2)
        self.assertEqual(s["unreadable_ids"], ["g5", "i6"])
        self.assertEqual(s["false_rejection"]["of"], 4)
        self.assertEqual(s["false_acceptance"]["of"], 5)

    def test_no_judged_case_gives_no_rate_rather_than_a_division_error(self):
        s = rv.score([_record("g1", "good", "unreadable")])
        self.assertIsNone(s["false_rejection"]["rate"])
        self.assertIsNone(s["false_rejection"]["interval"])

    def test_per_trap_breakdown_counts_judged_and_misjudged(self):
        s = rv.score(self._records())
        self.assertEqual(s["traps"]["helper"], {"judged": 2, "misjudged": 1})
        self.assertEqual(s["traps"]["hard part"], {"judged": 2, "misjudged": 1})
        self.assertEqual(s["traps"]["plain"], {"judged": 1, "misjudged": 1})
        self.assertEqual(s["traps"][rv.NO_TRAP], {"judged": 4, "misjudged": 0})

    def test_agreement_across_repeats_finds_the_case_that_flipped(self):
        records = [_record("a", "good", "accept", repeat=0), _record("a", "good", "reject", repeat=1),
                   _record("b", "incomplete", "reject", repeat=0),
                   _record("b", "incomplete", "reject", repeat=1)]
        agreement = rv.score(records)["agreement"]
        self.assertEqual(agreement["repeated"], 2)
        self.assertEqual(agreement["consistent"], 1)
        self.assertEqual(agreement["flipping"], ["a"])
        self.assertAlmostEqual(agreement["rate"], 0.5)

    def test_misjudged_cases_carry_the_reviewers_reason(self):
        s = rv.score(self._records())
        ids = [(m["id"], m["why"]) for m in s["misjudged"]]
        self.assertIn(("i1", "looks complete"), ids)
        self.assertIn(("g4", "wanted a table"), ids)
        self.assertNotIn(("i3", "no total"), ids, "a correct rejection is not misjudged")

    def test_score_accepts_the_envelope_that_run_returns(self):
        envelope = {"results": self._records()}
        self.assertEqual(rv.score(envelope)["confusion"], rv.score(self._records())["confusion"])

    def test_table_names_the_rates_intervals_and_misjudged_ids(self):
        text = rv.table(rv.score(self._records()))
        self.assertIn("False acceptance (an incomplete build was passed): 2 of 5 = 40.0%", text)
        self.assertIn("95% Wilson interval 11.8% to 76.9%", text)
        self.assertIn("False rejection (a good build was failed): 1 of 4 = 25.0%", text)
        self.assertIn("i1 (incomplete, accept, repeat 0): looks complete", text)
        self.assertIn("g4 (good, reject, repeat 0): wanted a table", text)

    def test_table_says_when_no_case_was_repeated(self):
        self.assertIn("no case was asked more than once", rv.table(rv.score(self._records())))


class RunTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rv-run-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_asks_once_per_case_per_repeat(self):
        cases = [_case("a"), _case("b", label="incomplete")]
        calls = []

        def counting(system, user):
            calls.append(user)
            return rv.scripted_generate(system, user)

        data = rv.run(cases, counting, repeats=3)
        self.assertEqual(len(calls), 6)
        self.assertEqual(len(data["results"]), 6)
        self.assertTrue(data["complete"])
        self.assertEqual(sorted((r["id"], r["repeat"]) for r in data["results"]),
                         sorted((c, r) for c in ("a", "b") for r in range(3)))

    def test_the_generator_receives_the_reviewers_system_prompt_and_the_built_message(self):
        seen = []

        def record(system, user):
            seen.append((system, user))
            return rv.scripted_generate(system, user)

        rv.run([_case()], record)
        self.assertEqual(seen[0][0], ag.GOAL_REVIEW_SYSTEM)
        self.assertEqual(seen[0][1], rv.build_message(_case()))

    def test_results_file_holds_each_case_when_its_progress_line_prints(self):
        out = self.tmp / "results.json"
        cases = [_case("a"), _case("b")]
        seen = []

        def progress(line):
            seen.append(len(json.loads(out.read_text(encoding="utf-8"))["results"]))

        rv.run(cases, rv.scripted_generate, out=out, progress=progress)
        self.assertEqual(seen, [1, 2])

    def test_resume_skips_finished_cases_and_makes_no_call_for_them(self):
        out = self.tmp / "results.json"
        cases = [_case("a"), _case("b", label="incomplete")]
        rv.run(cases, rv.scripted_generate, out=out)
        calls = []

        def counting(system, user):
            calls.append(1)
            return rv.scripted_generate(system, user)

        again = rv.run(cases, counting, out=out)
        self.assertEqual(calls, [])
        self.assertEqual(len(again["results"]), 2)

    def test_resume_asks_only_the_new_repeat(self):
        out = self.tmp / "results.json"
        cases = [_case("a")]
        rv.run(cases, rv.scripted_generate, out=out, repeats=1)
        calls = []

        def counting(system, user):
            calls.append(1)
            return rv.scripted_generate(system, user)

        data = rv.run(cases, counting, out=out, repeats=2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(r["repeat"] for r in data["results"]), [0, 1])

    def test_spend_cap_stops_the_run_before_the_next_call(self):
        cases = rv.load_cases(CASES)[:4]
        worst = rv.worst_call_usd(rv.review_system(), rv.build_message(cases[0]))
        calls = []

        def paid(system, user):
            calls.append(1)
            reply = rv.scripted_generate(system, user)
            reply["cost_usd"] = 0.01
            return reply

        data = rv.run(cases, paid, max_usd=0.02 + worst / 2, out=self.tmp / "results.json")
        self.assertEqual(len(calls), 2, "the third call would pass the cap, so it is not made")
        self.assertEqual(len(data["results"]), 2)
        self.assertIn("spend cap", data["stopped"])
        self.assertFalse(data["complete"])
        self.assertAlmostEqual(data["spent_usd"], 0.02)
        self.assertIn("spend cap", json.loads((self.tmp / "results.json").read_text())["stopped"])

    def test_a_cap_below_one_call_makes_no_call_at_all(self):
        calls = []

        def counting(system, user):
            calls.append(1)
            return rv.scripted_generate(system, user)

        data = rv.run([_case()], counting, max_usd=0.0)
        self.assertEqual(calls, [])
        self.assertEqual(data["results"], [])
        self.assertIn("spend cap", data["stopped"])

    def test_a_generator_error_is_recorded_as_unreadable_and_the_run_goes_on(self):
        calls = []

        def flaky(system, user):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("network down")
            return rv.scripted_generate(system, user)

        data = rv.run([_case("a"), _case("b")], flaky)
        self.assertEqual(data["results"][0]["verdict"], "unreadable")
        self.assertIn("network down", data["results"][0]["error"])
        self.assertEqual(data["results"][1]["verdict"], "reject")
        self.assertEqual(len(data["results"]), 2)


class CommandLineTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rv-cli-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rv.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_exits_zero_and_prints_the_mode_that_ran(self):
        code, out, _ = self._run_main(["--dry-run", "--out", str(self.tmp / "dry")])
        self.assertEqual(code, 0)
        self.assertIn("MODE THAT RAN: dry-run", out)
        self.assertIn("GOAL REVIEWER EVALUATION", out)
        self.assertTrue((self.tmp / "dry" / "results.json").exists())

    def test_dry_run_never_calls_the_live_generator(self):
        with mock.patch.object(ag, "live_generate", _boom), \
                mock.patch.object(ag, "live_status", _boom):
            code, _, _ = self._run_main(["--dry-run", "--out", str(self.tmp / "dry")])
        self.assertEqual(code, 0)

    def test_dry_run_never_writes_under_artifacts(self):
        artifacts = ROOT / "artifacts"
        before = _snapshot(artifacts)
        self._run_main(["--dry-run", "--out", str(self.tmp / "dry")])
        self.assertEqual(before, _snapshot(artifacts))

    def test_the_scripted_reviewer_shows_both_kinds_of_error(self):
        code, out, _ = self._run_main(["--dry-run", "--out", str(self.tmp / "dry")])
        data = json.loads((self.tmp / "dry" / "results.json").read_text(encoding="utf-8"))
        s = rv.score(data)
        self.assertGreater(s["false_acceptance"]["count"], 0)
        self.assertGreater(s["false_rejection"]["count"], 0)

    def test_live_without_max_usd_is_refused_before_anything_runs(self):
        with mock.patch.object(ag, "live_generate", _boom), \
                mock.patch.object(ag, "live_status", _boom):
            code, _, err = self._run_main(["--live"])
        self.assertEqual(code, 2)
        self.assertIn("--max-usd", err)

    def test_live_with_a_zero_cap_is_refused_before_anything_runs(self):
        with mock.patch.object(ag, "live_generate", _boom), \
                mock.patch.object(ag, "live_status", _boom):
            code, _, err = self._run_main(["--live", "--max-usd", "0"])
        self.assertEqual(code, 2)
        self.assertIn("--max-usd", err)

    def test_live_and_dry_run_together_are_refused(self):
        code, _, err = self._run_main(["--live", "--dry-run", "--max-usd", "1"])
        self.assertEqual(code, 2)
        self.assertIn("exactly one", err)

    def test_choosing_neither_mode_is_refused(self):
        code, _, err = self._run_main([])
        self.assertEqual(code, 2)
        self.assertIn("exactly one", err)

    def test_a_bad_case_file_is_refused_with_a_message(self):
        bad = self.tmp / "bad.json"
        bad.write_text("[]", encoding="utf-8")
        code, _, err = self._run_main(["--dry-run", "--cases", str(bad)])
        self.assertEqual(code, 2)
        self.assertIn("cannot load cases", err)

    def test_zero_repeats_is_refused(self):
        code, _, err = self._run_main(["--dry-run", "--repeats", "0"])
        self.assertEqual(code, 2)
        self.assertIn("--repeats", err)


if __name__ == "__main__":
    unittest.main()
