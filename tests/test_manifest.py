"""Tests for the reproducibility manifest (manifest.py). Stdlib only.

Covers: required keys present, JSON-serializable, no secret leakage
(the live Cerebras key must never appear in the rendered manifest),
--check exit status, and --write round-trip. No live API calls.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import manifest


class CollectTest(unittest.TestCase):
    def test_required_keys_present(self):
        got = manifest.collect_manifest()
        for key in manifest.REQUIRED_KEYS:
            self.assertIn(key, got, key)
        self.assertEqual(got["manifest_version"],
                         manifest.MANIFEST_VERSION)

    def test_json_round_trip(self):
        got = manifest.collect_manifest()
        text = json.dumps(got, sort_keys=True)
        back = json.loads(text)
        self.assertEqual(back["manifest_version"],
                         manifest.MANIFEST_VERSION)

    def test_model_section_has_no_credentials(self):
        model = manifest.collect_manifest()["model"]
        self.assertEqual(model["provider"], "cerebras")
        self.assertIn("qwen", model["model"])
        blob = json.dumps(model).lower()
        self.assertNotIn("api_key", blob)
        self.assertNotIn("apikey", blob)
        self.assertNotIn("secret", blob)
        self.assertNotIn("token", blob)

    def test_live_key_never_in_manifest(self):
        key = os.environ.get("CEREBRAS_API_KEY") or os.environ.get(
            "CEREBRAS")
        if not key:
            self.skipTest("no live key in environment")
        blob = json.dumps(manifest.collect_manifest())
        self.assertNotIn(key, blob)

    def test_sandbox_section_is_honest(self):
        sandbox = manifest.collect_manifest()["sandbox"]
        self.assertFalse(sandbox["os_enforcement"])
        self.assertTrue(sandbox["same_user"])
        self.assertIn("sandbox.py prelude deny", sandbox["layers"])


class CheckTest(unittest.TestCase):
    def test_check_accepts_collected_manifest(self):
        self.assertEqual(
            manifest.check_manifest(manifest.collect_manifest()), [])

    def test_check_rejects_missing_keys(self):
        problems = manifest.check_manifest({"manifest_version": 1})
        self.assertTrue(any("missing key" in p for p in problems))

    def test_cli_check_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, "manifest.py", "--check"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, timeout=120,
            cwd=os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(proc.returncode, 0, proc.stdout.decode(
            "utf-8", "replace"))

    def test_cli_write_round_trip(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "manifest.json")
            proc = subprocess.run(
                [sys.executable, "manifest.py", "--write", out],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, timeout=120, cwd=root)
            self.assertEqual(proc.returncode, 0, proc.stdout.decode(
                "utf-8", "replace"))
            with open(out, encoding="utf-8") as handle:
                back = json.load(handle)
            self.assertEqual(manifest.check_manifest(back), [])


if __name__ == "__main__":
    unittest.main()
