"""Tests for app_economics: specs, Latin-square orders, plan and estimate, statistics,
verdict branches, resume and spend cap, and the command line.

Nothing here calls a paid model. Runs use autonomy_experiment's scripted generator, and
the entry point is checked to refuse a live run without --live and --max-usd.
"""

import contextlib
import importlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import app_economics as ae  # noqa: E402
import autonomy_experiment as aexp  # noqa: E402

BANNED_WORDS = re.compile(r"\b(blog|todo|task|product|customer|crm|login|user)s?\b", re.I)


def _snapshot(root):
    """Every file under ROOT with its mtime and size, to prove a run wrote nothing there."""
    out = {}
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file():
                st = path.stat()
                out[str(path)] = (st.st_mtime_ns, st.st_size)
    return out


def _requirement_units(text):
    """Top-level lines: each is one requirement, and a scenario block counts once."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.startswith(" ")]


def synthetic(warm_cost, cold_cost, warm_ok=None, cold_ok=None, order_rows=None):
    """Results in the shape run() writes, with costs given by functions of (order, position).
    An ok function returns True when that build meets all five requirement units."""
    rows = order_rows or ae.orders()
    cells = []
    for o, row in enumerate(rows):
        for p, sid in enumerate(row, 1):
            for cond, cost_fn, ok_fn in (("warm", warm_cost, warm_ok),
                                         ("cold", cold_cost, cold_ok)):
                ok = True if ok_fn is None else ok_fn(o, p)
                cells.append({
                    "order": o, "position": p, "spec": sid, "condition": cond,
                    "record": {"totals": {"cost_usd": cost_fn(o, p), "model_calls": 4,
                                          "tokens": 100, "seconds": 2.0},
                               "requirements": {"met": 5 if ok else 2, "total": 5}}})
    return {"cells": cells, "orders": rows, "specs": ae_ids()}


def ae_ids():
    return [s["id"] for s in ae.SPECS]


def flat(value):
    return lambda o, p: value


def falling(o, p):
    return 1.0 - 0.1 * p


def rising(o, p):
    return 1.0 + 0.1 * p


def steep(o, p):
    return 1.0 - 0.2 * p


class SpecTests(unittest.TestCase):

    def test_five_specs_each_with_three_prompts_and_five_requirement_units(self):
        self.assertEqual(len(ae.SPECS), 5)
        self.assertEqual(len({s["id"] for s in ae.SPECS}), 5)
        for s in ae.SPECS:
            self.assertEqual(len(s["prompts"]), 3, s["id"])
            self.assertEqual(len(_requirement_units(s["requirements"])), 5, s["id"])
            self.assertEqual(s["kind"], "cli", s["id"])

    def test_every_spec_has_the_same_number_of_typed_commands(self):
        counts = {s["id"]: len(re.findall(r'run "', s["requirements"])) for s in ae.SPECS}
        self.assertEqual(len(set(counts.values())), 1, counts)

    def test_requirements_parse_with_the_module_and_yield_no_errors(self):
        req = importlib.import_module("requirements")
        for s in ae.SPECS:
            reqs, errors = req.parse(s["requirements"])
            self.assertEqual(errors, [], s["id"])
            self.assertEqual(len(reqs), 5, s["id"])

    def test_every_requirement_line_uses_only_the_run_grammar(self):
        shape = re.compile(r'^(scenario: \S.*|run "[^"]*"( then run "[^"]*")?'
                           r'( (prints|does not print) "[^"]*")?)$')
        for s in ae.SPECS:
            for line in s["requirements"].splitlines():
                self.assertTrue(shape.match(line.strip()), "%s: %r" % (s["id"], line))

    def test_expected_text_is_stated_in_a_prompt_or_typed_in_a_command(self):
        for s in ae.SPECS:
            prompts = " ".join(s["prompts"])
            typed = re.findall(r'run "([^"]*)"', s["requirements"])
            for line in s["requirements"].splitlines():
                expected = re.findall(r'"([^"]*)"', line)
                if not expected or "prints" not in line and "does not print" not in line:
                    continue
                text = expected[-1]
                self.assertTrue(text in prompts or any(text in t for t in typed),
                                "%s: %r is neither stated nor typed" % (s["id"], text))

    def test_no_banned_words_in_specs(self):
        for s in ae.SPECS:
            text = " ".join([s["title"]] + s["prompts"] + [s["requirements"]])
            self.assertIsNone(BANNED_WORDS.search(text), s["id"])


class OrderTests(unittest.TestCase):

    def test_latin_square_puts_each_spec_once_at_each_position(self):
        rows = ae.orders()
        self.assertEqual(len(rows), 5)
        for p in range(5):
            self.assertEqual(sorted(r[p] for r in rows), sorted(ae_ids()))
        for row in rows:
            self.assertEqual(sorted(row), sorted(ae_ids()))

    def test_ten_orders_are_two_balanced_squares_one_forward_one_reversed(self):
        rows = ae.orders(count=10)
        self.assertEqual(len(rows), 10)
        for block in (rows[:5], rows[5:]):
            for p in range(5):
                self.assertEqual(sorted(r[p] for r in block), sorted(ae_ids()))
        self.assertNotEqual(rows[:5], rows[5:])

    def test_order_count_must_be_a_positive_multiple_of_five(self):
        for bad in (3, 7, 0, 4):
            with self.assertRaises(ValueError, msg=str(bad)):
                ae.orders(count=bad)

    def test_plan_has_one_cell_per_order_position_and_condition(self):
        cells = ae.plan(ae.SPECS, ae.orders())
        self.assertEqual(len(cells), 5 * 5 * 2)
        keys = {(c["order"], c["position"], c["condition"]) for c in cells}
        self.assertEqual(len(keys), 50)
        for o in range(5):
            for p in range(1, 6):
                self.assertEqual({c["condition"] for c in cells
                                  if c["order"] == o and c["position"] == p},
                                 {"warm", "cold"})

    def test_plan_refuses_a_row_that_is_not_a_permutation_of_the_specs(self):
        bad = [[ae_ids()[0]] * 5]
        with self.assertRaises(ValueError):
            ae.plan(ae.SPECS, bad)

    def test_estimate_counts_builds_and_prompt_sessions_and_prices_them(self):
        cells = ae.plan(ae.SPECS, ae.orders())
        est = ae.estimate(ae.SPECS, cells)
        self.assertEqual((est["cells"], est["app_builds"]), (50, 50))
        self.assertEqual(est["prompt_sessions"], 150)
        self.assertEqual(est["typical_usd"], round(150 * ae.ae.TYPICAL_BUILD_USD, 2))
        self.assertEqual(est["worst_case_usd"], round(150 * ae.ae.WORST_BUILD_USD, 2))


class StatisticsTests(unittest.TestCase):

    def test_ols_slope_recovers_the_slope_of_known_data(self):
        self.assertAlmostEqual(ae.ols_slope([1, 2, 3, 4, 5], [3, 5, 7, 9, 11]), 2.0)
        self.assertAlmostEqual(ae.ols_slope([1, 2, 3, 4, 5], [4, 4, 4, 4, 4]), 0.0)

    def test_ols_slope_is_undefined_without_spread_in_x(self):
        self.assertIsNone(ae.ols_slope([2, 2, 2], [1, 2, 3]))
        self.assertIsNone(ae.ols_slope([1], [1]))

    def test_bootstrap_is_deterministic_for_a_seed(self):
        rows = [[(p, 1.0 + ((o * 7 + p * 3) % 5) / 10.0) for p in range(1, 6)]
                for o in range(5)]
        first = ae.bootstrap_slope(rows, seed=11, reps=300)
        second = ae.bootstrap_slope(rows, seed=11, reps=300)
        self.assertEqual(first, second)
        self.assertLessEqual(first["low"], first["high"])

    def test_bootstrap_interval_collapses_when_every_order_is_identical(self):
        rows = [[(p, 1.0 - 0.1 * p) for p in range(1, 6)] for _ in range(5)]
        out = ae.bootstrap_slope(rows, seed=3, reps=200)
        self.assertAlmostEqual(out["slope"], -0.1)
        self.assertAlmostEqual(out["low"], -0.1)
        self.assertAlmostEqual(out["high"], -0.1)

    def test_difference_is_warm_minus_cold_at_each_position(self):
        warm = [{p: 1.0 - 0.1 * p for p in range(1, 6)} for _ in range(5)]
        cold = [{p: 1.0 for p in range(1, 6)} for _ in range(5)]
        out = ae.bootstrap_difference(warm, cold, list(range(1, 6)), seed=1, reps=50)
        self.assertAlmostEqual(out["by_position"][0]["point"], -0.1)
        self.assertAlmostEqual(out["by_position"][-1]["point"], -0.5)
        self.assertAlmostEqual(out["overall"]["point"], -0.3)

    def test_cost_per_successful_build_divides_all_cost_by_the_successes(self):
        ok = lambda o, p: (o + p) % 5 == 0   # 5 of the 25 warm builds succeed
        result = synthetic(falling, flat(1.0), warm_ok=ok)
        analysis = ae.analyse(result, reps=20)
        warm = analysis["overall"]["warm"]
        self.assertEqual(warm["successes"], 5)
        self.assertAlmostEqual(warm["cost_per_success"], warm["total_cost"] / 5)

    def test_partial_orders_are_left_out_of_the_inference(self):
        result = synthetic(falling, flat(1.0), order_rows=ae.orders())
        result["cells"] = [c for c in result["cells"]
                           if not (c["order"] == 2 and c["position"] == 3
                                   and c["condition"] == "cold")]
        analysis = ae.analyse(result, reps=20)
        self.assertEqual(analysis["orders"]["complete"], 4)
        self.assertNotIn(2, analysis["orders"]["complete_ids"])


class VerdictTests(unittest.TestCase):

    def test_falling_warm_cost_with_equal_success_is_reported_as_falling(self):
        analysis = ae.analyse(synthetic(falling, flat(1.0)), reps=100)
        self.assertEqual(analysis["verdict"], ae.FALLS)
        self.assertIn("warm slope interval entirely below zero", analysis["rule"])

    def test_flat_costs_give_no_evidence_that_cost_falls(self):
        analysis = ae.analyse(synthetic(flat(1.0), flat(1.0)), reps=100)
        self.assertEqual(analysis["verdict"], ae.NO_EVIDENCE)

    def test_rising_warm_cost_is_reported_as_rising(self):
        analysis = ae.analyse(synthetic(rising, flat(1.0)), reps=100)
        self.assertEqual(analysis["verdict"], ae.RISES)

    def test_falling_cost_with_collapsed_success_is_not_reported_as_falling(self):
        analysis = ae.analyse(synthetic(falling, flat(1.0), warm_ok=lambda o, p: False),
                              reps=100)
        self.assertEqual(analysis["verdict"], ae.NO_EVIDENCE)
        self.assertIn("lower than cold success by more than 10 points", analysis["rule"])

    def test_falling_warm_slope_but_cold_falls_faster_is_not_reported_as_falling(self):
        analysis = ae.analyse(synthetic(falling, steep), reps=100)
        self.assertEqual(analysis["verdict"], ae.NO_EVIDENCE)
        self.assertIn("last position is not entirely below zero", analysis["rule"])

    def test_fewer_than_two_complete_orders_give_no_evidence(self):
        rows = ae.orders()[:1]
        result = synthetic(falling, flat(1.0), order_rows=rows)
        analysis = ae.analyse(result, reps=20)
        self.assertEqual(analysis["verdict"], ae.NO_EVIDENCE)
        self.assertIn("fewer than two complete orders", analysis["rule"])

    def test_table_states_the_verdict_the_rule_and_the_intervals(self):
        analysis = ae.analyse(synthetic(falling, flat(1.0)), reps=100)
        text = ae.table(analysis)
        self.assertIn("VERDICT: cost falls with experience", text)
        self.assertIn("Rule. Cost is the cost of one app build", text)
        self.assertIn("Slope of cost against position", text)
        self.assertIn("Warm minus cold, USD per build", text)
        self.assertIn("USD/success", text)


def _two_spec_setup():
    """A small plan (two specs, two orders, eight cells) for runner tests."""
    specs = ae.SPECS[:2]
    rows = ae.orders([s["id"] for s in specs])
    return specs, rows


def _costly(system, user):
    reply = aexp.scripted_generate(system, user)
    reply["cost_usd"] = 0.5
    return reply


class RunnerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="app-econ-run-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resume_skips_finished_cells_and_makes_no_model_call(self):
        specs, rows = _two_spec_setup()
        out = self.tmp / "out"
        ae.run(specs, rows, aexp.scripted_generate, out, progress=lambda line: None)
        calls = []

        def counting(system, user):
            calls.append(1)
            return aexp.scripted_generate(system, user)

        lines = []
        again = ae.run(specs, rows, counting, out, progress=lines.append)
        self.assertEqual(calls, [], "no cell is run again")
        self.assertEqual(lines, [])
        self.assertTrue(again["complete"])
        self.assertEqual(len(again["cells"]), 8)

    def test_results_hold_each_cell_when_its_progress_line_prints(self):
        specs, rows = _two_spec_setup()
        out = self.tmp / "out"
        seen = []

        def progress(line):
            data = json.loads((out / "results.json").read_text(encoding="utf-8"))
            seen.append(len(data["cells"]))

        ae.run(specs, rows, aexp.scripted_generate, out, progress=progress)
        self.assertEqual(seen, list(range(1, 9)))

    def test_progress_lines_are_also_appended_to_progress_log_with_elapsed_and_eta(self):
        specs, rows = _two_spec_setup()
        out = self.tmp / "out"
        ae.run(specs, rows, aexp.scripted_generate, out, progress=lambda line: None)
        log = (out / "progress.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(log), 8)
        self.assertTrue(all("elapsed" in ln and "ETA" in ln for ln in log))

    def test_spend_cap_stops_the_run_cleanly_and_a_resume_finishes_it(self):
        specs, rows = _two_spec_setup()
        out = self.tmp / "out"
        lines = []
        first = ae.run(specs, rows, _costly, out, max_usd=1.5, progress=lines.append)
        self.assertIn("spend cap", first["stopped"])
        self.assertFalse(first["complete"])
        self.assertGreaterEqual(len(first["cells"]), 1)
        self.assertLess(len(first["cells"]), 8)
        self.assertTrue(any(ln.startswith("STOP") for ln in lines))
        kept = json.dumps(first["cells"][0], sort_keys=True)
        second = ae.run(specs, rows, aexp.scripted_generate, out, progress=lambda line: None)
        self.assertTrue(second["complete"])
        self.assertEqual(len(second["cells"]), 8)
        self.assertEqual(json.dumps(second["cells"][0], sort_keys=True), kept)

    def test_spend_cap_below_one_cell_runs_nothing(self):
        specs, rows = _two_spec_setup()
        calls = []

        def counting(system, user):
            calls.append(1)
            return _costly(system, user)

        result = ae.run(specs, rows, counting, self.tmp / "out", max_usd=0.3,
                        progress=lambda line: None)
        self.assertEqual(result["cells"], [])
        self.assertEqual(calls, [])

    def test_resume_refuses_a_different_plan(self):
        specs, rows = _two_spec_setup()
        out = self.tmp / "out"
        ae.run(specs, rows, aexp.scripted_generate, out, progress=lambda line: None)
        with self.assertRaises(ValueError):
            ae.run(specs, ae.orders([s["id"] for s in specs], count=4), aexp.scripted_generate,
                   out, progress=lambda line: None)

    def test_warm_store_is_shared_within_an_order_and_empty_at_its_first_position(self):
        specs, rows = _two_spec_setup()
        out = self.tmp / "out"
        store_path = out / "lessons" / "order-00.json"
        store_path.parent.mkdir(parents=True)
        store_path.write_text('{"stale": 3}', encoding="utf-8")
        seen = []
        original = aexp.run_cell

        def spy(spec, arm, generate, workdir, **kwargs):
            lessons = kwargs.get("lessons")
            seen.append((workdir.name, lessons, lessons is not None and lessons.path.exists()))
            return original(spec, arm, generate, workdir, **kwargs)

        with mock.patch.object(aexp, "run_cell", side_effect=spy):
            ae.run(specs, rows, aexp.scripted_generate, out, progress=lambda line: None)
        first_warm = [x for x in seen if x[0].startswith("o00-p1-warm")][0]
        self.assertIsNotNone(first_warm[1])
        self.assertFalse(first_warm[2], "the stale store of the order was removed first")
        warm_o0 = [x[1] for x in seen if x[0].startswith("o00-") and x[0].endswith("warm")]
        self.assertEqual(len(warm_o0), 2)
        self.assertEqual({str(s.path) for s in warm_o0}, {str(store_path)})
        self.assertTrue(all(x[1] is None for x in seen if x[0].endswith("cold")))


class DryRunTests(unittest.TestCase):
    """One dry run of the whole plan, shared by the tests below (it takes about half a minute)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="app-econ-dry-"))
        cls.out = cls.tmp / "dry"
        cls.before = _snapshot(ROOT / "artifacts")

        def boom(*args, **kwargs):
            raise AssertionError("the live generator must not be called in a dry run")

        buf = io.StringIO()
        with mock.patch.object(ag, "live_generate", boom), \
                mock.patch.object(ag, "live_status", boom), \
                contextlib.redirect_stdout(buf):
            cls.code = ae.main(["--dry-run", "--out", str(cls.out)])
        cls.stdout = buf.getvalue()
        cls.after = _snapshot(ROOT / "artifacts")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_dry_run_exits_zero_and_never_calls_the_live_generator(self):
        self.assertEqual(self.code, 0)
        self.assertTrue((self.out / "results.json").exists())

    def test_dry_run_writes_nothing_under_artifacts(self):
        self.assertEqual(self.before, self.after)

    def test_dry_run_report_states_a_verdict(self):
        self.assertIn("VERDICT:", self.stdout)
        self.assertIn("Slope of cost against position", self.stdout)


class CommandLineTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="app-econ-cli-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ae.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_live_without_max_usd_is_refused_and_nothing_runs(self):
        def boom(*args, **kwargs):
            raise AssertionError("nothing may run when the live flags are incomplete")

        with mock.patch.object(ag, "live_generate", boom), \
                mock.patch.object(ag, "live_status", boom):
            code, _, err = self._main(["--live"])
        self.assertEqual(code, 2)
        self.assertIn("--max-usd", err)

    def test_live_with_a_non_positive_cap_is_refused(self):
        code, _, err = self._main(["--live", "--max-usd", "0"])
        self.assertEqual(code, 2)
        self.assertIn("--max-usd", err)

    def test_live_combined_with_dry_run_is_refused(self):
        code, _, err = self._main(["--live", "--dry-run", "--max-usd", "5"])
        self.assertEqual(code, 2)
        self.assertIn("choose one", err)

    def test_no_mode_is_refused(self):
        code, _, err = self._main([])
        self.assertEqual(code, 2)
        self.assertIn("nothing to do", err)

    def test_an_order_count_that_is_not_a_multiple_of_five_is_refused(self):
        code, _, err = self._main(["--estimate", "--orders", "7"])
        self.assertEqual(code, 2)
        self.assertIn("multiple of 5", err)

    def test_estimate_prints_the_plan_and_both_figures(self):
        code, out, _ = self._main(["--estimate"])
        self.assertEqual(code, 0)
        self.assertIn("50 cells (app builds), 150 prompt sessions", out)
        self.assertIn("typical $%.2f" % (150 * ae.ae.TYPICAL_BUILD_USD), out)
        self.assertIn("worst case $%.2f" % (150 * ae.ae.WORST_BUILD_USD), out)

    def test_analyse_reprints_the_report_from_saved_results_without_running(self):
        saved = self.tmp / "saved"
        saved.mkdir()
        data = synthetic(lambda o, p: 1.0 - 0.1 * p, lambda o, p: 1.0)
        data.update({"schema": 1, "arm": ae.ARM_ID, "mode": "demo", "max_usd": None,
                     "limits": {}, "estimate": {}, "spent_usd": 0.0, "stopped": None,
                     "complete": True})
        (saved / "results.json").write_text(json.dumps(data), encoding="utf-8")
        with mock.patch.object(ae, "run") as run_mock:
            code, out, _ = self._main(["--analyse", str(saved)])
        self.assertEqual(code, 0)
        run_mock.assert_not_called()
        self.assertIn("VERDICT: cost falls with experience", out)


if __name__ == "__main__":
    unittest.main()
