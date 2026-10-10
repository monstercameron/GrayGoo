"""Specialization report: mechanism figures, period splitting, scope filters and output."""
import contextlib
import io
import json
import statistics
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import economics as ec  # noqa: E402
import specialization as sp  # noqa: E402

IN_PRICE = 0.99e-6   # USD per input token, the price the fixture replies are priced at
OUT_PRICE = 1.49e-6  # USD per output token
REPORT_KEYS = {"mode", "project", "builds", "functions_saved", "spent_usd", "model_calls",
               "seconds", "window", "periods", "app_periods", "mechanisms", "saved", "notes"}
MECHANISM_KEYS = {"id", "label", "events", "saved_calls", "saved_usd", "saved_seconds",
                  "basis", "how"}
MECHANISM_IDS = ["auto-fix", "compaction", "edits", "warm-repl", "kit", "reuse", "free-checks"]
LABELS = ["Replies repaired without a model call",
          "Prompt text left out by context compaction",
          "Repairs sent as small edits instead of whole functions",
          "Lisp checks answered by the warm process",
          "Helper functions supplied instead of built",
          "Prompts answered by functions that already existed",
          "Faults found by checks that cost no model call"]


def cost(tokens_in, tokens_out):
    return tokens_in * IN_PRICE + tokens_out * OUT_PRICE


def goal(t, project="alpha", mode="live", arm="main"):
    return {"kind": "goal", "t": t, "prompt": "a prompt", "mode": mode, "arm": arm,
            "project": project}


def call(t, label, context=None):
    event = {"kind": "model_call", "t": t, "label": label}
    if context is not None:
        event["context"] = context
    return event


def reply(t, tokens_in, tokens_out, latency_ms, model="qwen-3.8-27b"):
    return {"kind": "model_reply", "t": t, "model": model, "text": "{}",
            "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": cost(tokens_in, tokens_out), "latency_ms": latency_ms,
            "estimated": False}


def event(kind, t, **fields):
    out = {"kind": kind, "t": t}
    out.update(fields)
    return out


def write_log(directory, name, lines):
    """Write one JSONL file: dicts become JSON lines, strings are written as they are."""
    with open(Path(directory) / name, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write((json.dumps(line) if isinstance(line, dict) else line) + "\n")


def write_main_fixture(directory):
    """Five live main builds, one demo and one nomem build, and two unreadable files.

    Build s1 (alpha, build): plan, a step with compaction, a repair; two auto-fixes
    (one empty); one promoted function f1; one small edit; a cold repl check (no
    flag) and a warm one; a failing smoke check.
    Build s2 (alpha, cache): one quick reuse; a malformed line in the middle.
    Build s3 (alpha, plan): plan with compaction, a repair, two promoted functions
    f2 and f3 (first verdicts True and False), one auto-fix, a kit seed, an acceptance
    run with one failure, a warm check.
    Build s4 (alpha, build): one final call, a kit seed, an interface problem, a cold
    check (warm false). Build s5 (beta, build, no summary): one step, a promoted g1,
    a kit seed, a style problem; flow comes from its decision event.
    """
    write_log(directory, "s1.jsonl", [
        goal(1000),
        call(1001, "plan"),
        reply(1002, 1000, 100, 2000.0),
        call(1003, "step", context={"chars": 1000, "saved": 400, "level": 1}),
        reply(1004, 2000, 200, 4000.0),
        event("verdict", 1005, ok=False, lane="f1"),
        call(1006, "repair", context={"chars": 500, "saved": 0, "level": 0}),
        reply(1007, 500, 50, 1000.0),
        event("verdict", 1008, ok=True, lane="f1"),
        event("auto_fix", 1009, label="step", fixes=["added-docstring", "nested-state-data"],
              dropped=[]),
        event("auto_fix", 1010, label="step", fixes=[], dropped=[]),
        event("promoted", 1011, name="f1", tools=["f1"], lane="f1"),
        event("edit", 1012, name="f1", edits=1, saved_chars=4000, lane="f1"),
        event("repl", 1013, label="rehearse", ok=True, elapsed_ms=80.0),
        event("repl", 1014, label="rehearse", ok=True, elapsed_ms=2.0, warm=True),
        event("smoke", 1015, call="GET /", ok=False, status=500, error="boom"),
        event("summary", 1016, outcome="success", flow="build", seconds=100.0),
        event("done", 1017, state="done"),
    ])
    write_log(directory, "s2.jsonl", [
        goal(1100),
        call(1101, "quick-reuse"),
        reply(1102, 100, 10, 500.0),
        "{this line is not json",
        event("summary", 1104, outcome="success", flow="cache", seconds=10.0),
        event("done", 1105, state="done"),
    ])
    write_log(directory, "s3.jsonl", [
        goal(1200),
        call(1201, "plan", context={"chars": 2000, "saved": 1000, "level": 1}),
        reply(1202, 3000, 300, 3000.0),
        call(1203, "repair"),
        reply(1204, 1000, 100, 2000.0),
        event("verdict", 1205, ok=True, lane="f2"),
        event("verdict", 1206, ok=False, lane="f3"),
        event("promoted", 1207, name="f2", tools=["f2"], lane="f2"),
        event("promoted", 1208, name="f3", tools=["f3"], lane="f3"),
        event("auto_fix", 1209, label="plan", fixes=["added-docstring"], dropped=[]),
        event("kit_seeded", 1210, tools=["pair-value", "table-rows"]),
        event("acceptance", 1211, failed=1, passed=3),
        event("repl", 1212, label="rehearse", ok=True, elapsed_ms=4.0, warm=True),
        event("summary", 1213, outcome="success", flow="plan", seconds=200.0),
        event("done", 1214, state="done"),
    ])
    write_log(directory, "s4.jsonl", [
        goal(1300),
        call(1301, "final"),
        reply(1302, 1500, 150, 1500.0),
        event("kit_seeded", 1303, tools=["table-rows", "html-page"]),
        event("interface_check", 1304, problems=["unused variable"]),
        event("repl", 1305, label="rehearse", ok=True, elapsed_ms=100.0, warm=False),
        event("summary", 1306, outcome="success", flow="build", seconds=50.0),
        event("done", 1307, state="done"),
    ])
    write_log(directory, "s5.jsonl", [
        goal(1400, project="beta"),
        event("decision", 1401, action="build", plan={}),
        call(1402, "step"),
        reply(1403, 1000, 120, 1000.0),
        event("promoted", 1404, name="g1", tools=["g1"]),
        event("kit_seeded", 1405, tools=["pair-value"]),
        event("style_check", 1406, undefined=["foo"]),
        event("done", 1410, state="done"),
    ])
    # Excluded: a demo build with a failing smoke check, and a nomem twin.
    write_log(directory, "d1.jsonl", [
        goal(1500, mode="demo"),
        call(1501, "plan"),
        reply(1502, 100000, 10000, 900.0, model="demo-script"),
        event("smoke", 1503, call="GET /", ok=False, status=500, error="x"),
        event("promoted", 1504, name="z", tools=["z"]),
        event("done", 1505, state="done"),
    ])
    write_log(directory, "n1.jsonl", [
        goal(1600, arm="nomem"),
        call(1601, "plan"),
        reply(1602, 9000, 900, 500.0),
        event("promoted", 1603, name="q", tools=["q"]),
        event("done", 1604, state="done"),
    ])
    # Tolerated: a file with no readable event, and an empty file.
    write_log(directory, "junk.jsonl", ["not json at all"])
    write_log(directory, "empty.jsonl", [])


# Costs of the main fixture, by build, from the formula above. The output token
# counts are not all one tenth of the input counts, so the price fit is well posed.
S1_COSTS = [cost(1000, 100), cost(2000, 200), cost(500, 50)]
S2_COST = cost(100, 10)
S3_COSTS = [cost(3000, 300), cost(1000, 100)]
S4_COST = cost(1500, 150)
S5_COST = cost(1000, 120)
C1, C3 = sum(S1_COSTS), sum(S3_COSTS)
ALL_SPENT = C1 + S2_COST + C3 + S4_COST + S5_COST


def write_plain_builds(directory, n):
    """N one-call builds, one promoted function each, saved in reverse name order."""
    for i in range(n):
        t = 1000 + 10 * i
        write_log(directory, "b%03d.jsonl" % (n - i), [
            goal(t),
            call(t + 1, "step"),
            reply(t + 2, 100, 10, 500.0),
            event("promoted", t + 3, name="f%d" % i, tools=["f%d" % i]),
            event("done", t + 4, state="done"),
        ])


class MainFixtureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        write_main_fixture(self.dir)
        self.data = sp.report(sessions_dir=self.dir)
        self.mech = {m["id"]: m for m in self.data["mechanisms"]}

    def test_scope_counts_only_live_main_builds(self):
        # s1..s5 count; d1 (demo) and n1 (nomem) do not; junk and empty files do not.
        self.assertEqual(self.data["builds"], 5)
        self.assertEqual(self.data["functions_saved"], 4)
        self.assertEqual(self.data["model_calls"], 8)
        self.assertEqual(self.data["spent_usd"], round(ALL_SPENT, 4))
        self.assertEqual(self.data["seconds"], 370.0)
        self.assertEqual(self.data["window"], {"first": 1000.0, "last": 1410.0})

    def test_single_period_for_five_builds(self):
        self.assertEqual(len(self.data["periods"]), 1)
        period = self.data["periods"][0]
        repair_cost = cost(500, 50) + cost(1000, 100)
        self.assertEqual(period["label"], "builds 1-5")
        self.assertEqual(period["builds"], 5)
        self.assertEqual(period["functions_saved"], 4)
        self.assertEqual(period["calls_per_function"], 2.0)
        self.assertEqual(period["usd_per_function"], round(ALL_SPENT / 4, 4))
        self.assertEqual(period["seconds_per_function"], 92.5)
        # First verdicts: f1 False (s1), f2 True and f3 False (s3). s5 has none.
        self.assertEqual(period["first_try_rate"], round(1 / 3, 3))
        self.assertEqual(period["repair_share"], round(repair_cost / ALL_SPENT, 3))

    def test_auto_fix_mechanism(self):
        m = self.mech["auto-fix"]
        # the empty auto_fix event is not counted, and neither is the reply that only got a docstring
        self.assertEqual(m["events"], 1)
        self.assertEqual(m["saved_calls"], 1)
        self.assertEqual(m["cosmetic"], 1)
        repair_cost = statistics.median([cost(500, 50), cost(1000, 100)])
        self.assertEqual(m["saved_usd"], round(repair_cost, 4))
        self.assertEqual(m["saved_seconds"], round(1500 / 1000, 1))  # median 1.5 s
        self.assertEqual(m["basis"], "estimate")
        self.assertEqual(m["by_fix"], {"added-docstring": 1, "nested-state-data": 1})
        self.assertIn("1 further replies only had a docstring added", m["how"])

    def test_compaction_mechanism(self):
        m = self.mech["compaction"]
        # Two calls left text out: 400 characters (s1) and 1000 (s3).
        self.assertEqual(m["events"], 2)
        self.assertEqual(m["saved_chars"], 1400)
        # Calls with a context: 2000 + 500 + 3000 input tokens over 1000 + 500 + 2000 chars.
        tokens = 1400 * (2000 + 500 + 3000) / (1000 + 500 + 2000)  # 2200 tokens
        self.assertEqual(m["saved_usd"], round(tokens * IN_PRICE, 4))  # 0.0022
        self.assertIsNone(m["saved_calls"])
        self.assertIsNone(m["saved_seconds"])
        self.assertEqual(m["basis"], "estimate")

    def test_edits_mechanism(self):
        m = self.mech["edits"]
        self.assertEqual(m["events"], 1)
        self.assertIsNone(m["saved_calls"])
        # 4000 characters at 4 per token: 1000 output tokens.
        self.assertEqual(m["saved_usd"], round(1000 * OUT_PRICE, 4))  # 0.0015
        # Mean output speed over all replies: 1030 tokens in 15 seconds.
        self.assertEqual(m["saved_seconds"], round(1000 / (1030 / 15), 1))  # 14.6
        self.assertEqual(m["basis"], "estimate")

    def test_warm_repl_mechanism_uses_logged_cold_checks(self):
        m = self.mech["warm-repl"]
        # Two warm checks (2 ms and 4 ms); the only cold check is 100 ms (warm false).
        self.assertEqual(m["events"], 2)
        self.assertEqual(m["saved_seconds"], round(2 * (100 - 3) / 1000, 1))  # 0.2
        self.assertIsNone(m["saved_calls"])
        self.assertIsNone(m["saved_usd"])
        self.assertEqual(m["basis"], "measured")

    def test_kit_mechanism(self):
        m = self.mech["kit"]
        # alpha seeds pair-value, table-rows and html-page (3); beta seeds pair-value (1).
        self.assertEqual(m["events"], 4)
        # Builds that saved a function: calls per function 3, 1, 1 (median 1);
        # cost per function C1, C3 / 2, S5 (median C3 / 2).
        self.assertEqual(m["saved_calls"], 4)
        self.assertEqual(m["saved_usd"], round(4 * statistics.median([C1, C3 / 2, S5_COST]), 4))
        self.assertEqual(m["basis"], "estimate")

    def test_reuse_mechanism(self):
        m = self.mech["reuse"]
        self.assertEqual(m["events"], 1)  # only s2 has the cache flow
        typical = statistics.median([C1, C3, S4_COST, S5_COST])  # build and plan builds
        self.assertEqual(m["saved_usd"], round(max(0.0, typical - S2_COST), 4))  # 0.0027
        self.assertIsNone(m["saved_calls"])
        self.assertEqual(m["basis"], "estimate")

    def test_free_checks_mechanism(self):
        m = self.mech["free-checks"]
        # One failed smoke (s1), one acceptance with a failure (s3), one interface
        # problem (s4), one style problem (s5). The failing demo smoke is excluded.
        self.assertEqual(m["events"], 4)
        self.assertIsNone(m["saved_calls"])
        self.assertIsNone(m["saved_usd"])
        self.assertIsNone(m["saved_seconds"])
        self.assertEqual(m["basis"], "count")

    def test_saved_totals_sum_the_mechanisms(self):
        saved = self.data["saved"]
        self.assertEqual(saved["calls"], 1 + 4)  # auto-fix and kit only
        self.assertEqual(saved["usd"], round(sum(m["saved_usd"] or 0 for m in self.data["mechanisms"]), 4))
        self.assertEqual(saved["seconds"], 16.3)  # 1.5 + 14.6 + 0.2

    def test_mechanism_order_and_labels(self):
        self.assertEqual([m["id"] for m in self.data["mechanisms"]], MECHANISM_IDS)
        self.assertEqual([m["label"] for m in self.data["mechanisms"]], LABELS)

    def test_notes_name_the_fitted_prices_and_scope(self):
        notes = " ".join(self.data["notes"])
        self.assertIn("0.9900 USD per million input tokens", notes)
        self.assertIn("1.4900 per million output tokens", notes)
        self.assertIn("nomem twins are not counted", notes)
        self.assertIn("the auto-fix saving is an upper bound", notes)
        self.assertIn("like-for-like comparison", notes)
        self.assertTrue(all(isinstance(n, str) for n in self.data["notes"]))

    def test_matches_economics_totals(self):
        econ = ec.project_economics(None, "live", sessions_dir=self.dir)
        self.assertEqual(self.data["spent_usd"], round(econ["total"]["cost_usd"], 4))
        self.assertEqual(self.data["model_calls"], econ["total"]["model_calls"])
        self.assertEqual(self.data["seconds"], econ["total"]["seconds"])
        self.assertEqual(self.data["functions_saved"], econ["churn"]["saves"])

    def test_project_filter(self):
        alpha = sp.report("alpha", sessions_dir=self.dir)
        self.assertEqual(alpha["builds"], 4)
        self.assertEqual(alpha["functions_saved"], 3)
        self.assertEqual(alpha["spent_usd"], round(C1 + S2_COST + C3 + S4_COST, 4))
        beta = sp.report("beta", sessions_dir=self.dir)
        self.assertEqual(beta["builds"], 1)
        self.assertEqual(beta["project"], "beta")
        self.assertEqual(beta["spent_usd"], round(S5_COST, 4))
        self.assertEqual(beta["periods"], [])
        kit = {m["id"]: m for m in beta["mechanisms"]}["kit"]
        self.assertEqual(kit["events"], 1)

    def test_demo_and_both_modes(self):
        demo = sp.report(mode="demo", sessions_dir=self.dir)
        self.assertEqual(demo["builds"], 1)
        self.assertEqual(demo["spent_usd"], round(cost(100000, 10000), 4))
        both = sp.report(mode=None, sessions_dir=self.dir)
        self.assertEqual(both["builds"], 6)  # five live builds and the demo build, no nomem
        free = {m["id"]: m for m in both["mechanisms"]}["free-checks"]
        self.assertEqual(free["events"], 5)  # the demo smoke failure now counts too

    def test_report_keys_and_mechanism_shape(self):
        self.assertEqual(set(self.data), REPORT_KEYS)
        self.assertEqual(set(self.data["window"]), {"first", "last"})
        self.assertIsInstance(self.data["notes"], list)
        for m in self.data["mechanisms"]:
            self.assertTrue(MECHANISM_KEYS <= set(m), m["id"])
            self.assertIn(m["basis"], ("measured", "estimate", "count"))
            self.assertTrue(m["how"])

    def test_render_mentions_every_label_and_basis(self):
        text = sp.render(self.data)
        for label in LABELS:
            self.assertIn(label, text)
        for basis in ("[measured]", "[estimate]", "[count]"):
            self.assertIn(basis, text)
        self.assertIn("Periods, oldest first:", text)
        self.assertIn("Notes:", text)

    def test_json_flag_prints_parseable_report(self):
        out = io.StringIO()
        with mock.patch.object(sp, "SESSIONS_DIR", self.dir), contextlib.redirect_stdout(out):
            code = sp.main(["--json"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(set(data), REPORT_KEYS)
        self.assertEqual(data["builds"], 5)

    def test_text_output_for_one_project(self):
        out = io.StringIO()
        with mock.patch.object(sp, "SESSIONS_DIR", self.dir), contextlib.redirect_stdout(out):
            sp.main(["beta"])
        self.assertIn("project beta", out.getvalue())


class PeriodTests(unittest.TestCase):
    def test_period_splitting(self):
        expected = {
            2: [],
            3: [("builds 1-3", 3)],
            7: [("builds 1-4", 4), ("builds 5-7", 3)],
            13: [("builds 1-4", 4), ("builds 5-7", 3), ("builds 8-10", 3), ("builds 11-13", 3)],
        }
        for n, groups in expected.items():
            with self.subTest(builds=n), tempfile.TemporaryDirectory() as tmp:
                write_plain_builds(tmp, n)
                data = sp.report(sessions_dir=tmp)
                self.assertEqual(data["builds"], n)
                self.assertEqual([(p["label"], p["builds"]) for p in data["periods"]], groups)
                self.assertEqual(sum(p["functions_saved"] for p in data["periods"]),
                                 sum(p["builds"] for p in data["periods"]))
                for p in data["periods"]:
                    self.assertEqual(p["calls_per_function"], 1.0)
                    self.assertEqual(p["usd_per_function"], round(cost(100, 10), 4))
                    self.assertIsNone(p["first_try_rate"])  # no verdicts in these builds
                    self.assertEqual(p["repair_share"], 0.0)
                self.assertEqual(data["window"], {"first": 1000.0,
                                                  "last": 1000.0 + 10 * (n - 1) + 4})

    def test_periods_follow_time_not_file_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_plain_builds(tmp, 6)
            data = sp.report(sessions_dir=tmp)
            # The earliest build is named b006.jsonl, so its name sorts last.
            self.assertEqual(data["window"]["first"], 1000.0)
            self.assertEqual(data["periods"][0]["label"], "builds 1-3")
            self.assertEqual(data["periods"][0]["functions_saved"], 3)


class EdgeCaseTests(unittest.TestCase):
    def test_empty_directory_has_no_division_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = sp.report(sessions_dir=tmp)
        self.assertEqual(data["builds"], 0)
        self.assertEqual(data["spent_usd"], 0.0)
        self.assertEqual(data["periods"], [])
        self.assertEqual(data["window"], {"first": None, "last": None})
        self.assertEqual([m["id"] for m in data["mechanisms"]], MECHANISM_IDS)
        by_id = {m["id"]: m for m in data["mechanisms"]}
        for mid in MECHANISM_IDS:
            self.assertEqual(by_id[mid]["events"], 0, mid)
        self.assertEqual(by_id["reuse"]["saved_usd"], 0.0)
        self.assertEqual(by_id["warm-repl"]["saved_seconds"], 0.0)
        self.assertEqual(data["saved"], {"calls": 0, "usd": 0.0, "seconds": 0.0})

    def test_missing_directory_is_empty(self):
        data = sp.report(sessions_dir=Path(tempfile.gettempdir()) / "no-such-specialization-dir")
        self.assertEqual(data["builds"], 0)

    def test_warm_fallback_when_no_check_is_logged_cold(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_log(tmp, "w.jsonl", [
                goal(1000),
                event("repl", 1001, label="rehearse", ok=True, elapsed_ms=1.0, warm=True),
                event("repl", 1002, label="rehearse", ok=True, elapsed_ms=2.0, warm=True),
                event("repl", 1003, label="rehearse", ok=True, elapsed_ms=3.0, warm=True),
                event("done", 1004, state="done"),
            ])
            m = {x["id"]: x for x in sp.report(sessions_dir=tmp)["mechanisms"]}["warm-repl"]
        self.assertEqual(m["events"], 3)
        # Cold falls back to 122 ms; the warm median is 2 ms: 3 x 120 ms.
        self.assertEqual(m["saved_seconds"], round(3 * (122 - 2) / 1000, 1))  # 0.4
        self.assertEqual(m["basis"], "estimate")
        self.assertIn("fallback", m["how"])

    def test_price_fit_falls_back_when_replies_are_proportional(self):
        calls = [{"cost": cost(100, 10), "input_tokens": 100, "output_tokens": 10},
                 {"cost": cost(200, 20), "input_tokens": 200, "output_tokens": 20}]
        price_in, price_out, measured = sp._fit_prices(calls)
        self.assertFalse(measured)
        self.assertEqual((price_in, price_out), (sp.DEFAULT_PRICE_IN, sp.DEFAULT_PRICE_OUT))

    def test_price_fit_recovers_the_prices(self):
        calls = [{"cost": cost(i, o), "input_tokens": i, "output_tokens": o}
                 for i, o in ((100, 10), (3000, 40), (50, 900))]
        price_in, price_out, measured = sp._fit_prices(calls)
        self.assertTrue(measured)
        self.assertAlmostEqual(price_in, IN_PRICE, places=12)
        self.assertAlmostEqual(price_out, OUT_PRICE, places=12)


class RealLogTests(unittest.TestCase):
    @unittest.skipUnless(ec.SESSIONS_DIR.is_dir(), "no session logs in this checkout")
    def test_real_logs_have_the_report_shape(self):
        data = sp.report()
        self.assertEqual(set(data), REPORT_KEYS)
        self.assertEqual([m["id"] for m in data["mechanisms"]], MECHANISM_IDS)
        self.assertIsInstance(sp.render(data), str)
        json.dumps(data)  # must serialise


class MeasuredAutoFixTests(unittest.TestCase):
    ENTRY = {"id": "auto-fix", "label": "x", "events": 10, "saved_calls": 10, "saved_usd": 0.05,
             "saved_seconds": 20.0, "basis": "estimate", "how": "estimated"}

    def test_the_replay_measurement_replaces_the_estimate(self):
        m = sp._measured_auto_fix(self.ENTRY, {"rescued": 4, "cases": 50, "rescued_usd": 0.0123})
        self.assertEqual((m["saved_calls"], m["saved_usd"], m["saved_seconds"], m["basis"]),
                         (4, 0.0123, 8.0, "measured"))
        self.assertEqual((m["events"], m["estimate_calls"]), (10, 10))
        self.assertIn("50 logged replies", m["how"])
        self.assertEqual(self.ENTRY["basis"], "estimate")           # the entry passed in is left alone

    def test_a_replay_without_counts_changes_nothing(self):
        self.assertEqual(sp._measured_auto_fix(self.ENTRY, {"rescued": None}), self.ENTRY)
        self.assertEqual(sp._measured_auto_fix(self.ENTRY, {}), self.ENTRY)


class AppPeriodTests(unittest.TestCase):
    def test_only_app_sized_builds_enter_the_like_for_like_periods(self):
        def build(t, functions, repairs, fixes):
            calls = [{"cat": "repairs", "cost": 0.001, "latency_ms": 100}] * repairs + \
                    [{"cat": "first drafts", "cost": 0.002, "latency_ms": 100}] * functions
            return {"t": t, "functions": functions, "calls": calls, "cost": sum(c["cost"] for c in calls),
                    "seconds": 10.0 * functions, "first": [True] * functions,
                    "fixes": [["nested-state-data"]] * fixes + [["added-docstring"]]}
        day = 86400
        sessions = [build(1, 1, 5, 9)] + [build(10 * day + k, 4, 8, 0) for k in range(3)] + \
                   [build(12 * day + k, 5, 5, 10) for k in range(3)]
        rows = sp._app_periods(sessions)
        self.assertEqual([r["builds"] for r in rows], [3, 3])      # the one-function build is left out
        self.assertEqual([r["functions_saved"] for r in rows], [12, 15])
        self.assertEqual([r["repairs_per_function"] for r in rows], [2.0, 1.0])
        self.assertEqual([r["free_repairs_per_function"] for r in rows], [0.0, 2.0])   # docstrings are not counted
        self.assertNotEqual(rows[0]["label"], rows[1]["label"])
        self.assertEqual(sp._app_periods(sessions[:3]), [])        # fewer than three app builds


if __name__ == "__main__":
    unittest.main()
