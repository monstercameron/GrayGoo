"""Tests for semantic procedural memory (skills.py). Stdlib only.

Covers: family persist/round-trip, applicability match/reject,
implementation linking, outcome evidence, seed families, and the
comparison harness delta schema on fakes. The live code-only vs
semantic+code experiment itself remains open (todos.md baseline D).
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import skills
from skills import (SkillStore, make_family, match_applicability,
                    seed_default_families, compare_conditions,
                    make_fake_runner)


def _csv_kwargs():
    return dict(
        family_id="csv-test",
        intent="Parse CSV rows.",
        when=["csv", "parsing"],
        when_not=["tsv"],
        abstract_procedure=["Split rows.", "Unescape quotes."],
        contracts=["Row count preserved."],
        known_failure_modes=["ragged-rows"],
    )


class FamilyPersistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SkillStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        saved = self.store.save_family(**_csv_kwargs())
        self.assertEqual(saved["family_id"], "csv-test")
        self.assertEqual(saved["intent"], "Parse CSV rows.")
        self.assertEqual(saved["applicability"]["when"], ["csv", "parsing"])
        self.assertEqual(saved["applicability"]["when_not"], ["tsv"])
        self.assertEqual(len(saved["abstract_procedure"]), 2)
        self.assertEqual(saved["contracts"], ["Row count preserved."])
        self.assertEqual(saved["known_failure_modes"], ["ragged-rows"])
        self.assertEqual(saved["implementation_ids"], [])
        self.assertEqual(saved["evidence"]["reuse_count"], 0)
        self.assertIsNone(saved["evidence"]["success_rate"])

        fetched = self.store.get_family("csv-test")
        self.assertEqual(fetched, saved)

    def test_persist_survives_reopen(self):
        saved = self.store.save_family(**_csv_kwargs())
        reopened = SkillStore(self.tmp.name)
        self.assertEqual(reopened.get_family("csv-test"), saved)

    def test_save_full_record(self):
        record = make_family(**_csv_kwargs())
        saved = self.store.save_family(family=record)
        self.assertEqual(saved["family_id"], "csv-test")
        self.assertEqual(self.store.get_family("csv-test")["intent"],
                         "Parse CSV rows.")

    def test_missing_family_returns_none(self):
        self.assertIsNone(self.store.get_family("no-such-family"))

    def test_delete_family(self):
        self.store.save_family(**_csv_kwargs())
        self.assertTrue(self.store.delete_family("csv-test"))
        self.assertIsNone(self.store.get_family("csv-test"))
        self.assertFalse(self.store.delete_family("csv-test"))

    def test_list_families(self):
        self.store.save_family(**_csv_kwargs())
        self.store.save_family("aaa", "Other.", when=["x"])
        ids = [f["family_id"] for f in self.store.list_families()]
        self.assertEqual(ids, ["aaa", "csv-test"])

    def test_make_family_requires_id_and_intent(self):
        with self.assertRaises(ValueError):
            make_family("", "intent")
        with self.assertRaises(ValueError):
            make_family("id", "")


class ApplicabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SkillStore(self.tmp.name)
        self.store.save_family(**_csv_kwargs())

    def tearDown(self):
        self.tmp.cleanup()

    def test_match_positive_tags(self):
        family = self.store.get_family("csv-test")
        self.assertGreater(match_applicability(family, ["csv"]), 0)
        self.assertGreater(match_applicability(family, ["csv", "parsing"]),
                           match_applicability(family, ["csv"]))

    def test_reject_no_overlap(self):
        family = self.store.get_family("csv-test")
        self.assertEqual(match_applicability(family, ["dates"]), 0.0)
        self.assertEqual(match_applicability(family, []), 0.0)

    def test_reject_when_not(self):
        family = self.store.get_family("csv-test")
        # when_not overlap disqualifies even with when overlap.
        self.assertLess(match_applicability(family, ["csv", "tsv"]), 0)

    def test_accepts_applicability_dict(self):
        self.assertGreater(
            match_applicability({"when": ["a"], "when_not": []}, ["a"]), 0)

    def test_find_for_task_ranks_and_filters(self):
        self.store.save_family("dates", "Normalize dates.",
                               when=["dates"], when_not=[])
        hits = self.store.find_for_task(["csv", "parsing"])
        self.assertEqual([h["family_id"] for h in hits], ["csv-test"])
        # Disqualified families never surface.
        self.assertEqual(self.store.find_for_task(["csv", "tsv"]), [])
        # Dict task form works.
        hits = self.store.find_for_task({"tags": ["dates"]})
        self.assertEqual([h["family_id"] for h in hits], ["dates"])


class LinkingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SkillStore(self.tmp.name)
        self.store.save_family(**_csv_kwargs())

    def tearDown(self):
        self.tmp.cleanup()

    def test_link_implementation(self):
        updated = self.store.link_implementation("csv-test", "patch-abc")
        self.assertEqual(updated["implementation_ids"], ["patch-abc"])
        self.store.link_implementation("csv-test", "cap-xyz")
        fetched = self.store.get_family("csv-test")
        self.assertEqual(fetched["implementation_ids"],
                         ["patch-abc", "cap-xyz"])

    def test_link_dedupes(self):
        self.store.link_implementation("csv-test", "patch-abc")
        updated = self.store.link_implementation("csv-test", "patch-abc")
        self.assertEqual(updated["implementation_ids"], ["patch-abc"])

    def test_link_survives_reopen(self):
        self.store.link_implementation("csv-test", "patch-abc")
        reopened = SkillStore(self.tmp.name)
        self.assertEqual(
            reopened.get_family("csv-test")["implementation_ids"],
            ["patch-abc"])

    def test_link_unknown_family_raises(self):
        with self.assertRaises(KeyError):
            self.store.link_implementation("missing", "patch-abc")

    def test_unlink(self):
        self.store.link_implementation("csv-test", "patch-abc")
        updated = self.store.unlink_implementation("csv-test", "patch-abc")
        self.assertEqual(updated["implementation_ids"], [])


class OutcomeEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SkillStore(self.tmp.name)
        self.store.save_family(**_csv_kwargs())

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_outcome_updates_evidence(self):
        self.store.record_outcome("csv-test", "task-1", worked=True)
        self.store.record_outcome("csv-test", "task-2", worked=False)
        self.store.record_outcome("csv-test", "task-3", worked=True)
        evidence = self.store.get_family("csv-test")["evidence"]
        self.assertEqual(evidence["reuse_count"], 3)
        self.assertEqual(evidence["success_count"], 2)
        self.assertEqual(evidence["failure_count"], 1)
        self.assertAlmostEqual(evidence["success_rate"], 2.0 / 3.0)

    def test_outcome_log_round_trip(self):
        row = self.store.record_outcome("csv-test", "task-1", worked=True)
        self.assertEqual(row["family_id"], "csv-test")
        self.assertEqual(row["task_id"], "task-1")
        self.assertTrue(row["worked"])
        rows = self.store.get_outcomes("csv-test")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], "task-1")

    def test_outcomes_survive_reopen(self):
        self.store.record_outcome("csv-test", "task-1", worked=True)
        reopened = SkillStore(self.tmp.name)
        self.assertEqual(len(reopened.get_outcomes("csv-test")), 1)
        self.assertEqual(
            reopened.get_family("csv-test")["evidence"]["reuse_count"], 1)

    def test_success_alias(self):
        self.store.record_outcome("csv-test", "task-1", success=True)
        evidence = self.store.get_family("csv-test")["evidence"]
        self.assertEqual(evidence["success_count"], 1)

    def test_unknown_family_raises(self):
        with self.assertRaises(KeyError):
            self.store.record_outcome("missing", "task-1", worked=True)


class SeedFamiliesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SkillStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_seeds_with_narrow_applicability(self):
        seeded = seed_default_families(self.store)
        self.assertEqual(len(seeded), 3)
        ids = {f["family_id"] for f in seeded}
        self.assertEqual(ids, {"family-a-csv-parsing",
                               "resilient-cursor-pagination",
                               "family-a-date-normalization"})
        for family in seeded:
            # Every seed carries the full dual-memory shape.
            self.assertTrue(family["intent"])
            self.assertTrue(family["applicability"]["when"])
            # Honest narrow scope: every seed names its exclusions.
            self.assertTrue(family["applicability"]["when_not"])
            self.assertTrue(family["abstract_procedure"])
            self.assertTrue(family["contracts"])
            self.assertTrue(family["known_failure_modes"])
            # No fake links: implementations start empty.
            self.assertEqual(family["implementation_ids"], [])

    def test_seeding_is_idempotent(self):
        seed_default_families(self.store)
        self.store.link_implementation("family-a-csv-parsing", "patch-1")
        seed_default_families(self.store)
        fetched = self.store.get_family("family-a-csv-parsing")
        self.assertEqual(fetched["implementation_ids"], ["patch-1"])
        self.assertEqual(len(self.store.list_families()), 3)

    def test_seeds_match_and_reject(self):
        seed_default_families(self.store)
        hits = self.store.find_for_task(["csv", "family-a"])
        # Both Family A seeds match, but CSV outranks on tag overlap.
        self.assertEqual(hits[0]["family_id"], "family-a-csv-parsing")
        hits = self.store.find_for_task(["csv", "parsing"])
        self.assertEqual([h["family_id"] for h in hits],
                         ["family-a-csv-parsing"])
        # Semicolon-delimited data is explicitly out of scope.
        hits = self.store.find_for_task(["csv", "semicolon-delimited"])
        self.assertNotIn("family-a-csv-parsing",
                         [h["family_id"] for h in hits])


class HarnessTest(unittest.TestCase):
    def test_delta_schema_on_fakes(self):
        tasks = [{"id": "A-EXP-01"}, {"id": "A-EXP-02"}]
        runners = {
            "code-only": make_fake_runner({
                "A-EXP-01": {"success": True, "tokens": 100,
                             "latency_ms": 50.0, "calls": 1},
                "A-EXP-02": {"success": False, "tokens": 200,
                             "latency_ms": 70.0, "calls": 2},
            }),
            "semantic+code": make_fake_runner({
                "A-EXP-01": {"success": True, "tokens": 60,
                             "latency_ms": 40.0, "calls": 1},
                "A-EXP-02": {"success": True, "tokens": 80,
                             "latency_ms": 45.0, "calls": 1},
            }),
        }
        report = compare_conditions(tasks, ["code-only", "semantic+code"],
                                    runners=runners)
        # Schema keys.
        self.assertEqual(set(report.keys()),
                         {"per_task", "summary", "delta"})
        self.assertEqual(len(report["per_task"]), 4)
        self.assertIn("code-only", report["summary"])
        self.assertIn("semantic+code", report["summary"])
        for mode_summary in report["summary"].values():
            self.assertEqual(
                set(mode_summary.keys()),
                {"tasks", "successes", "success_rate", "tokens_total",
                 "tokens_per_task", "latency_ms_total",
                 "latency_ms_per_task", "calls_total", "calls_per_task"})
        # Summary math.
        code = report["summary"]["code-only"]
        sem = report["summary"]["semantic+code"]
        self.assertEqual(code["successes"], 1)
        self.assertAlmostEqual(code["success_rate"], 0.5)
        self.assertEqual(sem["successes"], 2)
        self.assertAlmostEqual(sem["tokens_per_task"], 70.0)
        # Delta table: one row, mode-minus-baseline.
        self.assertEqual(len(report["delta"]), 1)
        row = report["delta"][0]
        self.assertEqual(
            set(row.keys()),
            {"mode", "baseline", "tasks", "success_rate_delta",
             "tokens_per_task_delta", "latency_ms_per_task_delta",
             "calls_per_task_delta"})
        self.assertEqual(row["mode"], "semantic+code")
        self.assertEqual(row["baseline"], "code-only")
        self.assertAlmostEqual(row["success_rate_delta"], 0.5)
        self.assertAlmostEqual(row["tokens_per_task_delta"], 70.0 - 150.0)
        self.assertAlmostEqual(row["latency_ms_per_task_delta"],
                               42.5 - 60.0)
        self.assertAlmostEqual(row["calls_per_task_delta"], 1.0 - 1.5)

    def test_dict_modes_form(self):
        tasks = [{"id": "t1"}]
        report = compare_conditions(
            tasks, {"a": make_fake_runner({"t1": {"success": True}}),
                    "b": make_fake_runner({})})
        self.assertEqual(set(report["summary"].keys()), {"a", "b"})

    def test_missing_runner_raises(self):
        with self.assertRaises(ValueError):
            compare_conditions([{"id": "t1"}], ["a", "b"],
                               runners={"a": make_fake_runner({})})

    def test_harness_module_documents_open_experiment(self):
        self.assertIn("HARNESS ONLY", skills.__doc__)
        self.assertIn("live runs", skills.__doc__)


if __name__ == "__main__":
    unittest.main()
