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

_BENCH = Path(__file__).resolve().parent.parent / "benchmarks"
_FAMILY_A = _BENCH / "family-a"
_FAMILY_W = _BENCH / "family-w"


def _load_tasks(name):
    with open(_FAMILY_A / name, encoding="utf-8") as fh:
        return {task["id"]: task for task in json.load(fh)}


def _load_w_tasks(name):
    with open(_FAMILY_W / name, encoding="utf-8") as fh:
        return {task["id"]: task for task in json.load(fh)}


class SeedVerificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exposure = _load_tasks("exposure.json")
        cls.w_exposure = _load_w_tasks("exposure.json")

    def test_each_seed_passes_its_source_task(self):
        registry = execaps.ExecRegistry()
        for cap in registry._caps.values():
            if cap.source_task.startswith("SYNTHETIC:"):
                continue
            with self.subTest(cap=cap.id):
                pool = (self.w_exposure
                        if cap.source_task.startswith("W-EXP-")
                        else self.exposure)
                task = pool[cap.source_task]
                passed, failed = execaps.verify_capability(cap, task)
                self.assertTrue(passed, "failures: %r" % (failed,))

    def test_table_csv_passes_synthetic_checks(self):
        registry = execaps.ExecRegistry()
        cap = registry.get("cap-table-csv")
        task = {"checks": execaps.SYNTHETIC_CHECKS["cap-table-csv"]}
        passed, failed = execaps.verify_capability(cap, task)
        self.assertTrue(passed, "failures: %r" % (failed,))

    def test_registry_starts_with_thirteen_caps_empty_plan_cache(self):
        registry = execaps.ExecRegistry()
        self.assertEqual(len(registry._caps), 13)
        self.assertEqual(len(registry._comps), 0)
        self.assertEqual(registry.capability_count(), 13)


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


class SigMatchTest(unittest.TestCase):
    def test_exact_hits(self):
        hits = execaps.sig_word_hits(
            ("dedupe", "rows"), {"dedup", "duplicate", "rows"})
        self.assertEqual(hits["rows"], "rows")

    def test_inflection_hits(self):
        # Learned signatures inflect exposure wording; retrieval must
        # still fire on transfer phrasing (W-CMP-01 regression:
        # 'dedupe'/'duplicates' vs prompt 'dedup'/'duplicate').
        hits = execaps.sig_word_hits(
            ("dedupe", "duplicates", "rows"),
            {"dedup", "duplicate", "rows"})
        self.assertEqual(hits["dedupe"], "dedup")
        self.assertEqual(hits["duplicates"], "duplicate")
        self.assertEqual(hits["rows"], "rows")

    def test_short_stems_do_not_hit(self):
        hits = execaps.sig_word_hits(
            ("to", "token", "in", "input"), {"token", "input"})
        self.assertEqual(hits["to"], None)
        self.assertEqual(hits["in"], None)
        self.assertEqual(hits["token"], "token")

    def test_overlap_fraction(self):
        self.assertAlmostEqual(
            execaps.sig_overlap(
                ("dedupe", "duplicates", "exact", "first", "keep",
                 "rows"),
                {"paginate", "collect", "dedup", "duplicate", "rows"}),
            0.5)

    def test_synthesis_gate_matches_runtime(self):
        # distill's gate must score what the runtime scores, or caps
        # pass synthesis yet never fire (or vice versa).
        import distill
        task = {"prompt": "Paginate across pages to collect items, "
                          "then dedup duplicate rows. "
                          "Output a JSON array."}
        sig = ("dedupe", "duplicates", "exact", "first", "keep",
               "rows")
        self.assertAlmostEqual(
            distill.sig_prompt_overlap(sig, task),
            execaps.sig_overlap(sig, execaps._prompt_words(task)))


class CompositionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = execaps.ExecRegistry()

    def test_composition_requires_two_uses(self):
        with self.assertRaises(AssertionError):
            execaps.Composition("bad", "single use", ["cap-date-iso"],
                                lambda r, i: i, "dates", ("date",),
                                ("a",), ("b",))

    def test_search_discovers_map_plan(self):
        task = {"id": "R-CMP-01", "family": "R", "split": "transfer",
                "category": "csv",
                "prompt": "Parse the input as CSV with a header row, then "
                          "normalize the date column values to ISO format. "
                          "Output the rows as a JSON array."}
        check_input = "name,date\nann,5 Oct 2026\nbob,2026/10/06"
        plans = execaps.search_compositions(
            self.registry, task, check_input)
        self.assertTrue(plans)
        plan = plans[0]
        self.assertEqual(plan.uses, ["cap-csv-parse", "cap-date-iso"])
        self.assertEqual(plan.executed_count, 2)
        self.assertEqual(
            plan.id, "map:cap-csv-parse[date]>cap-date-iso")
        self.assertEqual(
            json.loads(plan.execute(self.registry, check_input)),
            [{"name": "ann", "date": "2026-10-05"},
             {"name": "bob", "date": "2026-10-06"}])

    def test_search_discovers_seq_plan_with_glue(self):
        task = {"id": "R-CMP-02", "family": "R", "split": "transfer",
                "category": "records",
                "prompt": "Flatten the input JSON object, then serialize "
                          "the flat object as CSV rows: a header row "
                          "'key,value' followed by one row per entry. "
                          "Output only the CSV."}
        check_input = '{"a": {"b": 1}, "c": "x"}'
        plans = execaps.search_compositions(
            self.registry, task, check_input)
        self.assertTrue(plans)
        plan = plans[0]
        self.assertEqual(plan.uses, ["cap-flatten", "cap-table-csv"])
        self.assertEqual(
            plan.id, "seq:cap-flatten>flat_to_rows>cap-table-csv")
        self.assertEqual(plan.execute(self.registry, check_input),
                         "key,value\na.b,1\nc,x")

    def test_search_rejects_plain_csv_task(self):
        # The csv->csv roundtrip chain trial-succeeds but its final
        # output type cannot satisfy the JSON demand: output-type
        # agreement rejects it before it can shadow real plans.
        task = {"id": "A-EXP-05", "family": "A", "split": "exposure",
                "category": "csv",
                "prompt": "Parse the input as CSV (RFC 4180: comma "
                          "delimiter, double-quote quoting, first row is "
                          "the header). Output a JSON array of objects."}
        self.assertEqual(execaps.search_compositions(
            self.registry, task, "a,b\n1,2"), [])

    def test_search_rejects_log_task_with_weak_second_step(self):
        # SEQ(clf-parse, flatten) trial-succeeds, but the prompt shows
        # no flatten evidence: per-step overlap gates it out.
        task = {"id": "A-EXP-13", "family": "A", "split": "exposure",
                "category": "logs",
                "prompt": "Parse one Common Log Format line. Output JSON "
                          "with host, user, time, method, path, status."}
        line = ('127.0.0.1 - frank [10/Oct/2000:13:55:36 -0700] '
                '"GET /a.gif HTTP/1.0" 200 2326')
        self.assertEqual(execaps.search_compositions(
            self.registry, task, line), [])

    def test_search_finds_no_plans_on_single_procedure_tasks(self):
        # Every exposure check is solvable by exactly one capability:
        # search must return [] on all of them, so arm D never
        # shadows REUSE with a spurious plan. Regression: a lossy
        # flatten->csv->parse roundtrip once passed the gates on
        # A-EXP-09 by matching the coarse "JSON" demand.
        exposure = _load_tasks("exposure.json")
        w_exposure = _load_w_tasks("exposure.json")
        registry = execaps.ExecRegistry()
        for task_id in ("A-EXP-01", "A-EXP-05", "A-EXP-09", "A-EXP-13"):
            task = exposure[task_id]
            for check in task["checks"]:
                with self.subTest(task=task_id):
                    self.assertEqual(execaps.search_compositions(
                        registry, task, check["input"]), [])
        for task_id in sorted(w_exposure):
            task = w_exposure[task_id]
            for check in task["checks"]:
                with self.subTest(task=task_id):
                    self.assertEqual(execaps.search_compositions(
                        registry, task, check["input"]), [])

    def test_search_finds_no_plans_on_novelty_task(self):
        # W-NOV-01 shares only generic words ("json", "array") with
        # the csv roundtrip steps: joint coverage must reject the
        # chain so the task routes to NOVEL instead of a spurious
        # COMPOSE plan with an empty trial output.
        task = _load_w_tasks("novel.json")["W-NOV-01"]
        for check in task["checks"]:
            with self.subTest(check=check["input"][:40]):
                self.assertEqual(execaps.search_compositions(
                    self.registry, task, check["input"]), [])

    def test_search_is_deterministic(self):
        task = {"id": "R-CMP-01", "family": "R", "split": "transfer",
                "category": "csv",
                "prompt": "Parse the input as CSV with a header row, then "
                          "normalize the date column values to ISO format. "
                          "Output the rows as a JSON array."}
        check_input = "name,date\nann,5 Oct 2026"
        first = [p.id for p in execaps.search_compositions(
            self.registry, task, check_input)]
        second = [p.id for p in execaps.search_compositions(
            self.registry, task, check_input)]
        self.assertEqual(first, second)
        self.assertTrue(first)

    def test_low_mean_chain_still_discovered(self):
        # Mean step overlap is a RANKING signal, never a gate: a chain
        # whose every step clears the per-step floor must be found
        # even when the mean sits below the old 0.5 bar (learned-SIG
        # regression: exposure filler words dilute transfer means).
        first = execaps.ExecCapability(
            "cap-alpha", "alpha step", "demo",
            ("text-a",), ("text-b",),
            lambda text: "mid:" + text, lambda text: True,
            ("alpha", "gather", "filler", "words", "here", "now"),
            "SYNTHETIC")
        second = execaps.ExecCapability(
            "cap-beta", "beta step", "demo",
            ("text-b",), ("text-c",),
            lambda text: "out:" + text, lambda text: True,
            ("beta", "finish", "extra", "filler", "terms", "kept"),
            "SYNTHETIC")
        registry = execaps.ExecRegistry(
            capabilities=[first, second])
        task = {"id": "SYN-CMP", "family": "S", "split": "transfer",
                "category": "demo",
                "prompt": "Alpha gather the records, then beta finish "
                          "them. Return the finished bundle."}
        plans = execaps.search_compositions(registry, task, "raw")
        self.assertEqual([p.id for p in plans],
                         ["seq:cap-alpha>cap-beta"])
        self.assertEqual(plans[0].execute(registry, "raw"),
                         "out:mid:raw")

    def test_find_composition_memoizes_winner(self):
        registry = execaps.ExecRegistry()
        self.assertEqual(len(registry._comps), 0)
        task = {"id": "R-CMP-01", "family": "R", "split": "transfer",
                "category": "csv",
                "prompt": "Parse the input as CSV with a header row, then "
                          "normalize the date column values to ISO format. "
                          "Output the rows as a JSON array."}
        check_input = "name,date\nann,5 Oct 2026"
        comp = registry.find_composition(task, check_input)
        self.assertIsNotNone(comp)
        self.assertEqual(comp.id, "map:cap-csv-parse[date]>cap-date-iso")
        self.assertEqual(list(registry._comps), [comp.id])
        # Same plan object serves the second check (stable evidence).
        again = registry.find_composition(task, check_input)
        self.assertIs(again, comp)


if __name__ == "__main__":
    unittest.main()
