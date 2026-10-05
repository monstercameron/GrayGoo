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


if __name__ == "__main__":
    unittest.main()
