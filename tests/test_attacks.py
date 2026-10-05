"""Tests for the adversarial rehearsal-boundary harness (attacks.py).

Stdlib only. These tests exercise the HARNESS (verdict shapes, registry,
validation) with stubbed attack callables. They never spawn SBCL and never
run live attacks: the callable form of run_attack performs no worker runs.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import attacks

RESULT_KEYS = {"attack", "verdict", "evidence"}
EXPECTED_ATTACKS = (
    "infinite-loop",
    "memory-bomb",
    "process-spawn",
    "filesystem-escape",
    "network-egress",
    "kernel-mutation",
    "evaluator-inspection",
)


def _stub_safe(ctx):
    del ctx
    return "SAFE", {"timed_out": True, "driver_alive": True}


def _stub_escaped(ctx):
    del ctx
    return "VULNERABLE", {"executed": True}


class RunAttackHarnessTest(unittest.TestCase):
    def test_stubbed_safe_attack_records_safe_shape(self):
        result = attacks.run_attack("infinite-loop", _stub_safe)
        self.assertEqual(set(result), RESULT_KEYS)
        self.assertEqual(result["attack"], "infinite-loop")
        self.assertEqual(result["verdict"], "SAFE")
        self.assertIsInstance(result["evidence"], dict)
        self.assertTrue(result["evidence"]["timed_out"])

    def test_stubbed_escaped_attack_records_vulnerable_shape(self):
        result = attacks.run_attack("process-spawn", _stub_escaped)
        self.assertEqual(set(result), RESULT_KEYS)
        self.assertEqual(result["attack"], "process-spawn")
        self.assertEqual(result["verdict"], "VULNERABLE")
        self.assertIsInstance(result["evidence"], dict)
        self.assertTrue(result["evidence"]["executed"])

    def test_stubbed_inconclusive_shape(self):
        result = attacks.run_attack(
            "network-egress", lambda ctx: ("INCONCLUSIVE", {"note": "x"}))
        self.assertEqual(result["verdict"], "INCONCLUSIVE")
        self.assertEqual(set(result), RESULT_KEYS)

    def test_unknown_attack_rejected(self):
        with self.assertRaises(ValueError):
            attacks.run_attack("no-such-attack", _stub_safe)

    def test_bad_custom_verdict_rejected(self):
        with self.assertRaises(ValueError):
            attacks.run_attack("infinite-loop",
                               lambda ctx: ("MAYBE", {}))

    def test_non_dict_evidence_rejected(self):
        with self.assertRaises(ValueError):
            attacks.run_attack("infinite-loop",
                               lambda ctx: ("SAFE", ["not", "a", "dict"]))

    def test_bad_payload_type_rejected(self):
        with self.assertRaises(ValueError):
            attacks.run_attack("infinite-loop", 42)


class AttackRegistryTest(unittest.TestCase):
    def test_all_seven_attacks_registered(self):
        self.assertEqual(tuple(attacks.ATTACK_NAMES), EXPECTED_ATTACKS)

    def test_run_all_rejects_unknown_name(self):
        with self.assertRaises(ValueError):
            attacks.run_all(["infinite-loop", "bogus"])


class FakeCtx:
    """Duck-typed AttackContext: canned worker results, no SBCL."""

    def __init__(self, results):
        self._results = list(results)

    def run(self, code, timeout_s=10.0, memory_mb=512):
        del code, timeout_s, memory_mb
        return self._results.pop(0)


def _worker_result(ok=False, error="", return_value="",
                   timed_out=False):
    return {"ok": ok, "error": error, "return_value": return_value,
            "timed_out": timed_out, "stdout": "", "elapsed_ms": 1.0}


class NetworkEgressVerdictTest(unittest.TestCase):
    """Hermetic tests for the two-probe egress verdict logic."""

    def test_both_signals_is_safe(self):
        ctx = FakeCtx([
            _worker_result(
                error="rehearsal sandbox denied: MODULE SB-BSD-SOCKETS "
                      "is forbidden in this worker"),
            _worker_result(
                error="Package SB-BSD-SOCKETS does not exist."),
        ])
        verdict, evidence = attacks._attack_network_egress(ctx, None)
        self.assertEqual(verdict, "SAFE")
        self.assertTrue(evidence["require_denied"])
        self.assertTrue(evidence["package_absent"])
        self.assertFalse(evidence["connected"])

    def test_missing_gate_signal_is_inconclusive(self):
        ctx = FakeCtx([
            _worker_result(ok=True, return_value=":ALREADY-LOADED"),
            _worker_result(
                error="Package SB-BSD-SOCKETS does not exist."),
        ])
        verdict, evidence = attacks._attack_network_egress(ctx, None)
        self.assertEqual(verdict, "INCONCLUSIVE")
        self.assertFalse(evidence["require_denied"])

    def test_connect_is_vulnerable(self):
        ctx = FakeCtx([
            _worker_result(
                error="rehearsal sandbox denied: MODULE SB-BSD-SOCKETS "
                      "is forbidden in this worker"),
            _worker_result(ok=True, return_value=":EGRESS-OPEN"),
        ])
        verdict, evidence = attacks._attack_network_egress(ctx, None)
        self.assertEqual(verdict, "VULNERABLE")
        self.assertTrue(evidence["connected"])


if __name__ == "__main__":
    unittest.main()
