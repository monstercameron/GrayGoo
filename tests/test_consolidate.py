"""Tests for capability consolidation + forgetting (consolidate.py). Stdlib only."""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import consolidate
from consolidate import (
    cluster_intents,
    find_duplicates,
    find_overlapping,
    find_unused,
    propose_generalize,
    propose_merge,
    rehearse_consolidation,
    retire,
    retrieval_candidates,
    retrieval_quality_before_after,
    renew_on_evidence,
    sweep_expired,
)
from patches import PatchStore

DAY = 86400.0


def _skill(sid, intent, **extra):
    record = {"id": sid, "intent": intent, "status": "live"}
    record.update(extra)
    return record


class DuplicateDetectionTest(unittest.TestCase):
    def test_near_duplicates_cluster(self):
        a = _skill("a", "parse CSV rows into records")
        b = _skill("b", "parse CSV rows into records quickly")
        clusters = find_duplicates([a, b])
        self.assertEqual(len(clusters), 1)
        self.assertEqual({r["id"] for r in clusters[0]}, {"a", "b"})

    def test_distinct_intents_do_not_cluster(self):
        a = _skill("a", "parse CSV rows into records")
        b = _skill("b", "send email receipts to customers")
        self.assertEqual(find_duplicates([a, b]), [])
        self.assertEqual(cluster_intents([a, b], threshold=0.6), [])

    def test_retired_excluded_from_clustering(self):
        a = _skill("a", "parse CSV rows into records")
        b = _skill("b", "parse CSV rows into records quickly",
                   status="deprecated")
        self.assertEqual(find_duplicates([a, b]), [])


class OverlapDetectionTest(unittest.TestCase):
    def test_overlapping_intents_cluster_at_overlap_threshold(self):
        a = _skill("a", "parse CSV rows into records")
        d = _skill("d", "parse JSON payloads into records")
        self.assertEqual(find_duplicates([a, d]), [])  # not near-dups
        clusters = find_overlapping([a, d])
        self.assertEqual(len(clusters), 1)
        self.assertEqual({r["id"] for r in clusters[0]}, {"a", "d"})

    def test_unrelated_intents_do_not_overlap(self):
        a = _skill("a", "parse CSV rows into records")
        b = _skill("b", "send email receipts to customers")
        self.assertEqual(find_overlapping([a, b]), [])


class UnusedDetectionTest(unittest.TestCase):
    def test_unused_flagged_and_recently_used_kept(self):
        now = time.time()
        stale = _skill("stale", "parse CSV rows",
                       reuses=[{"helped": True,
                                "recorded_at": now - 40 * DAY}])
        fresh = _skill("fresh", "parse CSV rows fast",
                       reuses=[{"helped": True,
                                "recorded_at": now - 2 * DAY}])
        never = _skill("never", "parse CSV rows slowly")
        unused = find_unused([stale, fresh, never], window_days=30, now=now)
        self.assertEqual([r["id"] for r in unused], ["never", "stale"])

    def test_last_used_timestamp_counts_as_use(self):
        now = time.time()
        record = _skill("s", "parse CSV rows", last_used_at=now - DAY)
        self.assertEqual(find_unused([record], window_days=30, now=now), [])

    def test_store_reuses_consulted(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            store = PatchStore(tmp.name)
            saved = store.save_patch("t1", {"code": "x"},
                                     task_family="parsing")
            # No reuse recorded -> unused.
            self.assertEqual(len(find_unused(store, window_days=30)), 1)
            store.record_reuse(saved["patch_id"], "t2", helped=True)
            self.assertEqual(find_unused(store, window_days=30), [])
        finally:
            tmp.cleanup()


class ProposalTest(unittest.TestCase):
    def test_merge_proposal_shape(self):
        a = _skill("a", "parse CSV rows into records")
        b = _skill("b", "parse CSV rows into records quickly")
        proposal = propose_merge([a, b])
        self.assertEqual(proposal["kind"], "merge")
        self.assertEqual(sorted(proposal["members"]), ["a", "b"])
        self.assertTrue(proposal["rationale"])
        self.assertIn("parse CSV rows into records",
                      proposal["merged_intent"])
        self.assertEqual(set(("kind", "members", "rationale",
                              "merged_intent")) - set(proposal), set())

    def test_merge_needs_two_candidates(self):
        with self.assertRaises(ValueError):
            propose_merge([_skill("a", "parse CSV rows")])

    def test_generalize_proposal_shape(self):
        a = _skill("a", "parse CSV rows into records",
                   task_family="parsing")
        d = _skill("d", "parse JSON payloads into records",
                   task_family="parsing")
        proposal = propose_generalize({"family": "parsing",
                                       "members": [a, d]})
        self.assertEqual(proposal["kind"], "generalize")
        self.assertEqual(sorted(proposal["members"]), ["a", "d"])
        self.assertTrue(proposal["rationale"])
        self.assertIn("parsing", proposal["merged_intent"])

    def test_generalize_accepts_bare_member_list(self):
        a = _skill("a", "parse CSV rows into records")
        d = _skill("d", "parse JSON payloads into records")
        proposal = propose_generalize([a, d])
        self.assertEqual(proposal["kind"], "generalize")
        self.assertEqual(len(proposal["members"]), 2)


class RehearseTest(unittest.TestCase):
    def _proposal(self):
        return {"kind": "merge", "members": ["a", "b"],
                "rationale": "test", "merged_intent": "parse rows"}

    def test_pipeline_called_exactly_once(self):
        calls = []

        def fake(candidate):
            calls.append(candidate)
            return {"verdict": "pass"}

        result = rehearse_consolidation(self._proposal(), pipeline_fn=fake)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["intent"], "parse rows")
        self.assertEqual(calls[0]["source"], "consolidation")
        self.assertTrue(result["passed"])
        self.assertEqual(result["verdict"], {"verdict": "pass"})

    def test_pipeline_called_once_per_proposal(self):
        calls = []

        def fake(candidate):
            calls.append(candidate)
            return {"passed": len(calls) == 2}

        results = rehearse_consolidation([self._proposal(),
                                          self._proposal()],
                                         pipeline_fn=fake)
        self.assertEqual(len(calls), 2)
        self.assertEqual([r["passed"] for r in results], [False, True])

    def test_failing_verdict_marks_not_passed(self):
        result = rehearse_consolidation(
            self._proposal(), pipeline_fn=lambda c: {"verdict": "fail"})
        self.assertFalse(result["passed"])


class RetireTest(unittest.TestCase):
    def test_retire_marks_and_excludes_from_retrieval(self):
        a = _skill("a", "parse CSV rows into records")
        b = _skill("b", "parse CSV rows into records quickly")
        report = retire([a, b])
        self.assertEqual(report, {"retired": ["a", "b"], "count": 2,
                                  "status": "deprecated"})
        self.assertEqual(a["status"], "deprecated")
        self.assertEqual(b["status"], "deprecated")
        self.assertEqual(retrieval_candidates([a, b]), [])

    def test_retire_subset_keeps_rest_retrievable(self):
        a = _skill("a", "parse CSV rows into records")
        b = _skill("b", "send email receipts")
        retire([a, b], ids=["a"])
        self.assertEqual(a["status"], "deprecated")
        self.assertEqual(b["status"], "live")
        self.assertEqual(retrieval_candidates([a, b]), [b])

    def test_retire_store_persists(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            store = PatchStore(tmp.name)
            saved = store.save_patch("t1", {"code": "x"})
            report = retire(store, ids=[saved["patch_id"]])
            self.assertEqual(report["count"], 1)
            fetched = store.get_patch(saved["patch_id"])
            self.assertEqual(fetched["status"], "deprecated")
            self.assertEqual(retrieval_candidates(store), [])
        finally:
            tmp.cleanup()


class SweepTest(unittest.TestCase):
    def test_expired_swept_only_with_counts(self):
        now = time.time()
        records = [
            _skill("old", "parse CSV rows", created_at=now - 10 * DAY,
                   ttl_days=7),
            _skill("new", "parse CSV rows fast", created_at=now,
                   ttl_days=7),
            _skill("ageless", "parse CSV rows slowly"),
        ]
        report = sweep_expired(records, now=now)
        self.assertEqual(report["swept"], 1)
        self.assertEqual(report["remaining"], 2)
        self.assertEqual(report["expired_ids"], ["old"])
        self.assertEqual([r["id"] for r in records], ["new", "ageless"])

    def test_sweep_delegates_to_patch_store(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            store = PatchStore(tmp.name)
            store.save_patch("t-old", {"code": "old"},
                             created_at=time.time() - 10 * DAY)
            store.save_patch("t-new", {"code": "new"})
            report = sweep_expired(store)
            self.assertEqual(report, {"swept": 1, "remaining": 1})
            self.assertEqual(len(store.list_patches()), 1)
        finally:
            tmp.cleanup()


class RenewTest(unittest.TestCase):
    def test_renewal_refused_without_evidence(self):
        record = _skill("s", "parse CSV rows", created_at=1000.0)
        self.assertFalse(renew_on_evidence(record))
        self.assertFalse(renew_on_evidence(record, evidence=[]))
        self.assertFalse(renew_on_evidence(
            record, evidence=[{"helped": False, "recorded_at": time.time()}]))
        self.assertEqual(record["created_at"], 1000.0)
        self.assertNotIn("renewed_at", record)

    def test_renewal_granted_with_helped_evidence(self):
        now = time.time()
        record = _skill("s", "parse CSV rows", created_at=1000.0)
        granted = renew_on_evidence(
            record, evidence=[{"helped": True, "recorded_at": now}],
            now=now)
        self.assertTrue(granted)
        self.assertEqual(record["created_at"], now)
        self.assertEqual(record["renewed_at"], now)
        self.assertEqual(record["renewals"], 1)

    def test_renewal_uses_store_reuse_log(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            store = PatchStore(tmp.name)
            saved = store.save_patch("t1", {"code": "x"})
            pid = saved["patch_id"]
            self.assertFalse(renew_on_evidence(store, pid))
            store.record_reuse(pid, "t2", helped=True)
            self.assertTrue(renew_on_evidence(store, pid))
        finally:
            tmp.cleanup()


class RetrievalQualityTest(unittest.TestCase):
    def _index_with_retired_decoy(self):
        return [
            _skill("aa-junk", "parse CSV rows into records",
                   status="deprecated"),
            _skill("zz-good", "parse CSV rows into records quickly"),
        ]

    def test_forgetting_improves_rank_quality(self):
        index = self._index_with_retired_decoy()
        queries = [{"text": "parse CSV rows into records",
                    "relevant": ["zz-good"]}]
        report = retrieval_quality_before_after(index, queries)
        self.assertAlmostEqual(report["before"]["mrr"], 0.5)
        self.assertAlmostEqual(report["after"]["mrr"], 1.0)
        self.assertAlmostEqual(report["delta_mrr"], 0.5)
        self.assertGreater(report["delta"], 0)

    def test_explicit_before_after_indexes(self):
        before = self._index_with_retired_decoy()
        after = [r for r in before if r["id"] == "zz-good"]
        report = retrieval_quality_before_after(
            before, after, [("parse CSV rows into records", ["zz-good"])])
        self.assertAlmostEqual(report["before"]["mrr"], 0.5)
        self.assertAlmostEqual(report["after"]["mrr"], 1.0)
        self.assertEqual(report["before"]["n"], 1)

    def test_custom_retrieve_fn_with_scored_pairs(self):
        def fake_rank(query, index):
            return [(r["id"], 1.0) for r in index]

        index = [_skill("a", "parse CSV rows"),
                 _skill("b", "send email", status="deprecated")]
        report = retrieval_quality_before_after(
            index, [{"text": "anything", "relevant": ["a"]}],
            retrieve_fn=fake_rank)
        self.assertAlmostEqual(report["before"]["mrr"], 1.0)
        self.assertAlmostEqual(report["after"]["mrr"], 1.0)
        self.assertAlmostEqual(report["delta_mrr"], 0.0)


if __name__ == "__main__":
    unittest.main()
