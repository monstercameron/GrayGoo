"""Thesis acceptance tests (core directive, section 14).

One test per rung of the execution hierarchy, each on a held-out
Family R task through the real C/D adapters (stub stands in for Qwen
offline; live runs substitute the model without changing the path):

    reuse      R-REU-01 solves with 0 model calls.
    compose    R-CMP-01 solves with 0 model calls.
    adapt      R-ADP-01: direct exec abstains, retrieved context plus
               the model solves it (ADAPT, calls > 0).
    novelty    R-NOV-01: nothing applies, nothing retrieves, the model
               solves from scratch (NOVEL).
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks import run_abcd, runner  # noqa: E402
import execaps  # noqa: E402

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAMILY_R = ROOT / "benchmarks" / "family-r"
RECORDED = FAMILY_R / "recorded" / "stub_all_pass.json"

TASKS = {t["id"]: t for t in runner.load_tasks(FAMILY_R)}


def _solve(task_id, allow_compose):
    """Solve one task through the C/D adapter; return (record, adapter)."""
    task = TASKS[task_id]
    inner = run_abcd.CountingStub(RECORDED)
    registry = execaps.ExecRegistry()  # fresh build: the "restart" step
    adapter = run_abcd.ExecAdapter(
        inner, registry, allow_compose=allow_compose,
        adapt_memory=run_abcd.seed_b_memory())
    summary = runner.run([task], adapter, strip_fences=True)
    record = run_abcd.rollup(task, summary["tasks"][0], adapter)
    return record, adapter, inner


class ReuseAcceptanceTest(unittest.TestCase):
    def test_unseen_task_solves_with_zero_calls(self):
        record, _, inner = _solve("R-REU-01", allow_compose=False)
        self.assertTrue(record["passed"])
        self.assertEqual(record["outcome"], "REUSE")
        self.assertEqual(record["calls"], 0)
        self.assertEqual(len(inner.history), 0)
        self.assertEqual(record["via"], ["cap-date-iso"] * 2)


class ComposeAcceptanceTest(unittest.TestCase):
    def test_composite_task_solves_with_zero_calls(self):
        record, _, inner = _solve("R-CMP-01", allow_compose=True)
        self.assertTrue(record["passed"])
        self.assertEqual(record["outcome"], "COMPOSE")
        self.assertEqual(record["calls"], 0)
        self.assertEqual(len(inner.history), 0)


class AdaptAcceptanceTest(unittest.TestCase):
    def test_near_match_abstains_then_adapts(self):
        record, adapter, inner = _solve("R-ADP-01", allow_compose=True)
        # Direct execution must NOT have fired (ISO-week line is
        # outside the capability contract).
        self.assertEqual(record["via"], [])
        self.assertTrue(record["passed"])
        self.assertEqual(record["outcome"], "ADAPT")
        self.assertGreater(record["calls"], 0)
        self.assertGreater(len(inner.history), 0)
        # The fallback prompt carried retrieved context (minimal:
        # top-1 exemplar), not an empty scratch prompt.
        self.assertIn("R-ADP-01", adapter.adapted_by_task)


class NoveltyAcceptanceTest(unittest.TestCase):
    def test_unknown_task_falls_back_to_model(self):
        record, adapter, inner = _solve("R-NOV-01", allow_compose=True)
        self.assertEqual(record["via"], [])
        self.assertTrue(record["passed"])
        self.assertEqual(record["outcome"], "NOVEL")
        self.assertGreater(record["calls"], 0)
        self.assertNotIn("R-NOV-01", adapter.adapted_by_task)


if __name__ == "__main__":
    unittest.main()
