"""Tests for the demo bundle export (bundle.py). Stdlib only, offline.

Covers: bundle builds to a temp path, contains every declared member
plus bundle-manifest.json, inner manifest pins the current git HEAD
and sha256 of each member (verified by re-hash), and no secret leaks
(the live Cerebras key must not appear in any member).
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bundle


def git_head():
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        capture_output=True, text=True, timeout=30)
    return (out.stdout or "").strip()


class BuildTest(unittest.TestCase):
    def test_builds_and_lists_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "demo.zip")
            self.assertEqual(bundle.build_bundle(out), out)
            with zipfile.ZipFile(out) as zf:
                names = set(zf.namelist())
            for rel in bundle.MEMBERS:
                self.assertIn(rel, names, rel)
            self.assertIn("bundle-manifest.json", names)

    def test_inner_manifest_pins_head_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "demo.zip")
            bundle.build_bundle(out)
            with zipfile.ZipFile(out) as zf:
                inner = json.loads(
                    zf.read("bundle-manifest.json").decode("utf-8"))
                for rel, pinned in inner["files"].items():
                    blob = zf.read(rel)
                    self.assertEqual(hashlib.sha256(blob).hexdigest(),
                                     pinned, rel)
        self.assertEqual(inner["bundle_version"],
                         bundle.BUNDLE_VERSION)
        self.assertEqual(inner["git_head"], git_head())
        self.assertEqual(inner["manifest"]["manifest_version"],
                         bundle.manifest.MANIFEST_VERSION)

    def test_cli_writes_zip(self):
        root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "demo.zip")
            proc = subprocess.run(
                [sys.executable, os.path.join(root, "bundle.py"),
                 "--out", out],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(zipfile.is_zipfile(out))

    def test_no_secret_in_bundle(self):
        key = os.environ.get("CEREBRAS_API_KEY") or os.environ.get(
            "CEREBRAS")
        if not key:
            self.skipTest("no live key in environment")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "demo.zip")
            bundle.build_bundle(out)
            with zipfile.ZipFile(out) as zf:
                for name in zf.namelist():
                    try:
                        text = zf.read(name).decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    self.assertNotIn(key, text, name)


if __name__ == "__main__":
    unittest.main()
