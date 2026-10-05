"""Offline tests for the transfer-experiment pilots. Stdlib only.

Covers harness aggregation math on fixtures (delta computation,
condition/mode labeling, block formatting/truncation, offline seeding).
No live model calls.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments import key_experiment, semantic_compare, ttvm_sample


def _record(task_id, passed, usage, injected_chars=0):
    return {
        "summary": {
            "tasks": [{
                "id": task_id,
                "split": "transfer",
                "passed": passed,
                "usage": usage,
            }],
        },
        "memory": {"retrieval": {"injected_chars": injected_chars}},
    }


def _usage(calls, in_tok, out_tok, latency_ms):
    return [
        {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "latency_ms": latency_ms,
            "cost_usd": None,
        }
        for _ in range(calls)
    ]


class ConditionLabelTest(unittest.TestCase):
    def test_four_conditions_labeled(self):
        self.assertEqual(tuple(key_experiment.CONDITIONS), ("A", "B", "C", "D"))
        for cond in key_experiment.CONDITIONS:
            self.assertTrue(key_experiment.CONDITION_LABELS[cond])

    def test_pilot_subset_is_eight_transfer_tasks(self):
        self.assertEqual(len(key_experiment.PILOT_TASK_IDS), 8)
        self.assertEqual(
            list(key_experiment.PILOT_TASK_IDS),
            ["A-TRN-%02d" % n for n in range(1, 9)],
        )

    def test_mode_labels(self):
        self.assertEqual(
            tuple(semantic_compare.MODES), ("code-only", "semantic+code"))
        for mode in semantic_compare.MODES:
            self.assertTrue(semantic_compare.MODE_LABELS[mode])


class SummarizeConditionTest(unittest.TestCase):
    def test_aggregation_math(self):
        records = [
            _record("A-TRN-02", False,
                    _usage(2, 100, 50, 500.0), injected_chars=120),
            _record("A-TRN-01", True,
                    _usage(2, 200, 60, 700.0), injected_chars=80),
        ]
        summary = key_experiment.summarize_condition("C", records)
        self.assertEqual(summary["condition"], "C")
        self.assertEqual(summary["label"],
                         key_experiment.CONDITION_LABELS["C"])
        self.assertEqual(summary["tasks_total"], 2)
        self.assertEqual(summary["tasks_passed"], 1)
        self.assertEqual(summary["success_rate"], 0.5)
        self.assertEqual(summary["first_pass_success_rate"], 0.5)
        self.assertEqual(summary["repair_loops"], 0)
        self.assertEqual(summary["held_out_success_rate"], 0.5)
        self.assertEqual(summary["fail_ids"], ["A-TRN-02"])
        totals = summary["usage_totals"]
        self.assertEqual(totals["calls"], 4)
        self.assertEqual(totals["input_tokens"], 600)
        self.assertEqual(totals["output_tokens"], 220)
        self.assertEqual(totals["total_tokens"], 820)
        means = summary["per_task_mean"]
        self.assertEqual(means["calls"], 2.0)
        self.assertEqual(means["total_tokens"], 410.0)
        self.assertEqual(means["latency_ms"], 1200.0)
        self.assertEqual(summary["mean_injected_chars"], 100.0)

    def test_empty_records(self):
        summary = key_experiment.summarize_condition("A", [])
        self.assertEqual(summary["tasks_total"], 0)
        self.assertEqual(summary["success_rate"], 0.0)
        self.assertEqual(summary["fail_ids"], [])


class ComputeDeltasTest(unittest.TestCase):
    def _summaries(self):
        def summ(passed, total, tokens_per_task, calls=2.0, latency=1000.0):
            return {
                "tasks_passed": passed,
                "success_rate": round(passed / total, 4),
                "per_task_mean": {
                    "total_tokens": tokens_per_task,
                    "calls": calls,
                    "latency_ms": latency,
                },
            }

        return {
            "A": summ(4, 8, 200.0),
            "B": summ(5, 8, 400.0),
            "C": summ(6, 8, 350.0),
            "D": summ(6, 8, 500.0),
        }

    def test_deltas_vs_a(self):
        deltas = key_experiment.compute_deltas(self._summaries(),
                                               baseline="A")
        by_cond = {d["condition"]: d for d in deltas}
        self.assertEqual(set(by_cond), {"B", "C", "D"})
        self.assertEqual(by_cond["B"]["tasks_delta"], 1)
        self.assertEqual(by_cond["B"]["success_rate_delta"], 0.125)
        self.assertEqual(by_cond["B"]["tokens_per_task_delta"], 200.0)
        self.assertEqual(by_cond["C"]["tasks_delta"], 2)
        self.assertEqual(by_cond["C"]["success_rate_delta"], 0.25)
        self.assertEqual(by_cond["D"]["calls_per_task_delta"], 0.0)
        for delta in deltas:
            self.assertEqual(delta["baseline"], "A")

    def test_missing_baseline_yields_no_deltas(self):
        self.assertEqual(
            key_experiment.compute_deltas({"B": {}}, baseline="A"), [])


class BlockBuilderTest(unittest.TestCase):
    def test_condition_a_is_empty(self):
        builder = key_experiment.make_block_builder("A", None, None)
        block, info = builder({"id": "A-TRN-01", "category": "dates"})
        self.assertEqual(block, "")
        self.assertEqual(info["condition"], "A")

    def test_unknown_condition_raises(self):
        builder = key_experiment.make_block_builder("Z", None, None)
        with self.assertRaises(ValueError):
            builder({"id": "A-TRN-01", "category": "dates"})

    def test_lesson_block_empty_and_truncated(self):
        self.assertEqual(key_experiment.format_lesson_block([]), "")
        retrieved = [{
            "id": "L1",
            "statement": "s" * 200,
            "applies_when": {"when": "w" * 200},
        }]
        short = key_experiment.format_lesson_block(retrieved, max_chars=60)
        self.assertLessEqual(len(short), 60)
        self.assertIn("L1", short)

    def test_transcript_block_unknown_category_empty(self):
        self.assertEqual(
            key_experiment.format_transcript_block({"category": "nope"}),
            "")

    def test_transcript_block_truncated(self):
        block = key_experiment.format_transcript_block(
            {"category": "csv"}, max_chars=100)
        self.assertLessEqual(len(block), 100)
        full = key_experiment.format_transcript_block({"category": "csv"})
        self.assertIn("A-EXP-06/traj", full)
        self.assertIn("A-EXP-07/traj", full)

    def test_task_tags(self):
        tags = key_experiment.task_tags({"category": "logs"})
        self.assertIn("logs", tags)
        self.assertIn("family-a", tags)

    def test_semantic_block_empty_and_truncated(self):
        self.assertEqual(semantic_compare.format_semantic_block([]), "")
        families = [{
            "family_id": "f1",
            "intent": "do things",
            "abstract_procedure": ["step " + "x" * 100],
        }]
        short = semantic_compare.format_semantic_block(
            families, max_chars=90)
        self.assertLessEqual(len(short), 90)
        self.assertIn("f1", short)

    def test_semantic_task_tags(self):
        self.assertEqual(
            semantic_compare.semantic_task_tags({"category": "dates"}),
            ["dates", "family-a"])

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            semantic_compare.make_block_builder("Z", None, None)


class OfflineMemoryTest(unittest.TestCase):
    def test_registry_seeds_retrieve_top_k(self):
        registry = key_experiment.build_registry()
        retrieved = registry.retrieve_for_task(
            ["csv", "family-a"], k=key_experiment.LESSONS_TOP_K)
        self.assertLessEqual(len(retrieved), 3)
        self.assertGreater(len(retrieved), 0)
        for lesson in retrieved:
            self.assertEqual(lesson["status"], "active")
            self.assertTrue(lesson["statement"])

    def test_seed_patch_memory_verifies_offline(self):
        exposure = [{
            "id": "A-EXP-05",
            "family": "A",
            "split": "exposure",
            "category": "csv",
            "prompt": "parse csv",
            "checks": [
                {"input": "a\n1", "expected": "X",
                 "compare": "exact"},
                {"input": "b\n2", "expected": "Y",
                 "compare": "exact"},
            ],
        }]
        recorded = {"A-EXP-05": ["X", "WRONG"]}
        with tempfile.TemporaryDirectory() as tmp:
            memory, stored = key_experiment.seed_patch_memory(
                os.path.join(tmp, "patches"), exposure, recorded)
            self.assertEqual(stored, 1)
            found = memory.retrieve({"category": "csv"})
            self.assertEqual(len(found), 1)
            self.assertEqual(
                found[0]["candidate"]["output"], "X")
            block = memory.format_block(found)
            self.assertIn("Procedure 1", block)
            self.assertEqual(memory.format_block([]), "")
            outcome = memory.record_reuse(
                found[0], {"id": "A-TRN-03", "split": "transfer",
                           "category": "csv"}, helped=True)
            self.assertTrue(outcome["helped"])
            self.assertTrue(outcome["held_out"])

    def test_skill_store_pilot_families(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = semantic_compare.build_skill_store(
                os.path.join(tmp, "skills"))
            families = store.list_families()
            self.assertEqual(len(families), 5)
            top = store.find_for_task(["records", "family-a"], limit=1)
            self.assertEqual(len(top), 1)
            self.assertEqual(top[0]["family_id"],
                             "family-a-records-transform")
            top = store.find_for_task(["logs", "family-a"], limit=1)
            self.assertEqual(top[0]["family_id"], "family-a-log-parsing")
            outcome = store.record_outcome(
                "family-a-records-transform", "A-TRN-05", worked=True)
            self.assertTrue(outcome["worked"])
            family = store.get_family("family-a-records-transform")
            self.assertEqual(family["evidence"]["reuse_count"], 1)
            self.assertEqual(family["evidence"]["success_count"], 1)


class SummarizeModeTest(unittest.TestCase):
    def test_mode_math_and_deltas(self):
        code = [
            _record("A-TRN-01", True, _usage(2, 300, 50, 500.0)),
            _record("A-TRN-02", False, _usage(2, 300, 50, 500.0)),
        ]
        sem = [
            _record("A-TRN-01", True, _usage(2, 400, 50, 600.0)),
            _record("A-TRN-02", True, _usage(2, 400, 50, 600.0)),
        ]
        code_summary = semantic_compare.summarize_mode("code-only", code)
        sem_summary = semantic_compare.summarize_mode("semantic+code", sem)
        self.assertEqual(code_summary["tasks_passed"], 1)
        self.assertEqual(sem_summary["tasks_passed"], 2)
        self.assertEqual(code_summary["per_task_mean"]["total_tokens"], 700.0)
        deltas = semantic_compare.compute_mode_deltas(
            {"code-only": code_summary, "semantic+code": sem_summary})
        self.assertEqual(len(deltas), 1)
        delta = deltas[0]
        self.assertEqual(delta["mode"], "semantic+code")
        self.assertEqual(delta["baseline"], "code-only")
        self.assertEqual(delta["tasks_delta"], 1)
        self.assertEqual(delta["success_rate_delta"], 0.5)
        self.assertEqual(delta["tokens_per_task_delta"], 200.0)

    def test_missing_baseline_yields_no_deltas(self):
        self.assertEqual(
            semantic_compare.compute_mode_deltas({}), [])


class SamplerTimingTest(unittest.TestCase):
    """TTVM sampler prefers in-worker candidate_ms (issues.md #77)."""

    def test_prefers_candidate_ms(self):
        evidence = {"direct": {"items": [
            {"elapsed_ms": 900.0, "candidate_ms": 12.5},
            {"elapsed_ms": 800.0, "candidate_ms": 7.5},
        ]}}
        self.assertAlmostEqual(
            ttvm_sample._worker_elapsed_ms(evidence), 20.0)

    def test_falls_back_to_elapsed_ms(self):
        evidence = {"direct": {"items": [
            {"elapsed_ms": 900.0},
            {"elapsed_ms": 800.0, "candidate_ms": None},
            {"elapsed_ms": 700.0, "candidate_ms": True},
        ]}}
        self.assertAlmostEqual(
            ttvm_sample._worker_elapsed_ms(evidence), 2400.0)

    def test_ignores_malformed_evidence(self):
        self.assertEqual(ttvm_sample._worker_elapsed_ms(None), 0.0)
        self.assertEqual(ttvm_sample._worker_elapsed_ms({}), 0.0)
        self.assertEqual(
            ttvm_sample._worker_elapsed_ms({"direct": {"items": [None]}}),
            0.0)


if __name__ == "__main__":
    unittest.main()
