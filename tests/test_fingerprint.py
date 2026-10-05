"""Tests for the strong generation fingerprint (fingerprint.py)."""

import ast
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fingerprint

REPO_FILES = None


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _mini_tree(root):
    _write(os.path.join(root, "graygoo.asd"),
           '(defsystem "graygoo"\n  :version "0.1.0"\n  :depends-on ())\n')
    _write(os.path.join(root, "src", "kernel", "kernel.lisp"), "(in-package :k)\n")
    _write(os.path.join(root, "src", "capability", "capability.lisp"),
           "(in-package :c)\n")
    _write(os.path.join(root, "src", "state", "schema.lisp"), "(in-package :s)\n")


class ComputeDictTest(unittest.TestCase):
    def test_real_tree_shape(self):
        info = fingerprint.compute_dict(sbcl_exe="definitely-not-sbcl")
        self.assertGreater(info["files"], 0)
        self.assertEqual(info["source_files"], sorted(info["source_files"]))
        self.assertTrue(all(name.endswith(".lisp")
                            for name in info["source_files"]))
        self.assertEqual(len(info["digest"]), 64)
        self.assertEqual(len(info["source_tree"]), 64)
        self.assertEqual(info["asd"]["version"], "0.1.0")
        self.assertEqual(info["asd"]["depends_on"], [])
        self.assertEqual(info["epoch"], "-")
        self.assertEqual(info["protocol_version"],
                         fingerprint.PROTOCOL_VERSION)
        self.assertEqual(info["schema"]["generation"],
                         fingerprint.SCHEMA_GENERATION_DEFAULT)
        self.assertEqual(info["dependencies"]["sbcl"], "unknown")

    def test_deterministic(self):
        first = fingerprint.compute_dict(sbcl_exe="definitely-not-sbcl")
        second = fingerprint.compute_dict(sbcl_exe="definitely-not-sbcl")
        self.assertEqual(first, second)

    def test_sensitive_to_source_change(self):
        with tempfile.TemporaryDirectory() as root:
            _mini_tree(root)
            before = fingerprint.compute_dict(
                root, sbcl_exe="definitely-not-sbcl")
            self.assertEqual(before["files"], 3)
            with open(os.path.join(root, "src", "kernel", "kernel.lisp"),
                      "a", encoding="utf-8") as handle:
                handle.write(";; comment\n")
            after = fingerprint.compute_dict(
                root, sbcl_exe="definitely-not-sbcl")
            self.assertNotEqual(before["source_tree"], after["source_tree"])
            self.assertNotEqual(before["digest"], after["digest"])

    def test_sensitive_to_asd_change(self):
        with tempfile.TemporaryDirectory() as root:
            _mini_tree(root)
            before = fingerprint.compute_dict(
                root, sbcl_exe="definitely-not-sbcl")
            with open(os.path.join(root, "graygoo.asd"),
                      "a", encoding="utf-8") as handle:
                handle.write(";; comment\n")
            after = fingerprint.compute_dict(
                root, sbcl_exe="definitely-not-sbcl")
            self.assertNotEqual(before["asd"]["hash"], after["asd"]["hash"])
            self.assertNotEqual(before["digest"], after["digest"])

    def test_manifest_schema_proto_inputs_move_digest(self):
        with tempfile.TemporaryDirectory() as root:
            _mini_tree(root)
            base = fingerprint.compute_dict(
                root, sbcl_exe="definitely-not-sbcl")
            with_manifest = fingerprint.compute_dict(
                root, sbcl_exe="definitely-not-sbcl",
                capability_manifest={"cap-a": 2})
            self.assertNotEqual(base["digest"], with_manifest["digest"])
            self.assertIsNone(base["capability_manifest"]["extra"])
            self.assertIsNotNone(
                with_manifest["capability_manifest"]["extra"])
            bumped_schema = fingerprint.compute_dict(
                root, sbcl_exe="definitely-not-sbcl", schema_generation=7)
            self.assertNotEqual(base["digest"], bumped_schema["digest"])
            bumped_proto = fingerprint.compute_dict(
                root, sbcl_exe="definitely-not-sbcl", protocol_version=99)
            self.assertNotEqual(base["digest"], bumped_proto["digest"])

    def test_missing_parts_degrade_without_raising(self):
        with tempfile.TemporaryDirectory() as root:
            info = fingerprint.compute_dict(
                root, sbcl_exe="definitely-not-sbcl")
            self.assertEqual(info["files"], 0)
            self.assertEqual(info["asd"]["hash"], "missing")
            self.assertEqual(len(info["digest"]), 64)

    def test_bad_inputs_rejected(self):
        with self.assertRaises(ValueError):
            fingerprint.compute_dict(
                os.path.join("no", "such", "root", "xyz"))
        with self.assertRaises(TypeError):
            fingerprint.compute_dict(capability_manifest=["not", "a", "dict"])

    def test_epoch_convention(self):
        info = fingerprint.compute_dict(sbcl_exe="definitely-not-sbcl",
                                        epoch_id="test-epoch-7")
        self.assertEqual(info["epoch"], "test-epoch-7")

    def test_sbcl_probe_best_effort(self):
        info = fingerprint.compute_dict()
        self.assertIsInstance(info["dependencies"]["sbcl"], str)
        self.assertTrue(info["dependencies"]["sbcl"])


class ComputeStringTest(unittest.TestCase):
    def test_canonical_shape(self):
        text = fingerprint.compute_string(sbcl_exe="definitely-not-sbcl",
                                          epoch_id="e1")
        self.assertTrue(text.startswith("ggfp1:"), text)
        for field in ("src=", "asd=", "deps=", "caps=", "schema=", "proto=",
                      "epoch=e1"):
            self.assertIn(field, text)

    def test_matches_round_trip(self):
        text = fingerprint.compute_string(sbcl_exe="definitely-not-sbcl",
                                          epoch_id="e9")
        self.assertTrue(fingerprint.matches(
            text, sbcl_exe="definitely-not-sbcl", epoch_id="e9"))
        self.assertFalse(fingerprint.matches(
            text, sbcl_exe="definitely-not-sbcl", epoch_id="other"))
        self.assertFalse(fingerprint.matches(text + "x",
                                             sbcl_exe="definitely-not-sbcl",
                                             epoch_id="e9"))
        self.assertFalse(fingerprint.matches(None))
        self.assertFalse(fingerprint.matches(42))


class StdlibOnlyTest(unittest.TestCase):
    def test_imports_are_stdlib(self):
        allowed = {"__future__", "hashlib", "os", "re", "subprocess", "sys"}
        with open(fingerprint.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0]
                                for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(imported, allowed, imported)


if __name__ == "__main__":
    unittest.main()
