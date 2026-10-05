"""Tests for embeddings.py + semantic retrieval fusion. Stdlib only."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import retrieve
from embeddings import TfIdfBackend, VectorIndex, cosine_similarity
from retrieve import Capability, CapabilityIndex


def make_paraphrase_setup():
    """Paraphrased intent vs keyword-stuffed decoy, with IDF background."""
    docs = {
        # Paraphrase of "convert CSV rows into records": reworded verbs
        # and nouns, sharing only rare content ("rows", "record*").
        "paraphrase": "transform comma-separated rows into structured record entries",
        # Keyword-stuffed decoy: repeats the common term "CSV" but shares
        # nothing else with the query.
        "decoy": "parse CSV parse CSV parse CSV text fast quick",
        # IDF background: makes "csv"/"parse"/"text" common, "rows" rare.
        "bg-csv-load": "load csv file from disk quickly",
        "bg-json": "parse json text into objects",
        "bg-xml": "parse xml text into a tree",
        "bg-mail": "send email messages over the network",
        "bg-html": "render html page from template",
    }
    backend = TfIdfBackend().fit(list(docs.values()))
    index = VectorIndex(backend)
    index.add_many(docs.items())
    return backend, index


class TfIdfBackendTest(unittest.TestCase):
    def test_paraphrase_beats_keyword_stuffed_decoy(self):
        _, index = make_paraphrase_setup()
        ranked = index.top_k("convert CSV rows into records", k=8)
        order = [key for key, _ in ranked]
        # Sublinear TF + IDF damp the stuffed common term "csv"; the rare
        # shared content ("rows", "record*") must win.
        self.assertLess(order.index("paraphrase"), order.index("decoy"))
        scores = dict(ranked)
        self.assertGreater(scores["paraphrase"], scores["decoy"])
        self.assertGreater(scores["paraphrase"], 0.0)

    def test_determinism_same_query_twice_identical(self):
        _, index = make_paraphrase_setup()
        first = index.top_k("convert CSV rows into records", k=8)
        second = index.top_k("convert CSV rows into records", k=8)
        self.assertEqual([k for k, _ in first], [k for k, _ in second])
        self.assertEqual([s for _, s in first], [s for _, s in second])

    def test_empty_corpus_handled(self):
        backend = TfIdfBackend().fit([])
        self.assertTrue(backend.fitted)
        self.assertEqual(backend.n_docs, 0)
        self.assertEqual(backend.embed(["anything here"]), [{}])
        index = VectorIndex(backend)
        index.add_many([])
        self.assertEqual(index.top_k("anything here"), [])
        # Unfitted backend is equally safe: zero vectors, zero cosine.
        fresh = TfIdfBackend()
        self.assertEqual(fresh.embed(["hello"]), [{}])
        self.assertEqual(cosine_similarity({}, {}), 0.0)

    def test_cosine_basics(self):
        self.assertAlmostEqual(cosine_similarity({"a": 1.0}, {"a": 1.0}), 1.0)
        self.assertAlmostEqual(cosine_similarity({"a": 1.0}, {"b": 1.0}), 0.0)
        self.assertAlmostEqual(
            cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)
        with self.assertRaises(TypeError):
            cosine_similarity({"a": 1.0}, [1.0])


class SemanticFusionTest(unittest.TestCase):
    def make_index(self):
        return CapabilityIndex([
            Capability(
                id="csv-parse",
                intent="transform comma-separated rows into structured record entries",
                input_types=["csv-text"],
                output_types=["record"],
                effects=[],
                family="parsing",
            ),
            Capability(
                id="csv-decoy",
                intent="parse CSV parse CSV text fast quick processing",
                input_types=["json-text"],
                output_types=["html-page"],
                effects=["network-write"],
                family="serving",
            ),
        ])

    def test_fusion_prefers_type_and_semantic_match(self):
        index = self.make_index()
        backend = TfIdfBackend()
        goal = {
            "text": "convert CSV rows into records",
            "input_types": ["csv-text"],
            "output_types": ["record"],
            "allowed_effects": [],
        }
        ranked = retrieve.find_capabilities_semantic(goal, index, backend, k=8)
        self.assertGreaterEqual(len(ranked), 1)
        self.assertEqual(ranked[0][0].id, "csv-parse")
        ids = [cap.id for cap, _ in ranked]
        self.assertNotIn("csv-decoy", ids)
        # Semantic term contributes positively over the keyword-only score.
        plain = dict((c.id, s) for c, s in index.find_capabilities(goal, k=8))
        fused = dict((c.id, s) for c, s in ranked)
        self.assertGreater(fused["csv-parse"], plain["csv-parse"])

    def test_fusion_without_effect_filter_still_prefers_fit(self):
        index = self.make_index()
        backend = TfIdfBackend()
        goal = {
            "text": "convert CSV rows into records",
            "input_types": ["csv-text"],
            "output_types": ["record"],
        }
        ranked = retrieve.find_capabilities_semantic(goal, index, backend, k=8)
        self.assertEqual(ranked[0][0].id, "csv-parse")

    def test_prebuilt_semantic_index_reused(self):
        index = self.make_index()
        backend = TfIdfBackend()
        sem = retrieve.build_semantic_index(index, backend)
        self.assertEqual(len(sem), 2)
        goal = {"text": "convert CSV rows into records"}
        first = retrieve.find_capabilities_semantic(
            goal, index, backend, semantic_index=sem)
        second = retrieve.find_capabilities_semantic(
            goal, index, backend, semantic_index=sem)
        self.assertEqual([c.id for c, _ in first],
                         [c.id for c, _ in second])
        self.assertEqual([s for _, s in first], [s for _, s in second])

    def test_empty_index_yields_no_match(self):
        index = CapabilityIndex()
        backend = TfIdfBackend()
        self.assertEqual(
            retrieve.find_capabilities_semantic({"text": "anything"}, index,
                                                backend),
            [])


if __name__ == "__main__":
    unittest.main()
