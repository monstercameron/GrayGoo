"""Capability notices: what the pure-Lisp sandbox cannot do, read from the goal.

The detector is keyword-only (no model, no worker). The session test uses a
fake model that replies with a malformed plan, so the run ends right after the
planner call without touching SBCL.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402

BLOG = "create a ssr blog using sqlite for persistence and login security"


def _needs(prompt):
    return [g["need"] for g in ag.capability_gaps(prompt)]


def _session(tmp, **kw):
    return ag.Session(kw.pop("prompt", "x"), kw.pop("gen", lambda s, u: None),
                      registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                      log_path=Path(tmp) / "l.jsonl", **kw)


class CapabilityDetectorTests(unittest.TestCase):
    def test_blog_prompt_finds_database_server_and_security(self):
        self.assertEqual(_needs(BLOG), ["database", "web server", "login security"])

    def test_every_gap_has_a_substitute_description(self):
        for g in ag.capability_gaps(BLOG):
            self.assertTrue(g["need"] and g["instead"], g)
        instead = {g["need"]: g["instead"] for g in ag.capability_gaps(BLOG)}
        self.assertIn("saves it in SQLite", instead["database"])
        self.assertIn("mounts it on a port", instead["web server"])
        self.assertIn("not safe for real accounts", instead["login security"])

    def test_plain_algorithm_prompts_return_nothing(self):
        for prompt in ("write a function that squares a number, then square 12",
                       "write a ray tracer",
                       "sum the numbers in a list",
                       "sha256 of abc",
                       "sort a list",
                       ""):
            self.assertEqual(ag.capability_gaps(prompt), [], prompt)

    def test_file_and_network_gaps(self):
        self.assertEqual(_needs("read a file and count its lines"), ["file system"])
        self.assertEqual(_needs("download the page and scrape prices"),
                         ["network connection"])
        self.assertEqual(_needs("call an API and cache the reply"),
                         ["network connection"])

    def test_misspellings_and_case_are_tolerated(self):
        self.assertIn("database", _needs("store it in SQLite lite"))
        self.assertIn("login security", _needs("add sucurity to the form"))
        self.assertIn("login security", _needs("Add LOGIN to the page"))

    def test_hash_of_a_plain_string_is_not_a_security_gap(self):
        self.assertEqual(ag.capability_gaps("hash the string abc with FNV-1a"), [])

    def test_note_is_empty_without_gaps_and_names_each_substitute(self):
        self.assertEqual(ag.capability_note([]), "")
        note = ag.capability_note(ag.capability_gaps(BLOG))
        self.assertIn("CAPABILITY LIMITS", note)
        self.assertIn("database: the app's STATE value", note)
        self.assertIn("(handle-request request state)", note)


class CapabilitySessionTests(unittest.TestCase):
    def _run(self, prompt):
        seen = []

        def gen(system, user):
            seen.append((system, user))
            # malformed plan: the run stops after the planner call, no worker
            return ag._fake({"action": "plan", "steps": "nope"})
        with tempfile.TemporaryDirectory() as tmp:
            sess = _session(tmp, prompt=prompt, gen=gen)
            sess.run()
        return sess, seen

    def test_notice_is_emitted_before_the_first_model_call(self):
        sess, _ = self._run(BLOG)
        kinds = [e["kind"] for e in sess.events]
        self.assertIn("capability_notice", kinds)
        self.assertLess(kinds.index("capability_notice"), kinds.index("model_call"))
        notice = next(e for e in sess.events if e["kind"] == "capability_notice")
        self.assertEqual([g["need"] for g in notice["gaps"]],
                         ["database", "web server", "login security"])

    def test_planner_prompt_carries_the_paragraph_but_system_prompt_does_not(self):
        sess, seen = self._run(BLOG)
        system, user = seen[0]
        self.assertIn("CAPABILITY LIMITS", user)
        self.assertIn("WEB APP CONTRACT", user)
        self.assertNotIn("CAPABILITY LIMITS", system)
        self.assertNotIn("CAPABILITY LIMITS", ag.SYSTEM_PROMPT)

    def test_summary_carries_capability_gaps(self):
        sess, _ = self._run(BLOG)
        summary = next(e for e in sess.events if e["kind"] == "summary")
        self.assertEqual([g["need"] for g in summary["capability_gaps"]],
                         ["database", "web server", "login security"])
        self.assertEqual(summary["outcome"], "failed")  # existing fields unchanged

    def test_ordinary_prompt_has_no_notice_and_empty_gaps(self):
        sess, seen = self._run("write a ray tracer")
        kinds = [e["kind"] for e in sess.events]
        self.assertNotIn("capability_notice", kinds)
        self.assertNotIn("CAPABILITY LIMITS", seen[0][1])
        summary = next(e for e in sess.events if e["kind"] == "summary")
        self.assertEqual(summary["capability_gaps"], [])


class NoiseTests(unittest.TestCase):
    def test_plain_algorithm_prompts_with_web_words_are_not_flagged(self):
        for p in ("write a function that escapes html special characters",
                  "parse a url query string",
                  "count the files in a list of names",
                  "write a function to secure-compare two strings",
                  "add a row to a table in an in-memory db"):
            self.assertEqual(ag.capability_gaps(p), [], p)

    def test_real_needs_are_still_flagged(self):
        need = lambda p: [g["need"] for g in ag.capability_gaps(p)]
        self.assertEqual(need("save the notes to a file"), ["file system"])
        self.assertEqual(need("start an http server on port 80"), ["web server"])
        self.assertEqual(need("create a ssr blog using sqlite for persistence and login security"),
                         ["database", "web server", "login security"])


class UnbalancedCallTests(unittest.TestCase):
    def test_unbalanced_test_call_gets_a_paren_count_reason(self):
        plan = {"action": "build", "name": "f", "description": "d",
                "definition": "(defun f (x) x)",
                "tests": [{"call": "(f '((1 2))", "expect": "((1 2))"}]}
        reason = ag.validate_build(plan)
        self.assertIn("unbalanced parentheses", reason or "")


if __name__ == "__main__":
    unittest.main()
