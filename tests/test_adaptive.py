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


if __name__ == "__main__":
    unittest.main()
