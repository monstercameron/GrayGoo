"""The web kit's password hashing: SHA-256, hash-password and password-matches-p.

Every kit test of the three entries runs in real SBCL; SHA-256 is checked on 20
random strings against hashlib in one SBCL process; hash-password is checked
against a Python reimplementation of the same iteration.
"""
import hashlib
import random
import re
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import webkit  # noqa: E402
import workers  # noqa: E402

AUTH_NAMES = ("sha256-hex", "hash-password", "password-matches-p")


def _sbcl_available():
    exe = workers.resolve_sbcl()
    return bool(shutil.which(exe) or Path(exe).exists())


def _entries():
    return [t for t in webkit.KIT if t["name"] in AUTH_NAMES]


def _definitions():
    return "\n".join(t["definition"] for t in _entries())


def _code_points(text):
    return "'(%s)" % " ".join(str(ord(ch)) for ch in text)


def _python_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _python_hash_password(password, salt):
    digest = _python_sha256(salt + ":" + password)
    for _ in range(1000):
        digest = _python_sha256(digest + ":" + salt)
    return digest


def _top_level_forms(text):
    """How many complete top-level forms TEXT holds (parentheses outside strings)."""
    depth, forms, in_string, escaped = 0, 0, False, False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                forms += 1
            elif depth < 0:
                return -1
    return forms if depth == 0 else -1


@unittest.skipUnless(_sbcl_available(), "SBCL is not installed")
class AuthKitInSbclTests(unittest.TestCase):
    def test_every_auth_kit_test_passes_in_sbcl(self):
        checks = ["(gg-check %s '%s)" % (call, expect)
                  for t in _entries() for call, expect in t["tests"]]
        env = ag._worker_fn("%s\n%s\n(list %s)" % (ag.GG_CHECK, _definitions(), " ".join(checks)))
        self.assertTrue(env.get("ok"), env.get("error"))
        results = env["return_value"].strip("()").split()
        self.assertEqual(len(results), len(checks), env["return_value"])
        self.assertEqual(set(results), {"T"}, list(zip(checks, results)))

    def test_sha256_matches_hashlib_on_twenty_random_strings(self):
        rng = random.Random(7)
        pool = list(range(32, 127)) + [233, 252, 8364, 0x4E2D, 0x1F600]
        texts = ["".join(chr(rng.choice(pool)) for _ in range(rng.randint(0, 300)))
                 for _ in range(20)]
        calls = ["(sha256-hex (map 'string #'code-char %s))" % _code_points(text)
                 for text in texts]
        sha_definition = next(t["definition"] for t in _entries() if t["name"] == "sha256-hex")
        env = ag._worker_fn("%s\n(list %s)" % (sha_definition, " ".join(calls)))
        self.assertTrue(env.get("ok"), env.get("error"))
        got = re.findall(r'"([0-9a-f]{64})"', env["return_value"])
        self.assertEqual(got, [_python_sha256(text) for text in texts])

    def test_hash_password_matches_the_python_iteration(self):
        pairs = [("password", "s1"), ("", "salt"), ("p" * 150, "x"),
                 (chr(233) + chr(8364), "s1")]
        calls = ["(hash-password (map 'string #'code-char %s) (map 'string #'code-char %s))"
                 % (_code_points(password), _code_points(salt)) for password, salt in pairs]
        env = ag._worker_fn("%s\n(list %s)" % (_definitions(), " ".join(calls)))
        self.assertTrue(env.get("ok"), env.get("error"))
        got = re.findall(r'"([0-9a-f]{64})"', env["return_value"])
        self.assertEqual(got, [_python_hash_password(p, s) for p, s in pairs])


class AuthKitSourceTests(unittest.TestCase):
    def test_each_auth_definition_is_exactly_one_top_level_form(self):
        self.assertEqual(sorted(t["name"] for t in _entries()), sorted(AUTH_NAMES))
        for t in _entries():
            self.assertEqual(_top_level_forms(t["definition"]), 1, t["name"])
            self.assertTrue(t["definition"].startswith("(defun " + t["name"] + " "), t["name"])

    def test_auth_definitions_avoid_loading_evaluation_and_implementation_escapes(self):
        for t in _entries():
            lowered = t["definition"].lower()
            for banned in ("require", "load", "sb-", "#.", "eval"):
                self.assertNotIn(banned, lowered, "%s contains %r" % (t["name"], banned))

    def test_usage_tells_the_model_how_to_store_and_check_passwords(self):
        self.assertIn("hash-password", webkit.USAGE)
        self.assertIn("password-matches-p", webkit.USAGE)


if __name__ == "__main__":
    unittest.main()
