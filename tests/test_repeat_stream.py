"""Offline tests for the repeat-stream driver. Stdlib only.

Runs tiny 2-round streams over real exposure tasks with the
all-pass stub adapter in a temp dir: round 1 stores, round 2 must
fire gated fast-path hits on the repeats (zero new stores for
repeated checks), calls/task must fall, and the library must not
duplicate. No live calls.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks import runner
from experiments import repeat_stream

RECORDED = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "benchmarks", "family-a", "recorded", "stub_all_pass.json")


def _exposure_tasks(n):
    tasks = sorted(
        (t for t in runner.load_tasks(
            Path(RECORDED).parent.parent) if t.get("split") == "exposure"),
        key=lambda t: t["id"])
    return tasks[:n]


class RepeatStreamTest(unittest.TestCase):
    def _run(self, tasks, rounds):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = os.path.join(tmp.name, "artifacts")

        def make_inner():
            return runner.StubAdapter(Path(RECORDED))

        return repeat_stream.run_stream(tasks, out, make_inner,
                                       rounds=rounds)

    def test_round2_hits_repeats_and_cuts_calls(self):
        payload = self._run(_exposure_tasks(4), rounds=(2, 4))
        first, second = payload["round_rows"]
        self.assertEqual(first["fast_path_hits"], 0)
        self.assertGreater(first["stored"], 0)
        # Round 2 repeats round-1 tasks: gated hits must fire.
        self.assertGreater(second["fast_path_hits"], 0)
        # Hits skip the inner adapter: fewer assisted checks than
        # total checks (stub records no usage, so count directly).
        total_checks = sum(
            len(t["checks"]) for t in _exposure_tasks(4))
        self.assertLess(second["assisted_checks"], total_checks)
        # Every hit replays a verified output: all hits help.
        self.assertEqual(second["hit_helped"],
                         second["fast_path_hits"])

    def test_library_dedups_repeated_checks(self):
        payload = self._run(_exposure_tasks(3), rounds=(3, 3, 3))
        rows = payload["round_rows"]
        self.assertGreater(rows[0]["stored"], 0)
        self.assertEqual(rows[1]["stored"], 0)
        self.assertEqual(rows[2]["stored"], 0)
        self.assertEqual(rows[0]["library_size"],
                         rows[2]["library_size"])

    def test_churn_retires_never_hit_patches(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = os.path.join(tmp.name, "artifacts")

        def make_inner():
            return runner.StubAdapter(Path(RECORDED))

        # Round 1 stores tasks 1-2; round 2 churns to 3-4 (no repeats);
        # retiring after round 2 must drop the never-hit round-1
        # patches; round 3 repeats 3-4 and must still hit them.
        payload = repeat_stream.run_stream(
            _exposure_tasks(4), out, make_inner,
            rounds=("1-2", "3-4", "3-4"), retire_unused_after=2)
        first, second, third = payload["round_rows"]
        self.assertEqual(first["stored"], 4)
        self.assertEqual(second["retired"], 4)
        self.assertEqual(second["library_size"], 8 - 4)
        self.assertEqual(third["fast_path_hits"], 4)
        self.assertEqual(third["hit_helped"], 4)
        self.assertEqual(third["passed"], third["tasks"])

    def test_summary_json_written(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = os.path.join(tmp.name, "artifacts")

        def make_inner():
            return runner.StubAdapter(Path(RECORDED))

        repeat_stream.run_stream(_exposure_tasks(2), out, make_inner,
                                 rounds=(1, 2))
        with open(os.path.join(out, "summary.json"),
                  encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["experiment"], "repeat-stream")
        self.assertEqual(len(payload["round_rows"]), 2)


if __name__ == "__main__":
    unittest.main()
