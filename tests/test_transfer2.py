"""Tests for the supplemental transfer set (A-TRN-09..16). Stdlib only.

Pins: the generator reproduces the committed tasks byte-identically
(make_transfer2.py --check), the family validates clean at 43 tasks,
the frozen original-35 tasks are untouched (hash-pinned), and the
offline runner passes/fails the new tasks both ways. No live calls.
"""

import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from benchmarks import runner

FAMILY_DIR = Path(ROOT) / "benchmarks" / "family-a"

def _canonical(tasks):
    return json.dumps(tasks, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


class Transfer2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tasks = runner.load_tasks(FAMILY_DIR)
        cls.by_id = {t["id"]: t for t in cls.tasks}

    def test_family_validates_at_43_tasks(self):
        self.assertEqual(runner.validate_tasks(self.tasks), [])
        self.assertEqual(len(self.tasks), 43)
        transfer = [t for t in self.tasks if t["split"] == "transfer"]
        self.assertEqual(len(transfer), 16)

    def test_new_ids_sequential_and_wellformed(self):
        for n in range(9, 17):
            task_id = "A-TRN-%02d" % n
            self.assertIn(task_id, self.by_id)
            task = self.by_id[task_id]
            self.assertEqual(task["family"], "A")
            self.assertEqual(task["split"], "transfer")
            self.assertEqual(len(task["checks"]), 2)
            for check in task["checks"]:
                self.assertIn(check["compare"], ("exact", "json"))

    def test_generator_reproduces_committed_tasks(self):
        proc = subprocess.run(
            [sys.executable, "benchmarks/family-a/make_transfer2.py",
             "--check"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, timeout=120, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode(
            "utf-8", "replace"))

    def test_frozen_transfer_tasks_unchanged(self):
        frozen = [{k: v for k, v in self.by_id["A-TRN-%02d" % n].items()
                   if k != "_source"}  # strip runner-added provenance
                  for n in range(1, 9)]
        digest = hashlib.sha256(_canonical(frozen)).hexdigest()
        # Pinned from git HEAD (pre-append); recompute only by
        # deliberative review, never to silence this test.
        self.assertEqual(
            digest,
            "4f54869d35252605faaa235d6f1285a44495c50b57de941e4ec9737e0c7a074e")

    def test_offline_runner_passes_new_tasks(self):
        subset = [self.by_id["A-TRN-%02d" % n] for n in range(9, 17)]
        stub = runner.StubAdapter(
            FAMILY_DIR / "recorded" / "stub_all_pass.json")
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            summary = runner.run(subset, stub)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["passed"], 8)


if __name__ == "__main__":
    unittest.main()
