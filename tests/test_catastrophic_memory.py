"""Catastrophic-memory test (issues.md #102). Stdlib only, offline.

Injects one WRONG promoted patch into a PatchMemory, then proves the
detect → contain → recover loop over production paths only
(PatchMemory, transfer rows, consolidate.retire — no new code):

1. DETECT: the bad patch fires on its input (1 misfire) and the
   failed check records a hurt row against it.
2. CONTAIN: the hurt row revokes it — it never fires again
   (find_fast_path confidence gate), while a good patch keeps firing.
3. RECOVER: consolidate.retire removes it from retrieval;
   re-solving the check re-stores the correct output (has_check).

No live calls, tempfile store.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import consolidate
from benchmarks.patch_memory_adapter import PatchMemory


def _task(task_id, split="transfer"):
    return {"id": task_id, "split": split, "category": "csv",
            "prompt": "p"}


class CatastrophicMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mem = PatchMemory(self.tmp.name)
        # One good patch (helps) and one injected-bad patch (hurts).
        self.good = self.mem.add_success(
            _task("A-EXP-01"), 0, "GOOD-IN", "GOOD-OUT")
        self.bad = self.mem.add_success(
            _task("A-EXP-02"), 0, "BAD-IN", "WRONG-OUT")
        for patch in (self.good, self.bad):
            self.mem.record_reuse(patch, _task("A-TRN-01"), True)

    def test_detect_contain_recover(self):
        history = self.mem.reuse_history()
        # DETECT: both fire on their recorded inputs (bad one misfires).
        self.assertIsNotNone(self.mem.find_fast_path(
            _task("A-EXP-02"), history=history, check_input="BAD-IN"))
        # The failed check records hurt against the bad patch.
        self.mem.record_reuse(self.bad, _task("A-TRN-09"), False)
        # CONTAIN: bad patch revoked, good patch unaffected.
        history = self.mem.reuse_history()
        self.assertIsNone(self.mem.find_fast_path(
            _task("A-EXP-02"), history=history, check_input="BAD-IN"))
        self.assertIsNotNone(self.mem.find_fast_path(
            _task("A-EXP-01"), history=history, check_input="GOOD-IN"))
        # RECOVER: retire the bad patch, re-store the correct output.
        report = consolidate.retire(
            self.mem.store, [self.bad["patch_id"]])
        self.assertEqual(report["count"], 1)
        self.assertEqual(self.mem.capability_count(), 1)
        self.assertFalse(self.mem.has_check("A-EXP-02", 0))
        fixed = self.mem.add_success(
            _task("A-EXP-02"), 0, "BAD-IN", "RIGHT-OUT")
        self.mem.record_reuse(fixed, _task("A-TRN-01"), True)
        self.assertEqual(
            self.mem.fast_path_output(self.mem.find_fast_path(
                _task("A-EXP-02"),
                history=self.mem.reuse_history(),
                check_input="BAD-IN")),
            "RIGHT-OUT")


if __name__ == "__main__":
    unittest.main()
