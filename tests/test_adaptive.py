"""Tests for the adaptive development loop (adaptive.py, learning L6).

Stdlib only (unittest + tempfiles). Each learner test plants a pattern
in a synthetic ledger/history, checks the learner extracts it, and
checks empty/sparse inputs degrade to documented defaults instead of
crashing or reporting extreme estimates.
"""

import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import adaptive
import events
import trajectories


def _open_ledger(testcase):
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    ledger = events.EventLedger(os.path.join(tmp.name, "ledger.db"))
    testcase.addCleanup(ledger.close)
    return ledger


def _plant_trajectory(ledger, task_id, context, success):
    candidate = {"id": "cand-%s" % task_id, "context": dict(context)}
    trajectories.record_trajectory(
        ledger, task_id,
        [{"candidate": candidate, "failure": None, "repair": None,
          "success": bool(success)}],
        family="adaptive-test")


class ContextWeightsTest(unittest.TestCase):
    def test_contract_pattern_extracted(self):
        ledger = _open_ledger(self)
        # Contract-present runs succeed 5/6; contract-absent 1/6.
        # Callers alternate orthogonally (3 wins / 3 losses each way).
        plan = [({"contract": True}, True),
                ({"contract": True}, True),
                ({"contract": True}, True),
                ({"contract": True}, True),
                ({"contract": True}, True),
                ({"contract": True}, False),
                ({"callers": True}, True),
                ({"callers": True}, False),
                ({"callers": True}, False),
                ({}, False),
                ({}, False),
                ({}, False)]
        for index, (context, success) in enumerate(plan):
            markers = dict(context)
            if index % 2 == 0:
                markers.setdefault("callers", True)
            _plant_trajectory(ledger, "ctx-%02d" % index, markers,
                              success)
        weights = adaptive.learn_context_weights(ledger)
        self.assertEqual(set(weights), set(adaptive.CONTEXT_ELEMENTS))
        self.assertGreater(weights["contract"], 0.0)
        self.assertGreater(weights["contract"], weights["callers"])
        details = adaptive.context_weight_details(ledger)
        self.assertEqual(details["contract"]["n_present"], 6)
        self.assertEqual(details["contract"]["wins_present"], 5)

    def test_empty_ledger_yields_zero_weights(self):
        ledger = _open_ledger(self)
        weights = adaptive.learn_context_weights(ledger)
        self.assertEqual(set(weights), set(adaptive.CONTEXT_ELEMENTS))
        for element, weight in weights.items():
            self.assertEqual(weight, 0.0, element)
        self.assertEqual(adaptive.learn_context_weights(None),
                         {element: 0.0
                          for element in adaptive.CONTEXT_ELEMENTS})
        self.assertEqual(adaptive.learn_context_weights([]),
                         {element: 0.0
                          for element in adaptive.CONTEXT_ELEMENTS})

    def test_sparse_data_stays_neutral(self):
        ledger = _open_ledger(self)
        _plant_trajectory(ledger, "sparse-1", {"contract": True}, True)
        _plant_trajectory(ledger, "sparse-2", {}, False)
        weights = adaptive.learn_context_weights(ledger)
        for element, weight in weights.items():
            self.assertLess(abs(weight), 0.1, element)
        # One-sided evidence (no contrast) must not invent confidence.
        lonely = _open_ledger(self)
        _plant_trajectory(lonely, "lone", {"contract": True}, True)
        for element, weight in adaptive.learn_context_weights(lonely).items():
            self.assertEqual(weight, 0.0, element)

    def test_list_marker_forms_accepted(self):
        records = [({"context": ["contract", "lessons"]}, True),
                   ({"context": ["contract"]}, True),
                   ({"context": ["lessons"]}, False),
                   ({"context": []}, False)]
        weights = adaptive.learn_context_weights(records)
        self.assertGreaterEqual(weights["contract"], 0.0)
        self.assertLess(abs(weights["failure"]), 0.1)


class TestOrderTest(unittest.TestCase):
    def _history(self):
        history = []
        for _ in range(10):
            history.append({"check": "property", "caught": True,
                            "elapsed_ms": 5.0})
        history.append({"check": "property", "caught": False,
                        "elapsed_ms": 5.0})
        history.append({"check": "property", "caught": False,
                        "elapsed_ms": 5.0})
        for _ in range(10):
            history.append({"check": "performance", "caught": True,
                            "elapsed_ms": 500.0})
        history.append({"check": "performance", "caught": False,
                        "elapsed_ms": 500.0})
        history.append({"check": "performance", "caught": False,
                        "elapsed_ms": 500.0})
        for _ in range(2):
            history.append({"check": "direct", "caught": False,
                            "elapsed_ms": 5.0})
        return history

    def test_yield_per_ms_ranking(self):
        order = adaptive.learn_test_order(self._history())
        # Fast high-yield check first; slow high-yield check demoted
        # even though its raw catch count matches.
        self.assertEqual(order[0], "property")
        self.assertLess(order.index("property"),
                        order.index("performance"))
        for name in adaptive.DEFAULT_TEST_ORDER:
            self.assertIn(name, order)

    def test_empty_input_yields_default_order(self):
        self.assertEqual(adaptive.learn_test_order([]),
                         list(adaptive.DEFAULT_TEST_ORDER))
        self.assertEqual(adaptive.learn_test_order(None),
                         list(adaptive.DEFAULT_TEST_ORDER))
        ledger = _open_ledger(self)
        self.assertEqual(adaptive.learn_test_order(ledger),
                         list(adaptive.DEFAULT_TEST_ORDER))

    def test_sparse_data_and_novel_checks(self):
        history = [{"check": "fuzz", "caught": True, "elapsed_ms": 5.0}]
        order = adaptive.learn_test_order(history)
        self.assertIn("fuzz", order)
        for name in adaptive.DEFAULT_TEST_ORDER:
            self.assertIn(name, order)
        scores = adaptive.test_check_scores(history)
        self.assertEqual(scores["fuzz"]["runs"], 1)
        self.assertEqual(scores["direct"]["runs"], 0)

    def test_ledger_outcomes_ranked(self):
        ledger = _open_ledger(self)
        for index in range(6):
            ledger.append_event(
                "test outcome", task_id="t-%d" % index,
                payload={"check": "property", "caught": True,
                         "elapsed_ms": 5.0})
        for index in range(6):
            ledger.append_event(
                "test outcome", task_id="t-%d" % index,
                payload={"check": "performance", "caught": True,
                         "elapsed_ms": 500.0})
        order = adaptive.learn_test_order(ledger)
        self.assertEqual(order[0], "property")
        self.assertLess(order.index("property"),
                        order.index("performance"))

    def test_malformed_records_ignored(self):
        history = ["junk", 42, None, {"no_name": True},
                   {"check": "direct", "caught": True, "elapsed_ms": 5.0}]
        order = adaptive.learn_test_order(history)
        self.assertEqual(order[0], "direct")


class RouteRepairTest(unittest.TestCase):
    def test_safe_defaults_without_data(self):
        self.assertEqual(adaptive.route_repair("syntax"),
                         "deterministic-fix")
        self.assertEqual(adaptive.route_repair("edge-case"),
                         "model-repair")
        self.assertEqual(adaptive.route_repair("wrong-output"),
                         "model-repair")
        self.assertEqual(adaptive.route_repair("compile"),
                         "model-repair")
        self.assertEqual(adaptive.route_repair("state-corruption"),
                         "escalate")
        self.assertEqual(adaptive.route_repair("effect-violation"),
                         "escalate")
        self.assertEqual(adaptive.route_repair("memory"), "escalate")
        self.assertEqual(adaptive.route_repair("timeout"), "escalate")
        ledger = _open_ledger(self)
        self.assertEqual(adaptive.route_repair("syntax", ledger),
                         "deterministic-fix")
        for failure_class in trajectories.FAILURE_CLASSES:
            route = adaptive.route_repair(failure_class)
            self.assertIn(route, adaptive.REPAIR_ROUTES, failure_class)

    def test_unknown_class_escalates_without_crash(self):
        self.assertEqual(adaptive.route_repair("bogus-class"), "escalate")
        self.assertEqual(adaptive.route_repair(None), "escalate")
        self.assertEqual(adaptive.route_repair(""), "escalate")
        self.assertEqual(adaptive.route_repair(42), "escalate")
        # Aliases normalize to the canonical class route.
        self.assertEqual(
            adaptive.route_repair("contract"),
            adaptive.route_repair("type/contract"))

    def test_recorded_outcomes_override_default(self):
        ledger = _open_ledger(self)
        for index in range(6):
            trajectories.record_repair_outcome(
                ledger, "route-%d" % index, "cand-%d" % index,
                "wrong-output", "deterministic-fix", True)
        for index in range(5):
            trajectories.record_repair_outcome(
                ledger, "route-m-%d" % index, "cand-m-%d" % index,
                "wrong-output", "model-repair", index == 0)
        self.assertEqual(adaptive.route_repair("wrong-output", ledger),
                         "deterministic-fix")
        # Ledger-first argument order works too.
        self.assertEqual(adaptive.route_repair(ledger, "wrong-output"),
                         "deterministic-fix")
        # Other classes are unaffected by wrong-output evidence.
        self.assertEqual(adaptive.route_repair("syntax", ledger),
                         "deterministic-fix")

    def test_strategy_outcomes_count_as_evidence(self):
        ledger = _open_ledger(self)
        for index in range(3):
            trajectories.record_repair_outcome(
                ledger, "strat-%d" % index, "cand-%d" % index,
                "wrong-output", "deterministic-fix", True)
        for index in range(3):
            trajectories.record_strategy_outcome(
                ledger, "strat-s-%d" % index, "deterministic-fix", True,
                detail={"failure_class": "wrong-output"})
        self.assertEqual(adaptive.route_repair("wrong-output", ledger),
                         "deterministic-fix")

    def test_sparse_or_unknown_evidence_keeps_default(self):
        ledger = _open_ledger(self)
        trajectories.record_repair_outcome(
            ledger, "thin-1", "cand-1", "syntax", "escalated", True)
        trajectories.record_repair_outcome(
            ledger, "thin-2", "cand-2", "syntax", "escalated", True)
        self.assertEqual(adaptive.route_repair("syntax", ledger),
                         "deterministic-fix")
        noisy = _open_ledger(self)
        for index in range(10):
            trajectories.record_repair_outcome(
                noisy, "noise-%d" % index, "cand-%d" % index,
                "syntax", "quantum-fix", True)
        self.assertEqual(adaptive.route_repair("syntax", noisy),
                         "deterministic-fix")


class EscalationPolicyTest(unittest.TestCase):
    def test_empty_input_yields_default_policy(self):
        expected = {"max_repairs": 1, "escalate_on_oscillation": True}
        self.assertEqual(adaptive.learn_escalation_policy([]), expected)
        self.assertEqual(adaptive.learn_escalation_policy(None), expected)
        ledger = _open_ledger(self)
        self.assertEqual(adaptive.learn_escalation_policy(ledger),
                         expected)

    def test_late_success_grows_budget(self):
        histories = [{"attempts": 3, "success": True}] * 8
        policy = adaptive.learn_escalation_policy(histories)
        self.assertEqual(policy["max_repairs"], 2)
        self.assertTrue(policy["escalate_on_oscillation"])
        flag_histories = [[False, False, True]] * 8
        policy = adaptive.learn_escalation_policy(flag_histories)
        self.assertEqual(policy["max_repairs"], 2)
        loop_histories = [{"repairs_used": 2, "status": "success"}] * 8
        policy = adaptive.learn_escalation_policy(loop_histories)
        self.assertEqual(policy["max_repairs"], 2)

    def test_budget_is_bounded(self):
        histories = [{"attempts": 10, "success": True}] * 8
        policy = adaptive.learn_escalation_policy(histories)
        self.assertEqual(policy["max_repairs"], adaptive.MAX_REPAIRS)
        self.assertLessEqual(policy["max_repairs"], 3)

    def test_sparse_or_failed_histories_stay_default(self):
        sparse = [{"attempts": 3, "success": True}] * 2
        policy = adaptive.learn_escalation_policy(sparse)
        self.assertEqual(policy["max_repairs"], 1)
        failed = [{"attempts": 3, "success": False}] * 8
        policy = adaptive.learn_escalation_policy(failed)
        self.assertEqual(policy["max_repairs"], 1)
        # Histories without outcome evidence are ignored, not invented.
        unknown = [[{"signature": "a"}, {"signature": "b"}]] * 8
        policy = adaptive.learn_escalation_policy(unknown)
        self.assertEqual(policy["max_repairs"], 1)

    def test_ledger_repair_sequences(self):
        ledger = _open_ledger(self)
        for task in range(6):
            task_id = "esc-%d" % task
            trajectories.record_repair_outcome(
                ledger, task_id, "cand-a", "edge-case",
                "model-repair", False)
            trajectories.record_repair_outcome(
                ledger, task_id, "cand-b", "edge-case",
                "model-repair", False)
            trajectories.record_repair_outcome(
                ledger, task_id, "cand-c", "edge-case",
                "model-repair", True)
        policy = adaptive.learn_escalation_policy(ledger)
        self.assertEqual(policy["max_repairs"], 2)
        self.assertTrue(policy["escalate_on_oscillation"])


class ImportHygieneTest(unittest.TestCase):
    def test_only_stdlib_events_trajectories(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "adaptive.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        imported = set()
        for line in source.splitlines():
            match = re.match(r"\s*(?:import|from)\s+([A-Za-z0-9_]+)",
                             line)
            if match:
                imported.add(match.group(1))
        self.assertTrue(imported)
        self.assertLessEqual(imported, {"json", "events", "trajectories"})


class RiskModelTest(unittest.TestCase):
    def _by_level(self, notes):
        self.assertEqual([note["level"] for note in notes],
                         ["R0", "R1", "R2", "R3", "R4", "R5", "R6"])
        for note in notes:
            self.assertEqual(set(note),
                             {"level", "predicted_count",
                              "actual_incidents"})
        return {note["level"]: note for note in notes}

    def test_predicted_vs_actual_tallied_per_level(self):
        records = ([{"risk": "R1", "incident": True}] * 3
                   + [{"risk": "R1", "incident": False}] * 7
                   + [{"predicted": "R2"}] * 4
                   + [{"risk": "r4", "incident": True}])
        by_level = self._by_level(adaptive.learn_risk_model(records))
        self.assertEqual(by_level["R1"]["predicted_count"], 10)
        self.assertEqual(by_level["R1"]["actual_incidents"], 3)
        self.assertEqual(by_level["R2"]["predicted_count"], 4)
        self.assertEqual(by_level["R2"]["actual_incidents"], 0)
        self.assertEqual(by_level["R4"]["predicted_count"], 1)
        self.assertEqual(by_level["R4"]["actual_incidents"], 1)

    def test_ledger_predictions_join_rollbacks(self):
        import instrument

        ledger = _open_ledger(self)
        for index in range(3):
            ledger.append_event(
                "risk classified", task_id="risk-%d" % index,
                candidate_id="cand-%d" % index,
                payload={"risk": "R1"})
        ledger.append_event("risk classified", task_id="risk-clean",
                            candidate_id="cand-clean",
                            payload={"risk": "R2"})
        instrument.log_rollback(ledger, "cap-cache", 8, 7,
                                "state contamination")
        # Rollback rows carry only capability linkage; link one
        # prediction to the rolled-back capability.
        ledger.append_event("risk classified", capability_id="cap-cache",
                            payload={"risk": "R1"})
        by_level = self._by_level(adaptive.learn_risk_model(ledger))
        self.assertEqual(by_level["R1"]["predicted_count"], 4)
        self.assertEqual(by_level["R1"]["actual_incidents"], 1)
        self.assertEqual(by_level["R2"]["predicted_count"], 1)
        self.assertEqual(by_level["R2"]["actual_incidents"], 0)

    def test_empty_and_malformed_inputs_yield_zeros(self):
        for source in (None, [], _open_ledger(self)):
            for note in adaptive.learn_risk_model(source):
                self.assertEqual(note["predicted_count"], 0)
                self.assertEqual(note["actual_incidents"], 0)
        junk = ["junk", 42, None, {"no_risk": True},
                {"risk": "R9"}, {"risk": 42},
                {"risk": "R3", "incident": False}]
        by_level = self._by_level(adaptive.learn_risk_model(junk))
        self.assertEqual(by_level["R3"]["predicted_count"], 1)
        self.assertEqual(by_level["R3"]["actual_incidents"], 0)
        self.assertEqual(sum(note["predicted_count"]
                             for note in by_level.values()), 1)


class ApplicabilityTest(unittest.TestCase):
    def _families(self):
        import skills

        return [
            skills.make_family(
                "csv-family", "Parse CSV rows.",
                when=["csv", "parsing"], when_not=["streaming"]),
            skills.make_family(
                "pager-family", "Walk cursor pagination.",
                when=["rest"], when_not=["graphql"]),
        ]

    def test_when_and_when_not_tallies(self):
        families = self._families()
        outcomes = [
            {"family_id": "csv-family", "tags": ["csv", "parsing"],
             "worked": True},
            {"family_id": "csv-family", "tags": ["csv"],
             "worked": True},
            {"family_id": "csv-family", "tags": ["csv", "streaming"],
             "worked": False},
            {"family_id": "csv-family", "tags": ["streaming"],
             "worked": False},
            {"family_id": "pager-family",
             "task": {"tags": ["rest"]}, "success": True},
        ]
        tallies = adaptive.learn_applicability(families, outcomes)
        self.assertEqual(set(tallies), {"csv-family", "pager-family"})
        csv = tallies["csv-family"]
        self.assertEqual(set(csv), {"when", "when_not"})
        self.assertEqual(csv["when"]["csv"],
                         {"success": 2, "failure": 1})
        self.assertEqual(csv["when"]["parsing"],
                         {"success": 1, "failure": 0})
        self.assertEqual(csv["when_not"]["streaming"],
                         {"success": 0, "failure": 2})
        self.assertEqual(tallies["pager-family"]["when"]["rest"],
                         {"success": 1, "failure": 0})
        # Unobserved declared tags keep explicit zero tallies.
        self.assertEqual(tallies["pager-family"]["when_not"]["graphql"],
                         {"success": 0, "failure": 0})

    def test_unknown_families_and_signalless_outcomes_skipped(self):
        families = self._families()
        outcomes = [
            {"family_id": "ghost-family", "tags": ["csv"],
             "worked": True},
            {"family_id": "csv-family", "tags": ["csv"]},
            {"family_id": "csv-family", "worked": True},
            "junk",
            {"family_id": "csv-family", "tags": ["novel-tag"],
             "worked": True},
        ]
        tallies = adaptive.learn_applicability(families, outcomes)
        self.assertNotIn("ghost-family", tallies)
        for bucket in ("when", "when_not"):
            for counts in tallies["csv-family"][bucket].values():
                self.assertEqual(counts, {"success": 0, "failure": 0})
        self.assertEqual(adaptive.learn_applicability(None, outcomes),
                         {})
        self.assertEqual(adaptive.learn_applicability(families, None),
                         adaptive.learn_applicability(families, []))

    def test_pure_function_never_writes_to_store(self):
        import copy
        import skills

        with tempfile.TemporaryDirectory() as tmp:
            store = skills.SkillStore(os.path.join(tmp, "skills"))
            for family in self._families():
                store.save_family(family=family)
            before = [store.get_family("csv-family"),
                      store.get_family("pager-family")]
            snapshot = copy.deepcopy(before)
            outcomes_path = os.path.join(tmp, "skills",
                                         "outcomes.jsonl")
            outcomes = [{"family_id": "csv-family", "tags": ["csv"],
                         "worked": True}]
            tallies = adaptive.learn_applicability(store, outcomes)
            self.assertEqual(tallies["csv-family"]["when"]["csv"],
                             {"success": 1, "failure": 0})
            # No store writes: family records byte-identical, no log.
            self.assertEqual([store.get_family("csv-family"),
                              store.get_family("pager-family")],
                             snapshot)
            self.assertFalse(os.path.exists(outcomes_path))
            # Inputs unmutated.
            self.assertEqual(outcomes,
                             [{"family_id": "csv-family",
                               "tags": ["csv"], "worked": True}])


class PostmortemTest(unittest.TestCase):
    def test_rollback_yields_lesson_candidate(self):
        import instrument

        ledger = _open_ledger(self)
        row = instrument.log_rollback(ledger, "cap-cache", 8, 7,
                                      "state contamination via cache")
        candidates = adaptive.postmortem_trigger(row)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["status"], "candidate")
        self.assertEqual(candidate["lesson_class"], "failure")
        self.assertEqual(candidate["source"], "rollback")
        self.assertEqual(candidate["confidence"], 0.55)
        self.assertIn("cap-cache", candidate["statement"])
        self.assertIn("state contamination via cache",
                      candidate["statement"])
        self.assertEqual(candidate["evidence"]["event_ids"],
                         [row["event_id"]])
        self.assertEqual(candidate["evidence"]["count"], 1)
        self.assertIn("cap-cache", candidate["applies_when"]["when"])
        # Shape matches mine.propose_lesson_candidate consumers.
        import mine

        mine.deduplicate(candidates + [dict(candidate)])

    def test_failure_class_and_bare_payload_accepted(self):
        event = {"event_type": "candidate rolled back",
                 "event_id": 41, "task_id": "task-7",
                 "capability_id": "cap-parse",
                 "payload": {"from_version": 3, "to_version": 2,
                             "reason": "cyclic cursor loop",
                             "failure_class": "timeout"}}
        (candidate,) = adaptive.postmortem_trigger(event)
        self.assertEqual(candidate["failure_class"], "timeout")
        self.assertIn("timeout", candidate["statement"])
        self.assertEqual(candidate["evidence"]["task_ids"], ["task-7"])
        bare = {"capability_id": "cap-x", "from_version": 2,
                "to_version": 1, "reason": "regression"}
        (candidate,) = adaptive.postmortem_trigger(bare)
        self.assertIn("cap-x", candidate["statement"])

    def test_non_rollback_and_garbage_yield_no_candidates(self):
        import instrument

        ledger = _open_ledger(self)
        task_row = instrument.log_task_outcome(ledger, "task-1", False,
                                               "failed green-stop")
        self.assertEqual(adaptive.postmortem_trigger(task_row), [])
        self.assertEqual(adaptive.postmortem_trigger(
            {"event_type": "task completed",
             "payload": {"success": False}}), [])
        for bad in (None, "rollback", 42, [], {}, {"payload": {}}):
            self.assertEqual(adaptive.postmortem_trigger(bad), [],
                             msg=repr(bad))


if __name__ == "__main__":
    unittest.main()
