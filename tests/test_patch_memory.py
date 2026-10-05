"""Focused tests for PatchMemory.find_fast_path input gating.

The matched-rerun fast-path fired on family+category+confidence alone
and returned recorded outputs for NOVEL inputs (0/4 input matches,
wrong answers, broke TRN-03/TRN-04). The gate: with ``check_input``
provided, fire only on exact recorded-input match. Stdlib only,
tempfile PatchStore, no live calls.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.patch_memory_adapter import PatchMemory


def _exposure_task():
    return {"id": "A-EXP-07", "split": "exposure", "category": "csv",
            "prompt": "p"}


def _transfer_task():
    return {"id": "A-TRN-03", "split": "transfer", "category": "csv",
            "prompt": "p"}


class FindFastPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mem = PatchMemory(self.tmp.name)
        self.patch = self.mem.add_success(
            _exposure_task(), 0, "IN-1", "OUT-1")
        self.mem.record_reuse(self.patch, _transfer_task(), True)
        self.history = self.mem.reuse_history()

    def test_legacy_rule_fires_without_input_gate(self):
        patch = self.mem.find_fast_path(_transfer_task(),
                                        history=self.history)
        self.assertIsNotNone(patch)
        self.assertEqual(self.mem.fast_path_output(patch), "OUT-1")

    def test_input_gate_blocks_novel_input(self):
        patch = self.mem.find_fast_path(
            _transfer_task(), history=self.history,
            check_input="NOVEL-INPUT")
        self.assertIsNone(patch)

    def test_input_gate_fires_on_exact_match(self):
        patch = self.mem.find_fast_path(
            _transfer_task(), history=self.history, check_input="IN-1")
        self.assertIsNotNone(patch)
        self.assertEqual(self.mem.fast_path_output(patch), "OUT-1")

    def test_adversarial_never_fires_even_on_match(self):
        task = {"id": "A-ADV-01", "split": "adversarial",
                "category": "csv", "prompt": "p"}
        self.assertIsNone(self.mem.find_fast_path(
            task, history=self.history, check_input="IN-1"))


if __name__ == "__main__":
    unittest.main()
