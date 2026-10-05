"""Retrieval-overhead-vs-size measurement (issues.md #103).

Seeds 500 patches across 5 categories and times `retrieve` +
`find_fast_path`. The bound is deliberately generous (10s for 50
queries over 500 patches — ~200ms/query) so this is a smoke/Reality
check, not a flaky perf assertion; real numbers go to
documents/retrieval-scale.md. Stdlib only, tempfile store.
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.patch_memory_adapter import PatchMemory

N_PATCHES = 500
N_QUERIES = 50
# Generous upper bound: 200ms per query over 500 patches.
BUDGET_S = 10.0


class RetrievalScaleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.mem = PatchMemory(cls.tmp.name)
        for i in range(N_PATCHES):
            task = {"id": "A-EXP-%02d" % (i % 18 + 1),
                    "split": "exposure",
                    "category": "cat-%d" % (i % 5), "prompt": "p"}
            patch = cls.mem.add_success(
                task, i % 2, "IN-%d" % i, "OUT-%d" % i)
            cls.mem.record_reuse(
                patch, {"id": "A-TRN-01", "split": "transfer",
                        "category": "cat-0", "prompt": "p"}, True)
        cls.history = cls.mem.reuse_history()

    def test_retrieval_bounded_at_500_patches(self):
        query = {"id": "A-TRN-01", "split": "transfer",
                 "category": "cat-0", "prompt": "p"}
        # Warm up: the first query populates the read cache (one cold
        # open per file — a one-time startup cost, not per-query
        # scaling). The timed loop measures steady-state retrieval.
        self.mem.retrieve(query)
        start = time.perf_counter()
        hits = 0
        for _ in range(N_QUERIES):
            patches = self.mem.retrieve(query)
            self.assertLessEqual(len(patches), 2)
            if self.mem.find_fast_path(query, history=self.history):
                hits += 1
        elapsed = time.perf_counter() - start
        per_query_ms = elapsed / N_QUERIES * 1000.0
        print("\nretrieval-scale: %d queries over %d patches in %.3fs "
              "(%.1f ms/query, %d fast-path hits)"
              % (N_QUERIES, N_PATCHES, elapsed, per_query_ms, hits))
        self.assertLess(elapsed, BUDGET_S)
        self.assertLess(per_query_ms, 100.0)


if __name__ == "__main__":
    unittest.main()
