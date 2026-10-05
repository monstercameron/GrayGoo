"""Tests for in-memory capability retrieval v0 (retrieve.py). Stdlib only."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import retrieve
from retrieve import Capability, CapabilityIndex, compose_plan


def make_index():
    return CapabilityIndex([
        Capability(
            id="csv-to-records",
            intent="parse CSV text rows into structured records",
            input_types=["csv-text"],
            output_types=["record"],
            effects=[],
            family="parsing",
            version=3,
            success_count=9,
            use_count=10,
        ),
        Capability(
            id="csv-keyword-decoy",
            intent="parse CSV text rows into structured records quickly",
            input_types=["json-text"],
            output_types=["html-page"],
            effects=["network-write"],
            family="serving",
            version=1,
        ),
        Capability(
            id="records-to-json",
            intent="serialize structured records as JSON text",
            input_types=["record"],
            output_types=["json-text"],
            effects=[],
            family="parsing",
            version=2,
            success_count=4,
            use_count=5,
        ),
    ])


class FindCapabilitiesTest(unittest.TestCase):
    def test_type_and_effect_match_beats_keyword_only(self):
        index = make_index()
        goal = {
            "text": "parse CSV text rows into structured records",
            "input_types": ["csv-text"],
            "output_types": ["record"],
            "allowed_effects": [],
        }
        ranked = index.find_capabilities(goal, k=8)
        self.assertGreaterEqual(len(ranked), 1)
        # The decoy shares MORE keywords but has wrong types and a
        # forbidden effect; the contract-fitting capability must win.
        self.assertEqual(ranked[0][0].id, "csv-to-records")
        ids = [cap.id for cap, _ in ranked]
        self.assertNotIn("csv-keyword-decoy", ids)

    def test_scores_descend_and_k_limits_results(self):
        index = make_index()
        ranked = index.find_capabilities({"text": "records"}, k=2)
        self.assertLessEqual(len(ranked), 2)
        scores = [s for _, s in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_forbidden_effect_excludes_capability(self):
        index = make_index()
        ranked = index.find_capabilities(
            {"text": "parse CSV"},
            {"forbidden_effects": ["network-write"]})
        ids = [cap.id for cap, _ in ranked]
        self.assertNotIn("csv-keyword-decoy", ids)

    def test_historical_reuse_breaks_ties(self):
        index = CapabilityIndex([
            Capability(id="unused", intent="parse CSV rows",
                       input_types=["csv-text"], output_types=["record"]),
            Capability(id="proven", intent="parse CSV rows",
                       input_types=["csv-text"], output_types=["record"],
                       success_count=8, use_count=10),
        ])
        ranked = index.find_capabilities({
            "text": "parse CSV rows",
            "input_types": ["csv-text"],
            "output_types": ["record"],
        })
        self.assertEqual([c.id for c, _ in ranked], ["proven", "unused"])

    def test_empty_index_yields_clean_no_match(self):
        index = CapabilityIndex()
        self.assertEqual(index.find_capabilities({"text": "anything"}), [])
        self.assertEqual(
            index.find_capabilities({"text": "x", "output_types": ["y"]},
                                    {"allowed_effects": []}),
            [])

    def test_no_relevant_capability_yields_no_match(self):
        index = make_index()
        ranked = index.find_capabilities(
            {"text": "bake sourdough bread", "family": "baking"})
        self.assertEqual(ranked, [])


class ComposePlanTest(unittest.TestCase):
    def test_chains_a_to_b_for_connectable_goal(self):
        index = CapabilityIndex([
            Capability(id="a-parse", intent="parse CSV into records",
                       input_types=["csv-text"], output_types=["record"]),
            Capability(id="b-render", intent="render records as JSON",
                       input_types=["record"], output_types=["json-text"]),
        ])
        plan = compose_plan({"input_types": ["csv-text"],
                             "output_types": ["json-text"]}, index)
        self.assertIsNotNone(plan)
        self.assertEqual([c.id for c in plan], ["a-parse", "b-render"])

    def test_direct_hit_returns_single_step(self):
        index = make_index()
        plan = compose_plan({"input_types": ["csv-text"],
                             "output_types": ["record"]}, index)
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].id, "csv-to-records")

    def test_unconnectable_goal_returns_none(self):
        index = make_index()
        plan = compose_plan({"input_types": ["csv-text"],
                             "output_types": ["audio-waveform"]}, index)
        self.assertIsNone(plan)

    def test_empty_index_returns_none(self):
        self.assertIsNone(compose_plan({"input_types": ["a"],
                                        "output_types": ["b"]},
                                       CapabilityIndex()))

    def test_composition_honors_effect_constraints(self):
        index = CapabilityIndex([
            Capability(id="a-parse", intent="parse CSV into records",
                       input_types=["csv-text"], output_types=["record"]),
            Capability(id="b-shady", intent="render records as JSON",
                       input_types=["record"], output_types=["json-text"],
                       effects=["network-write"]),
            Capability(id="b-clean", intent="render records as JSON",
                       input_types=["record"], output_types=["json-text"],
                       effects=[]),
        ])
        plan = compose_plan({"input_types": ["csv-text"],
                             "output_types": ["json-text"],
                             "allowed_effects": []}, index)
        self.assertIsNotNone(plan)
        self.assertEqual([c.id for c in plan], ["a-parse", "b-clean"])


if __name__ == "__main__":
    unittest.main()
