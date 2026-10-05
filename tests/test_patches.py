"""Tests for executable patch memory (patches.py). Stdlib only."""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import patches
from patches import PatchStore


class PatchPersistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PatchStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_persist_retrieve_round_trip(self):
        candidate = {"target": "write-csv", "definition": "(lambda (r s) r)"}
        saved = self.store.save_patch("task-A", candidate,
                                      task_family="parsing",
                                      tags=["csv", "escaping"])
        self.assertIn("patch_id", saved)
        self.assertEqual(saved["task_id"], "task-A")
        self.assertEqual(saved["candidate"], candidate)
        self.assertEqual(saved["ttl_days"], 7)
        self.assertEqual(saved["status"], "patch")
        self.assertIn("created_at", saved)

        fetched = self.store.get_patch(saved["patch_id"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched, saved)

    def test_persist_survives_reopen(self):
        saved = self.store.save_patch("task-A", {"code": "x"})
        reopened = PatchStore(self.tmp.name)
        self.assertEqual(reopened.get_patch(saved["patch_id"]), saved)

    def test_missing_patch_returns_none(self):
        self.assertIsNone(self.store.get_patch("no-such-patch"))


class PatchExpiryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PatchStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_expiry_enforced_on_read(self):
        # Created 10 days ago with the default 7-day TTL: expired.
        old = time.time() - 10 * 86400.0
        saved = self.store.save_patch("task-old", {"code": "old"},
                                      created_at=old)
        # Expiry applies on read even before any sweep runs.
        self.assertIsNone(self.store.get_patch(saved["patch_id"]))
        self.assertEqual(self.store.find_for_task(), [])

    def test_fresh_patch_readable_before_ttl(self):
        saved = self.store.save_patch("task-new", {"code": "new"})
        self.assertIsNotNone(self.store.get_patch(saved["patch_id"]))

    def test_ttl_sweep_deletes_expired_and_reports_counts(self):
        old = time.time() - 10 * 86400.0
        self.store.save_patch("task-old-1", {"code": "a"}, created_at=old)
        self.store.save_patch("task-old-2", {"code": "b"}, created_at=old)
        live = self.store.save_patch("task-live", {"code": "c"})
        report = self.store.sweep()
        self.assertEqual(report["swept"], 2)
        self.assertEqual(report["remaining"], 1)
        self.assertIsNotNone(self.store.get_patch(live["patch_id"]))
        # Second sweep finds nothing left to expire.
        report2 = self.store.sweep()
        self.assertEqual(report2["swept"], 0)
        self.assertEqual(report2["remaining"], 1)

    def test_custom_ttl_days(self):
        two_days_ago = time.time() - 2 * 86400.0
        short = self.store.save_patch("t1", {"code": "x"},
                                      created_at=two_days_ago, ttl_days=1)
        long = self.store.save_patch("t2", {"code": "y"},
                                     created_at=two_days_ago, ttl_days=30)
        self.assertIsNone(self.store.get_patch(short["patch_id"]))
        self.assertIsNotNone(self.store.get_patch(long["patch_id"]))


class PatchRetrievalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PatchStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_find_for_task_by_family(self):
        self.store.save_patch("t1", {"code": "a"}, task_family="parsing")
        self.store.save_patch("t2", {"code": "b"}, task_family="serving")
        hits = self.store.find_for_task("parsing")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["task_family"], "parsing")

    def test_find_for_task_by_tags(self):
        self.store.save_patch("t1", {"code": "a"}, task_family="parsing",
                              tags=["csv"])
        self.store.save_patch("t2", {"code": "b"}, task_family="parsing",
                              tags=["json"])
        hits = self.store.find_for_task(tags=["csv"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["tags"], ["csv"])

    def test_find_for_task_excludes_expired(self):
        old = time.time() - 30 * 86400.0
        self.store.save_patch("t1", {"code": "a"}, task_family="parsing",
                              created_at=old)
        self.assertEqual(self.store.find_for_task("parsing"), [])


class PatchReuseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PatchStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_reuse_round_trip(self):
        saved = self.store.save_patch("task-A", {"code": "x"})
        row = self.store.record_reuse(saved["patch_id"], "task-B",
                                      helped=True, tokens_saved=120,
                                      latency_saved_ms=45.5)
        self.assertEqual(row["patch_id"], saved["patch_id"])
        self.assertEqual(row["task_id"], "task-B")
        self.assertTrue(row["helped"])
        self.assertEqual(row["tokens_saved"], 120)
        self.assertEqual(row["latency_saved_ms"], 45.5)

        rows = self.store.get_reuses(saved["patch_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], "task-B")

    def test_record_helped_and_hurt(self):
        saved = self.store.save_patch("task-A", {"code": "x"})
        self.store.record_reuse(saved["patch_id"], "task-B", helped=True,
                                tokens_saved=50, latency_saved_ms=10.0)
        self.store.record_reuse(saved["patch_id"], "task-C", helped=False,
                                tokens_saved=-20, latency_saved_ms=-5.0)
        rows = self.store.get_reuses(saved["patch_id"])
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["task_id"] for r in rows}, {"task-B", "task-C"})

    def test_reuse_log_survives_reopen(self):
        saved = self.store.save_patch("task-A", {"code": "x"})
        self.store.record_reuse(saved["patch_id"], "task-B", helped=True)
        reopened = PatchStore(self.tmp.name)
        rows = reopened.get_reuses(saved["patch_id"])
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
