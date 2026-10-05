"""Tests for the external hidden-test evaluator (stdlib only, offline).

Covers: the mandatory gate over the real subprocess boundary (correct
outputs pass, wrong/missing outputs fail), transform semantics
preservation across every transform type, hidden-corpus self-consistency,
and strict rejection of malformed protocol requests.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluator import protocol, service, transforms

CSV_PARAMS = {"columns": {"name": "name", "city": "city", "note": "note"}}
KV_PARAMS = {"ignore": ["trace"]}


def seed_cases():
    return {
        "flatten": transforms.make_case(
            "flatten",
            '{"alpha": {"beta": 1, "gamma": [1000, 2000]}, "zed": "x"}'),
        "csv_select": transforms.make_case(
            "csv_select",
            'name,city,note\n"Doe, Jane",Berlin,"likes cats"\nSmith,Paris,hi',
            params=dict(CSV_PARAMS)),
        "kv_parse": transforms.make_case(
            "kv_parse",
            'trace=t-1 level=error user=bob msg="disk full"',
            params=dict(KV_PARAMS)),
        "dates": transforms.make_case(
            "dates",
            "Record 42: 5 Oct 2026 (verified)\nRecord 43: 2026/10/06 (pending)"),
    }


def all_correct_outputs(cases=None):
    outputs = {}
    for case in cases or service.load_hidden_cases():
        for i, check in enumerate(case["checks"]):
            outputs["%s:%d" % (case["id"], i)] = check["expected"]
    return outputs


class GateOverProcessBoundaryTest(unittest.TestCase):
    def test_correct_outputs_pass_over_process_boundary(self):
        request = protocol.make_request("cand-good", all_correct_outputs(),
                                        request_id="req-pass")
        envelope = service.evaluate_in_subprocess(request)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["request_id"], "req-pass")
        self.assertEqual(envelope["candidate_id"], "cand-good")
        self.assertEqual(envelope["verdict"], "pass")
        summary = envelope["evidence"]["summary"]
        self.assertEqual(summary["total_cases"], 4)
        self.assertEqual(summary["passed_cases"], 4)
        self.assertEqual(summary["total_checks"], 8)
        self.assertEqual(summary["passed_checks"], 8)
        self.assertEqual(summary["case_pass_rate"], 1.0)
        self.assertEqual(summary["check_pass_rate"], 1.0)
        self.assertEqual(
            envelope["evidence"]["thresholds"], service.DEFAULT_THRESHOLDS)
        for case in envelope["evidence"]["cases"]:
            self.assertTrue(case["passed"], case["id"])
        self.assertEqual(envelope["evidence"]["unexpected_outputs"], [])

    def test_wrong_output_fails_with_per_case_evidence(self):
        outputs = all_correct_outputs()
        outputs["HID-A-02:0"] = "{}"
        request = protocol.make_request("cand-bad", outputs,
                                        request_id="req-fail")
        envelope = service.evaluate_in_subprocess(request)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["verdict"], "fail")
        by_id = {c["id"]: c for c in envelope["evidence"]["cases"]}
        self.assertFalse(by_id["HID-A-02"]["passed"])
        self.assertTrue(by_id["HID-A-01"]["passed"])
        failing = [c for c in by_id["HID-A-02"]["checks"] if not c["passed"]]
        self.assertEqual(len(failing), 1)
        self.assertEqual(failing[0]["index"], 0)
        self.assertEqual(failing[0]["error"], "mismatch")
        summary = envelope["evidence"]["summary"]
        self.assertEqual(summary["passed_checks"], 7)

    def test_missing_output_fails_that_check(self):
        outputs = all_correct_outputs()
        del outputs["HID-A-03:1"]
        envelope = service.evaluate_in_subprocess(
            protocol.make_request("cand-missing", outputs))
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["verdict"], "fail")
        by_id = {c["id"]: c for c in envelope["evidence"]["cases"]}
        self.assertFalse(by_id["HID-A-03"]["passed"])
        missing = [c for c in by_id["HID-A-03"]["checks"]
                   if c["index"] == 1][0]
        self.assertFalse(missing["passed"])
        self.assertEqual(missing["error"], "missing_output")

    def test_json_compare_ignores_key_order(self):
        outputs = all_correct_outputs()
        parsed = json.loads(outputs["HID-A-04:0"])
        outputs["HID-A-04:0"] = json.dumps(
            {k: parsed[k] for k in reversed(list(parsed))})
        envelope = service.evaluate_in_subprocess(
            protocol.make_request("cand-reorder", outputs))
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["verdict"], "pass")

    def test_unexpected_output_keys_are_reported_not_scored(self):
        outputs = all_correct_outputs()
        outputs["HID-A-99:0"] = "whatever"
        envelope = service.evaluate_in_subprocess(
            protocol.make_request("cand-extra", outputs))
        self.assertEqual(envelope["verdict"], "pass")
        self.assertEqual(envelope["evidence"]["unexpected_outputs"],
                         ["HID-A-99:0"])

    def test_relaxed_thresholds_are_applied_and_echoed(self):
        outputs = all_correct_outputs()
        outputs["HID-A-01:0"] = "nope"
        request = protocol.make_request(
            "cand-lax", outputs,
            thresholds={"min_case_pass_rate": 0.75,
                        "min_check_pass_rate": 0.875})
        envelope = service.evaluate_in_subprocess(request)
        self.assertEqual(envelope["verdict"], "pass")
        self.assertEqual(envelope["evidence"]["thresholds"],
                         {"min_case_pass_rate": 0.75,
                          "min_check_pass_rate": 0.875})
        strict = service.evaluate_in_subprocess(
            protocol.make_request("cand-lax", outputs))
        self.assertEqual(strict["verdict"], "fail")


class EvaluateShapeTest(unittest.TestCase):
    def test_result_is_single_mandatory_gate(self):
        result = service.evaluate("c1", all_correct_outputs())
        self.assertEqual(set(result), {"verdict", "evidence"})
        self.assertIn(result["verdict"], ("pass", "fail"))
        self.assertIn("cases", result["evidence"])
        self.assertIn("thresholds", result["evidence"])
        self.assertIn("summary", result["evidence"])

    def test_garbage_json_output_reports_invalid_json(self):
        outputs = all_correct_outputs()
        outputs["HID-A-04:1"] = "not { json"
        result = service.evaluate("c3", outputs)
        self.assertEqual(result["verdict"], "fail")
        check = [c for c in result["evidence"]["cases"][3]["checks"]
                 if c["index"] == 1][0]
        self.assertEqual(check["error"], "invalid_json_output")

    def test_non_string_output_fails_check_not_request(self):
        outputs = all_correct_outputs()
        outputs["HID-A-01:0"] = ["not", "text"]
        result = service.evaluate("c2", outputs)
        self.assertEqual(result["verdict"], "fail")
        check = [c for c in result["evidence"]["cases"][0]["checks"]
                 if c["index"] == 0][0]
        self.assertEqual(check["error"], "non_string_output")


class TransformSemanticsTest(unittest.TestCase):
    def assertPreserved(self, original, transformed):
        self.assertTrue(transforms.check_case(transformed),
                        "solver no longer maps input -> expected after %s"
                        % transformed["transforms_applied"])
        self.assertTrue(transformed["transforms_applied"])

    def test_rename_identifiers_kv(self):
        case = seed_cases()["kv_parse"]
        new = transforms.rename_identifiers(
            case, {"level": "severity", "user": "account"})
        self.assertPreserved(case, new)
        self.assertNotEqual(new["input"], case["input"])
        self.assertNotEqual(new["expected"], case["expected"])
        self.assertIn('"severity": "error"', new["expected"])

    def test_rename_identifiers_csv(self):
        case = seed_cases()["csv_select"]
        new = transforms.rename_identifiers(
            case, {"name": "full_name", "city": "town"})
        self.assertPreserved(case, new)
        self.assertEqual(new["expected"], case["expected"])
        self.assertEqual(new["params"]["columns"],
                         {"full_name": "name", "town": "city",
                          "note": "note"})

    def test_rename_identifiers_flatten(self):
        case = seed_cases()["flatten"]
        new = transforms.rename_identifiers(
            case, {"alpha": "a1", "gamma": "g2", "beta": "b3"})
        self.assertPreserved(case, new)
        self.assertIn("a1.g2[0]", new["expected"])

    def test_permute_ordering_all_kinds(self):
        for kind, case in seed_cases().items():
            with self.subTest(kind=kind):
                # Fixed seed must always preserve semantics ...
                self.assertPreserved(
                    case, transforms.permute_ordering(case, seed=7))
                # ... and some seed must actually reorder the input.
                differed = [
                    transforms.permute_ordering(case, seed=seed)
                    for seed in range(20)
                    if transforms.permute_ordering(
                        case, seed=seed)["input"] != case["input"]]
                self.assertTrue(differed,
                                "permute never reordered %s" % kind)
                self.assertPreserved(case, differed[0])

    def test_rescale_values(self):
        case = seed_cases()["flatten"]
        new = transforms.rescale_values(case, factor=100)
        self.assertPreserved(case, new)
        self.assertIn("100000", new["expected"])
        with self.assertRaises(ValueError):
            transforms.rescale_values(seed_cases()["kv_parse"])

    def test_inject_irrelevant_fields_all_kinds(self):
        for kind, case in seed_cases().items():
            with self.subTest(kind=kind):
                new = transforms.inject_irrelevant_fields(
                    case, seed=11, count=2)
                self.assertPreserved(case, new)
                self.assertNotEqual(new["input"], case["input"])

    def test_change_formatting_all_kinds(self):
        for kind, case in seed_cases().items():
            with self.subTest(kind=kind):
                new = transforms.change_formatting(case, seed=3)
                self.assertPreserved(case, new)
                self.assertEqual(new["expected"], case["expected"])

    def test_new_boundary_examples_solver_checked(self):
        params = {"csv_select": dict(CSV_PARAMS),
                  "kv_parse": dict(KV_PARAMS)}
        for kind in ("flatten", "csv_select", "kv_parse", "dates"):
            with self.subTest(kind=kind):
                made = transforms.new_boundary_examples(
                    kind, seed=1, count=4, params=params.get(kind))
                self.assertEqual(len(made), 4)
                for case in made:
                    self.assertTrue(transforms.check_case(case),
                                    "bad boundary answer for %r" % case["input"])
                    self.assertEqual(case["transforms_applied"],
                                     ["new_boundary_example"])

    def test_transform_pipeline_stays_consistent(self):
        case = seed_cases()["csv_select"]
        new = transforms.apply_transforms(case, [
            ("permute_ordering", {"seed": 5}),
            ("inject_irrelevant_fields", {"seed": 5, "count": 1}),
            ("change_formatting", {"seed": 5}),
        ])
        self.assertPreserved(case, new)
        self.assertEqual(len(new["transforms_applied"]), 3)
        with self.assertRaises(ValueError):
            transforms.apply_transforms(case, [("nope", {})])

    def test_transforms_do_not_mutate_inputs(self):
        case = seed_cases()["flatten"]
        snapshot = json.dumps(case, sort_keys=True)
        transforms.rename_identifiers(case, {"zed": "z"})
        transforms.permute_ordering(case, seed=1)
        transforms.rescale_values(case)
        transforms.inject_irrelevant_fields(case)
        transforms.change_formatting(case)
        self.assertEqual(json.dumps(case, sort_keys=True), snapshot)


class HiddenCorpusTest(unittest.TestCase):
    def test_corpus_loads_and_is_self_consistent(self):
        cases = service.load_hidden_cases()
        self.assertGreaterEqual(len(cases), 4)
        ids = [c["id"] for c in cases]
        self.assertEqual(len(ids), len(set(ids)))
        for case in cases:
            for check in case["checks"]:
                rebuilt = {"kind": case["kind"], "input": check["input"],
                           "expected": check["expected"],
                           "compare": check["compare"],
                           "params": case.get("params", {})}
                self.assertTrue(
                    transforms.check_case(rebuilt),
                    "hidden %s answer disagrees with reference solver"
                    % case["id"])

    def test_corpus_lives_outside_benchmarks(self):
        repo_root = os.path.dirname(os.path.dirname(
            os.path.abspath(service.CASES_PATH)))
        self.assertTrue(
            os.path.abspath(service.CASES_PATH).startswith(
                os.path.join(repo_root, "evaluator") + os.sep))
        self.assertFalse(os.path.exists(
            os.path.join(repo_root, "benchmarks", "hidden_cases.json")))


class MalformedProtocolTest(unittest.TestCase):
    def test_validate_request_rejects_shapes(self):
        bad = [
            [], "x", 42, None,
            {},  # missing everything
            {"request_id": "r", "candidate_id": "c"},  # missing outputs
            {"request_id": "r", "candidate_id": "c", "outputs": {},
             "bogus": 1},  # unknown key
            {"request_id": "", "candidate_id": "c", "outputs": {}},
            {"request_id": "r", "candidate_id": "", "outputs": {}},
            {"request_id": "r", "candidate_id": "c", "outputs": []},
            {"request_id": "r", "candidate_id": "c", "outputs": {},
             "protocol_version": "9.9"},
            {"request_id": "r", "candidate_id": "c", "outputs": {},
             "thresholds": {"min_case_pass_rate": 2.0}},
            {"request_id": "r", "candidate_id": "c", "outputs": {},
             "thresholds": {"nope": 0.5}},
            {"request_id": "r", "candidate_id": "c", "outputs": {},
             "thresholds": []},
        ]
        for obj in bad:
            with self.subTest(obj=obj):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_request(obj)

    def test_subprocess_rejects_garbage_and_bad_schema(self):
        for raw, code in (("this is not json", "bad_json"),
                          (json.dumps({"request_id": "r"}), "bad_request"),
                          ("", "bad_json")):
            proc = subprocess.run(
                [sys.executable, service.SERVICE_PATH],
                input=raw, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0, raw)
            envelope = json.loads(proc.stdout)
            self.assertFalse(envelope["ok"], raw)
            self.assertNotIn("verdict", envelope, raw)
            self.assertEqual(envelope["error"]["code"], code, raw)


class FreshCasesTest(unittest.TestCase):
    def test_generation_is_seeded_and_deterministic(self):
        cases = service.load_hidden_cases()
        first = service.generate_fresh_cases(cases, seed=3, per_case=1)
        second = service.generate_fresh_cases(cases, seed=3, per_case=1)
        self.assertTrue(first)
        self.assertEqual(first, second)
        other = service.generate_fresh_cases(cases, seed=4, per_case=1)
        self.assertNotEqual(
            [c["checks"][0]["input"] for c in first],
            [c["checks"][0]["input"] for c in other])

    def test_fresh_cases_validate(self):
        cases = service.load_hidden_cases()
        fresh = service.generate_fresh_cases(cases, seed=0, per_case=2)
        self.assertTrue(fresh)
        for case in fresh:
            self.assertTrue(case["id"].startswith("fresh:"))
            self.assertTrue(transforms.check_case({
                "kind": case["kind"],
                "input": case["checks"][0]["input"],
                "expected": case["checks"][0]["expected"],
                "compare": case["checks"][0]["compare"],
                "params": case["params"]}), case["id"])

    def test_evaluate_with_fresh_scores_provided_outputs(self):
        cases = service.load_hidden_cases()
        fresh = service.generate_fresh_cases(cases, seed=1, per_case=1)
        outputs = {}
        for case in list(cases) + fresh:
            for index, check in enumerate(case["checks"]):
                outputs["%s:%d" % (case["id"], index)] = check["expected"]
        result = service.evaluate("c1", outputs, cases=cases,
                                  fresh={"seed": 1, "per_case": 1})
        self.assertEqual(result["verdict"], "pass", result["evidence"])
        self.assertEqual(result["evidence"]["fresh"]["generated"],
                         len(fresh))

    def test_fresh_wire_field_round_trip(self):
        request = protocol.make_request("c1", {}, fresh={"seed": 2})
        validated = protocol.validate_request(request)
        self.assertEqual(validated["fresh"], {"seed": 2})
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(
                protocol.make_request("c1", {}, fresh={"seed": -1}))
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request(
                protocol.make_request("c1", {}, fresh={"bogus": 1}))


if __name__ == "__main__":
    unittest.main()
