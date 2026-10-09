"""The learning layer: relevance, in-run memory, effectiveness, unknown errors, harness tallies."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import logreview  # noqa: E402
import oracle as orc  # noqa: E402


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = orc.LessonStore(Path(self.tmp.name) / "lessons.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_file_stays_plain_and_ledger_survives_a_restart(self):
        self.store.record(["unbound"], project="blog")
        self.store.record(["unbound", "arity"], project="blog")
        self.assertEqual(self.store.counts(), {"unbound": 2, "arity": 1})
        again = orc.LessonStore(Path(self.tmp.name) / "lessons.json")
        row = next(r for r in again.report()["lessons"] if r["lesson"] == "unbound")
        self.assertEqual(row["projects"], {"blog": 2})

    def test_this_runs_slips_come_first_then_the_projects_then_global(self):
        for _ in range(5):
            self.store.record(["not-list"])                       # globally most frequent
        for _ in range(2):
            self.store.record(["arity"], project="blog")          # frequent in this project
        self.store.record(["key-arg"])                             # seen once: below the bar
        self.assertEqual(self.store.select(limit=2), ["not-list", "arity"])
        self.assertEqual(self.store.select(project="blog", limit=2), ["arity", "not-list"])
        self.assertEqual(self.store.select(project="blog", session_keys=["key-arg"], limit=2),
                         ["key-arg", "arity"])

    def test_in_run_mistakes_are_called_out_in_full(self):
        for _ in range(3):
            self.store.record(["not-list"])
        text = self.store.advice(limit=2, brief=True, session_keys=["plist-as-alist"])
        first, second = text.split("\n")
        self.assertTrue(first.startswith("YOU ALREADY MADE THESE MISTAKES IN THIS RUN"))
        self.assertIn("(getf plist :key)", first)                  # full text, not one sentence
        self.assertTrue(second.startswith("RECURRING SLIPS TO AVOID"))
        self.assertNotIn("two opening", second)                    # brief

    def test_a_warning_that_does_not_work_is_detected_and_no_longer_shortened(self):
        for _ in range(4):
            self.store.mark_shown(["not-list"])
            self.store.record(["not-list"], warned={"not-list"})
        self.assertEqual(self.store.ineffective(), ["not-list"])
        self.assertIn("two opening", self.store.advice(limit=1, brief=True))
        row = self.store.report()["lessons"][0]
        self.assertEqual((row["shown_in_runs"], row["recurred_after_shown"],
                          row["recurrence_rate"]), (4, 4, 1.0))

    def test_errors_without_a_lesson_surface_once_they_repeat(self):
        for _ in range(3):
            self.store.record([], signature="floating point overflow",
                              detail="(f 1e308): floating point overflow", project="blog")
        self.store.record([], signature="one-off thing", detail="x")
        self.assertEqual([s for s, _, _ in self.store.unhandled()], ["floating point overflow"])
        self.assertEqual(self.store.counts(), {})                  # not a lesson yet

    def test_harness_fixes_and_events_are_tallied_and_rendered(self):
        self.store.record_fixes(["nested-state-data", "nested-state-data", "quoted-data-list"])
        self.store.record_harness("invalid-json-reply")
        rep = self.store.report()
        self.assertEqual(rep["harness_fixes"], {"nested-state-data": 2, "quoted-data-list": 1})
        self.assertEqual(rep["harness_events"], {"invalid-json-reply": 1})
        text = logreview.render_learning(rep)
        self.assertIn("nested-state-data 2", text)
        self.assertIn("NO lesson yet", text)

    def test_memory_only_store_supports_the_ledger_too(self):
        mem = orc.LessonStore()
        mem.record(["unbound"], project="p")
        mem.mark_shown(["unbound"])
        self.assertEqual(mem.report()["lessons"][0]["shown_in_runs"], 1)


class SessionLearningTests(unittest.TestCase):
    def test_a_slip_in_step_one_is_warned_about_in_the_next_prompt_of_the_same_run(self):
        prompts = []
        bad = {"action": "build", "name": "f", "description": "d",
               "definition": "(defun f (x) x)", "call": "(f 1)",
               "tests": [{"call": "(f 1)", "expect": "1"}]}
        replies = iter([bad, {"action": "stop"}])

        def gen(system, user):
            prompts.append(user)
            return ag._fake(next(replies))

        def worker(code):
            return {"ok": False, "stdout": "", "return_value": "", "timed_out": False,
                    "elapsed_ms": 1.0, "error": "The value :COOKIES is not of type LIST"}
        store = orc.LessonStore()
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("make f", gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              worker_fn=worker, lessons=store, log_path=Path(tmp) / "l.jsonl")
            sess.project = "blog"
            sess.run()
        self.assertNotIn("YOU ALREADY MADE", prompts[0])
        self.assertIn("YOU ALREADY MADE THESE MISTAKES IN THIS RUN", prompts[1])
        self.assertIn("Never ASSOC a plist", prompts[1])
        rep = store.report()
        self.assertEqual(rep["lessons"][0]["projects"], {"blog": 1})
        self.assertGreaterEqual(rep["lessons"][0]["shown_in_runs"], 1)

    def test_auto_fixes_and_bad_json_reach_the_ledger(self):
        import json
        plan = {"action": "build", "name": "sq", "description": "square",
                "definition": "(defun sq (x) (* x x)))", "call": "(sq 2)",
                "tests": [{"call": "(sq 2)", "expect": "4"}]}
        replies = iter(["{broken", json.dumps(plan)])
        store = orc.LessonStore()
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("square 2", lambda s, u: dict(ag._fake({}), text=next(replies)),
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              worker_fn=lambda c: {"ok": True, "return_value": "T", "error": "",
                                                   "stdout": "", "timed_out": False,
                                                   "elapsed_ms": 1.0},
                              lessons=store, log_path=Path(tmp) / "l.jsonl")
            sess.run()
        rep = store.report()
        self.assertEqual(rep["harness_events"], {"invalid-json-reply": 1})
        self.assertIn("trimmed-surplus-paren", rep["harness_fixes"])


if __name__ == "__main__":
    unittest.main()
