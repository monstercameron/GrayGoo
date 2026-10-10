"""Lifecycle economics: totals, categories, creation vs rework, waste, reuse, break-even."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import economics as ec  # noqa: E402


def goal(t=1.0, project="p", mode="live", arm="main", prompt="build a thing"):
    return {"kind": "goal", "t": t, "prompt": prompt, "mode": mode, "arm": arm,
            "project": project}


def paid(label, cost, lane=None, tokens=(100, 50)):
    """A model_call followed by its model_reply, as the harness logs them."""
    call = {"kind": "model_call", "label": label}
    reply = {"kind": "model_reply", "cost_usd": cost,
             "input_tokens": tokens[0], "output_tokens": tokens[1]}
    if lane is not None:
        call["lane"] = lane
        reply["lane"] = lane
    return [call, reply]


def promoted(name, lane=None):
    event = {"kind": "promoted", "name": name}
    if lane is not None:
        event["lane"] = lane
    return event


def step(name):
    return {"kind": "step", "name": name}


def summary(outcome, flow=None, seconds=None):
    event = {"kind": "summary", "outcome": outcome}
    if flow is not None:
        event["flow"] = flow
    if seconds is not None:
        event["seconds"] = seconds
    return event


def write(directory, sid, events):
    """One session log: dicts become JSON lines, strings are written as they are."""
    lines = [e if isinstance(e, str) else json.dumps(e) for e in events]
    (Path(directory) / (sid + ".jsonl")).write_text("\n".join(lines) + "\n", encoding="utf-8")


class EconomicsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def economics(self, project=None, mode="live"):
        return ec.project_economics(project, mode, self.dir)

    def fn(self, report, name):
        return next(f for f in report["functions"] if f["name"] == name)

    def test_totals(self):
        write(self.dir, "s1", [goal(1), *paid("plan", 0.01, tokens=(100, 50)),
                               *paid("step", 0.02, tokens=(200, 100)), summary("success", seconds=30)])
        write(self.dir, "s2", [goal(2), *paid("repair", 0.03, tokens=(300, 150)),
                               summary("failed", seconds=10)])
        r = self.economics()
        self.assertEqual(r["sessions"], 2)
        self.assertEqual(r["outcomes"], {"success": 1, "failed": 1, "cancelled": 0,
                                         "error": 0, "unknown": 0})
        self.assertEqual(r["total"], {"cost_usd": 0.06, "input_tokens": 600, "output_tokens": 300,
                                      "model_calls": 3, "seconds": 40.0})

    def test_seconds_without_summary_use_first_and_last_event(self):
        write(self.dir, "s1", [goal(10), *paid("step", 0.1), {"kind": "repl", "t": 25.0, "ok": True},
                               {"kind": "done", "state": "done"}])
        write(self.dir, "s2", [goal(30), *paid("step", 0.1), summary("success", seconds=30)])
        self.assertEqual(self.economics()["total"]["seconds"], 45.0)

    def test_grouping_by_kind_includes_retry_labels(self):
        write(self.dir, "s1", [goal(1), *paid("plan", 0.01), *paid("split", 0.02),
                               *paid("step", 0.04), *paid("step 1", 0.05),
                               *paid("repair", 0.1), *paid("rewrite (retry: invalid JSON)", 0.2),
                               *paid("visual-review", 0.4), *paid("mystery", 0.5),
                               summary("success")])
        kinds = self.economics()["by_kind"]
        expected = {"planning": (0.03, 2), "first drafts": (0.09, 2), "repairs": (0.3, 2),
                    "screenshot checks": (0.4, 1), "other": (0.5, 1)}
        for name, (cost, calls) in expected.items():
            self.assertAlmostEqual(kinds[name]["cost_usd"], cost, places=6, msg=name)
            self.assertEqual(kinds[name]["model_calls"], calls, msg=name)
            self.assertEqual(kinds[name]["tokens"], 150 * calls, msg=name)

    def test_creation_and_rework_with_interleaved_lanes(self):
        write(self.dir, "s1", [
            goal(1), *paid("plan", 0.5),
            {"kind": "model_call", "label": "step", "lane": "a"},
            {"kind": "model_call", "label": "step", "lane": "b"},
            {"kind": "model_reply", "lane": "b", "cost_usd": 0.2, "input_tokens": 100, "output_tokens": 50},
            {"kind": "model_reply", "lane": "a", "cost_usd": 0.1, "input_tokens": 100, "output_tokens": 50},
            promoted("a", "a"),
            *paid("repair", 0.05, lane="a"),
            *paid("repair", 0.03, lane="b"),
            promoted("b", "b"),
            summary("success")])
        r = self.economics()
        a, b = self.fn(r, "a"), self.fn(r, "b")
        self.assertEqual([f["name"] for f in r["functions"]], ["b", "a"])
        self.assertAlmostEqual(a["creation_cost_usd"], 0.1, places=6)
        self.assertAlmostEqual(a["rework_cost_usd"], 0.05, places=6)
        self.assertEqual(a["repair_calls"], 1)
        self.assertAlmostEqual(b["creation_cost_usd"], 0.23, places=6)
        self.assertAlmostEqual(b["rework_cost_usd"], 0.0, places=6)
        self.assertEqual(b["repair_calls"], 1)
        self.assertEqual(r["churn"]["rebuilt"], [])
        self.assertEqual(r["churn"]["rework_share"], 0.057)  # 0.05 of 0.88

    def test_creation_and_rework_without_lanes(self):
        write(self.dir, "s1", [
            goal(1), *paid("plan", 0.5),
            step("a"), *paid("step", 0.1), promoted("a"),
            step("b"), *paid("step", 0.2), *paid("repair", 0.04), promoted("b"),
            summary("success")])
        write(self.dir, "s2", [goal(2), step("a"), *paid("repair", 0.07), promoted("a"),
                               summary("success")])
        r = self.economics()
        a, b = self.fn(r, "a"), self.fn(r, "b")
        self.assertEqual([f["name"] for f in r["functions"]], ["b", "a"])
        self.assertEqual(a["builds"], 2)
        self.assertAlmostEqual(a["creation_cost_usd"], 0.1, places=6)
        self.assertAlmostEqual(a["rework_cost_usd"], 0.07, places=6)
        self.assertEqual(a["repair_calls"], 1)
        self.assertEqual(b["builds"], 1)
        self.assertAlmostEqual(b["creation_cost_usd"], 0.24, places=6)
        self.assertEqual(b["repair_calls"], 1)
        self.assertEqual(r["churn"]["rebuilt"], ["a"])
        self.assertEqual(r["churn"]["saves"], 3)
        self.assertEqual(r["churn"]["functions_saved"], 2)
        self.assertEqual(r["churn"]["rework_share"], 0.077)  # 0.07 of 0.91

    def test_function_saved_three_times(self):
        for i, cost in enumerate((0.1, 0.2, 0.3), start=1):
            write(self.dir, "s%d" % i, [goal(i), step("css-style"), *paid("step", cost),
                                        promoted("css-style"), summary("success")])
        r = self.economics()
        f = self.fn(r, "css-style")
        self.assertEqual(f["builds"], 3)
        self.assertAlmostEqual(f["creation_cost_usd"], 0.1, places=6)
        self.assertAlmostEqual(f["rework_cost_usd"], 0.5, places=6)
        self.assertEqual(r["churn"]["rebuilt"], ["css-style"])
        self.assertEqual(r["churn"]["rework_share"], 0.833)
        self.assertIn("css-style was saved 3 times.",
                      "\n".join(ec.render(r)))

    def test_failed_session_counts_in_waste(self):
        write(self.dir, "s1", [goal(1), *paid("step", 0.2), *paid("repair", 0.05),
                               *paid("rewrite (retry: invalid JSON)", 0.01), summary("failed")])
        write(self.dir, "s2", [goal(2), *paid("step", 0.1), summary("success")])
        r = self.economics()
        self.assertEqual(r["outcomes"]["failed"], 1)
        self.assertEqual(r["waste"], {"failed_session_cost_usd": 0.26, "repair_cost_usd": 0.06,
                                      "unreadable_reply_calls": 1})

    def test_cancelled_session(self):
        write(self.dir, "s1", [goal(1), *paid("step", 0.3), summary("cancelled")])
        r = self.economics()
        self.assertEqual(r["outcomes"]["cancelled"], 1)
        self.assertEqual(r["waste"]["failed_session_cost_usd"], 0.3)

    def reuse_scenario(self, extra=()):
        write(self.dir, "s1", [goal(1), *paid("step", 0.4), summary("success", flow="build")])
        write(self.dir, "s2", [goal(2), *paid("plan", 0.6), summary("success", flow="plan")])
        write(self.dir, "s3", [goal(3), *paid("quick-reuse", 0.01), summary("success", flow="cache")])
        write(self.dir, "s4", [goal(4), {"kind": "decision", "action": "use"},
                               *paid("quick-reuse", 0.03)])
        for sid, events in extra:
            write(self.dir, sid, events)
        return self.economics()

    def test_cache_and_reuse_savings(self):
        r = self.reuse_scenario()
        self.assertEqual(r["reuse"]["zero_build_sessions"], 2)
        self.assertEqual(r["reuse"]["their_cost_usd"], 0.04)
        self.assertEqual(r["reuse"]["avg_build_session_cost_usd"], 0.5)
        self.assertEqual(r["reuse"]["estimated_savings_usd"], 0.96)  # 2 * 0.5 - 0.04
        self.assertIn("Estimate", r["reuse"]["note"])

    def test_break_even_not_reached(self):
        be = self.reuse_scenario()["break_even"]
        self.assertEqual(be["learning_cost_usd"], 1.0)
        self.assertEqual(be["savings_so_far_usd"], 0.96)
        self.assertFalse(be["reached"])
        self.assertEqual(be["sessions_to_break_even"], 1)  # 0.04 left, 0.48 saved per reuse

    def test_break_even_reached(self):
        r = self.reuse_scenario(extra=[("s5", [goal(5), summary("success", flow="cache")])])
        be = r["break_even"]
        self.assertEqual(r["reuse"]["zero_build_sessions"], 3)
        self.assertEqual(be["savings_so_far_usd"], 1.46)
        self.assertTrue(be["reached"])
        self.assertEqual(be["sessions_to_break_even"], 0)

    def test_break_even_none_without_reuse(self):
        write(self.dir, "s1", [goal(1), *paid("step", 0.4), summary("success", flow="build")])
        be = self.economics()["break_even"]
        self.assertEqual(be["savings_so_far_usd"], 0.0)
        self.assertFalse(be["reached"])
        self.assertIsNone(be["sessions_to_break_even"])

    def test_break_even_none_when_reuse_costs_more_than_a_build(self):
        write(self.dir, "s1", [goal(1), *paid("step", 0.4), summary("success", flow="build")])
        write(self.dir, "s2", [goal(2), *paid("plan", 0.6), summary("success", flow="plan")])
        write(self.dir, "s3", [goal(3), *paid("quick-reuse", 0.9), summary("success", flow="cache")])
        r = self.economics()
        self.assertEqual(r["reuse"]["estimated_savings_usd"], 0.0)
        self.assertIsNone(r["break_even"]["sessions_to_break_even"])

    def test_nomem_arm_is_excluded(self):
        write(self.dir, "s1", [goal(1), *paid("step", 0.1), summary("success")])
        write(self.dir, "s2", [goal(2, arm="nomem"), *paid("step", 5.0), summary("success")])
        r = self.economics()
        self.assertEqual(r["sessions"], 1)
        self.assertEqual(r["total"]["cost_usd"], 0.1)

    def test_project_filter(self):
        write(self.dir, "s1", [goal(1, project="p"), *paid("step", 0.1), summary("success")])
        write(self.dir, "s2", [goal(2, project="q"), *paid("step", 0.7), summary("success")])
        self.assertEqual(self.economics("p")["total"]["cost_usd"], 0.1)
        self.assertEqual(self.economics("p")["sessions"], 1)
        self.assertEqual(self.economics()["total"]["cost_usd"], 0.8)
        self.assertEqual(self.economics()["sessions"], 2)

    def test_mode_filter(self):
        write(self.dir, "s1", [goal(1, mode="live"), *paid("step", 0.1), summary("success")])
        write(self.dir, "s2", [goal(2, mode="demo"), *paid("step", 9.0), summary("success")])
        self.assertEqual(self.economics()["sessions"], 1)
        self.assertEqual(ec.project_economics(None, "demo", self.dir)["total"]["cost_usd"], 9.0)
        self.assertEqual(ec.project_economics(None, None, self.dir)["sessions"], 2)

    def test_missing_fields_and_broken_lines_are_tolerated(self):
        write(self.dir, "old", [
            "{broken json",
            {"kind": "goal", "prompt": "old run", "mode": "live"},
            {"kind": "model_call", "label": "step"},
            {"kind": "model_reply", "cost_usd": 0.25},
            {"kind": "promoted", "name": "render-nav"},
            {"kind": "done", "state": "done", "cost_usd": 0.25},
            "not json at all"])
        write(self.dir, "garbage", ["{oops"])
        r = self.economics()
        self.assertEqual(r["sessions"], 1)
        self.assertEqual(r["outcomes"]["success"], 1)
        self.assertEqual(r["total"]["cost_usd"], 0.25)
        self.assertEqual(r["total"]["input_tokens"], 0)
        self.assertEqual(r["total"]["model_calls"], 1)
        self.assertEqual(r["total"]["seconds"], 0.0)
        self.assertEqual(self.fn(r, "render-nav")["creation_cost_usd"], 0.25)
        self.assertEqual(ec.project_economics(None, None, self.dir)["sessions"], 2)

    def test_empty_directory_gives_zeros(self):
        for directory in (self.dir, self.dir / "missing"):
            r = ec.project_economics(None, "live", directory)
            self.assertEqual(r["sessions"], 0)
            self.assertEqual(r["total"]["cost_usd"], 0.0)
            self.assertEqual(r["functions"], [])
            self.assertFalse(r["break_even"]["reached"])
            self.assertIsNone(r["break_even"]["sessions_to_break_even"])
            self.assertEqual(r["trend"], [])
            self.assertIn("no sessions", ec.render(r)[0])

    def test_trend_keeps_the_last_ten_in_time_order(self):
        for i in range(12):
            write(self.dir, "s%02d" % i, [goal(i, prompt="x" * 100), *paid("step", 0.01 * (i + 1)),
                                          summary("success")])
        trend = self.economics()["trend"]
        self.assertEqual(len(trend), 10)
        self.assertEqual(trend[0]["id"], "s02")
        self.assertEqual(trend[-1]["id"], "s11")
        self.assertEqual(trend[-1]["cost_usd"], 0.12)
        self.assertEqual(len(trend[0]["prompt"]), 60)

    def test_render_contains_the_key_figures(self):
        r = self.reuse_scenario()
        text = "\n".join(ec.render(r))
        self.assertIsInstance(ec.render(r), list)
        for needle in ("$1.0400", "$1.0000", "$0.9600", "Where the spend went", "planning",
                       "Reuse (estimate)", "Break-even (estimate)", "Waste:", "Cost of the last"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
