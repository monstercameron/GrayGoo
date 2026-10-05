"""Tests for execaps.py: executable capabilities + applicability guard.

Seed capabilities MUST pass their source exposure tasks -- otherwise
the C/D reuse experiment would be rigged. Applicability tests pin the
harmful-reuse guard: same-category but wrong-procedure inputs must NOT
fire (e.g. comma-CSV parser on semicolon input, ISO-normalizer on
ISO-week input).
"""

import json
import unittest
from pathlib import Path

import execaps

_FAMILY_A = Path(__file__).resolve().parent.parent / "benchmarks" / "family-a"


def _load_tasks(name):
    with open(_FAMILY_A / name, encoding="utf-8") as fh:
        return {task["id"]: task for task in json.load(fh)}


class SeedVerificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exposure = _load_tasks("exposure.json")

    def test_each_seed_passes_its_source_task(self):
        registry = execaps.ExecRegistry()
        for cap in registry._caps.values():
            if cap.source_task.startswith("SYNTHETIC:"):
                continue
            with self.subTest(cap=cap.id):
                task = self.exposure[cap.source_task]
                passed, failed = execaps.verify_capability(cap, task)
                self.assertTrue(passed, "failures: %r" % (failed,))

    def test_table_csv_passes_synthetic_checks(self):
        registry = execaps.ExecRegistry()
        cap = registry.get("cap-table-csv")
        task = {"checks": execaps.SYNTHETIC_CHECKS["cap-table-csv"]}
        passed, failed = execaps.verify_capability(cap, task)
        self.assertTrue(passed, "failures: %r" % (failed,))

    def test_registry_counts_five_caps_two_comps(self):
        registry = execaps.ExecRegistry()
        self.assertEqual(len(registry._caps), 5)
        self.assertEqual(len(registry._comps), 2)
        self.assertEqual(registry.capability_count(), 7)


class ApplicabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exposure = _load_tasks("exposure.json")
        cls.transfer = _load_tasks("transfer.json")
        cls.registry = execaps.ExecRegistry()

    def test_csv_parser_fires_on_rfc4180(self):
        cap = self.registry.get("cap-csv-parse")
        task = self.exposure["A-EXP-05"]
        self.assertTrue(cap.applies_to(task, task["checks"][0]["input"]))

    def test_csv_parser_rejects_semicolon_csv(self):
        # A-TRN-03 is same-category (csv) but a different procedure:
        # the comma parser must NOT claim it (harmful-reuse guard).
        cap = self.registry.get("cap-csv-parse")
        task = self.transfer["A-TRN-03"]
        for check in task["checks"]:
            self.assertFalse(cap.applies_to(task, check["input"]))

    def test_date_cap_rejects_iso_week_dates(self):
        # A-TRN-02 (ISO week 'YYYY-Www-d') is not normalizable by the
        # four-format date capability.
        cap = self.registry.get("cap-date-iso")
        task = self.transfer["A-TRN-02"]
        for check in task["checks"]:
            self.assertFalse(cap.applies_to(task, check["input"]))

    def test_wrong_category_never_applies(self):
        cap = self.registry.get("cap-flatten")
        task = dict(self.exposure["A-EXP-01"])
        self.assertFalse(
            cap.applies_to(task, task["checks"][0]["input"]))

    def test_find_for_task_selects_date_cap(self):
        task = self.exposure["A-EXP-01"]
        found = self.registry.find_for_task(
            task, task["checks"][0]["input"])
        self.assertEqual([cap.id for cap in found], ["cap-date-iso"])

    def test_find_for_task_empty_on_foreign_procedure(self):
        task = self.transfer["A-TRN-02"]
        found = self.registry.find_for_task(
            task, task["checks"][0]["input"])
        self.assertEqual(found, [])


class CompositionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = execaps.ExecRegistry()

    def test_composition_requires_two_uses(self):
        with self.assertRaises(AssertionError):
            execaps.Composition("bad", "single use", ["cap-date-iso"],
                                lambda r, i: i, "dates", ("date",),
                                ("a",), ("b",))

    def test_csv_date_iso_runs_both_capabilities(self):
        comp = self.registry._comps["cmp-csv-date-iso"]
        self.assertEqual(comp.executed_count, 2)
        actual = comp.execute(
            self.registry, "name,date\nann,5 Oct 2026\nbob,2026/10/06")
        self.assertEqual(json.loads(actual),
                         [{"name": "ann", "date": "2026-10-05"},
                          {"name": "bob", "date": "2026-10-06"}])

    def test_flat_to_csv_runs_both_capabilities(self):
        comp = self.registry._comps["cmp-flat-to-csv"]
        self.assertEqual(comp.executed_count, 2)
        actual = comp.execute(self.registry, '{"a": {"b": 1}, "c": "x"}')
        self.assertEqual(actual, "key,value\na.b,1\nc,x")

    def test_find_composition_matches_compose_task(self):
        task = {"id": "R-CMP-01", "family": "R", "split": "transfer",
                "category": "csv",
                "prompt": "Parse the input as CSV with a header row, then "
                          "normalize the date column values to ISO format. "
                          "Output the rows as a JSON array."}
        check_input = "name,date\nann,5 Oct 2026"
        comp = self.registry.find_composition(task, check_input)
        self.assertIsNotNone(comp)
        self.assertEqual(comp.id, "cmp-csv-date-iso")

    def test_find_composition_rejects_plain_csv_task(self):
        # A plain parse task must not trigger the date composition:
        # prompt overlap with the composition signature is too low.
        task = {"id": "A-EXP-05", "family": "A", "split": "exposure",
                "category": "csv",
                "prompt": "Parse the input as CSV (RFC 4180: comma "
                          "delimiter, double-quote quoting, first row is "
                          "the header). Output a JSON array of objects."}
        comp = self.registry.find_composition(
            task, "a,b\n1,2")
        self.assertIsNone(comp)


if __name__ == "__main__":
    unittest.main()
