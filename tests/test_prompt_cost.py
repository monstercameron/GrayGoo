"""Prompt size and speed refinements taken from the logs: focused registry, short
notes for non-planning calls, brief lessons, cheaper rewrites, side-by-side tests."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import oracle as orc  # noqa: E402
import webkit  # noqa: E402


def _tools(n):
    return [{"name": "tool-%d" % i, "description": "does thing number %d for the app" % i,
             "definition": "(defun tool-%d (x) x)" % i,
             "tests": [{"call": "(tool-%d '(1 2 3))" % i, "expect": "(1 2 3)"}]}
            for i in range(n)]


class FocusedRegistryTests(unittest.TestCase):
    def test_small_registry_and_planner_get_every_example(self):
        few = _tools(5)
        self.assertEqual(ag.registry_text(few, "anything").count("e.g."), 5)
        many = _tools(20)
        self.assertEqual(ag.registry_text(many, None).count("e.g."), 20)

    def test_large_registry_is_focused_on_named_and_recent_tools(self):
        many = _tools(20)
        text = ag.registry_text(many, "build something that calls (tool-3 x) and tool-7.")
        full = [ln for ln in text.splitlines() if ln.startswith("TOOL ")]
        self.assertEqual({ln.split()[1] for ln in full},
                         {"tool-3", "tool-7", "tool-16", "tool-17", "tool-18", "tool-19"})
        self.assertIn("OTHER SAVED TOOLS", text)
        for i in range(20):                      # nothing is hidden from the model
            self.assertIn("tool-%d (x)" % i, text)
        self.assertLess(len(text), 0.75 * len(ag.registry_text(many, None)))
        self.assertNotIn("tool-1 ", [ln.split()[1] + " " for ln in full])   # tool-1 != tool-16

    def test_step_prompt_is_much_smaller_than_the_planner_prompt_on_a_web_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            webkit.seed(reg)
            for t in _tools(14):
                reg.add(t)
            sess = ag.Session("make a blog", lambda s, u: ag._fake({"action": "stop"}),
                              registry=reg, log_path=Path(tmp) / "l.jsonl")
            plan = sess._user_prompt(ag.WEB_APP_CONTRACT, full=True)
            step = sess._user_prompt("", goal="(current-user request state) -> name, "
                                               "using cookie-value and table-rows")
            self.assertIn("WEB APP CONTRACT", plan)
            self.assertNotIn("WEB APP CONTRACT", step)
            self.assertIn("WEB APP: REQUEST is a plist", step)
            self.assertIn("TOOL cookie-value (request name)", step)        # named: full line
            self.assertIn("form-value (request name)", step)               # others: compact
            self.assertNotIn("TOOL form-value", step)
            # the planner prompt is itself compacted now, so the gap is smaller than it was
            self.assertLess(len(step), 0.6 * len(plan))
            self.assertLess(len(step), 3600)


class BriefLessonTests(unittest.TestCase):
    def test_brief_advice_is_one_sentence_per_lesson_and_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = orc.LessonStore(Path(tmp) / "lessons.json")
            for _ in range(3):
                store.note(["not-list", "unbound", "arity", "key-arg"])
            full = store.advice()
            brief = store.advice(limit=2, brief=True)
            self.assertTrue(brief.startswith("RECURRING SLIPS TO AVOID"))
            self.assertLess(len(brief), 0.4 * len(full))
            self.assertLessEqual(brief.count(". ") + 1, 4)


class CheaperCallTests(unittest.TestCase):
    def _session(self, tmp, replies, seen):
        it = iter(replies)

        def gen(system, user):
            seen.append((system, getattr(ag._TEMP, "effort", None),
                         getattr(ag._TEMP, "value", None)))
            return ag._fake(next(it))
        return ag.Session("make f", gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                          worker_fn=lambda code: {"ok": True, "stdout": "", "error": "",
                                                  "return_value": "(:GOT 9)",
                                                  "timed_out": False, "elapsed_ms": 1.0},
                          log_path=Path(tmp) / "l.jsonl")

    def test_tests_only_repair_uses_the_short_system_prompt(self):
        self.assertLess(len(ag.TEST_SYSTEM), 0.4 * len(ag.SYSTEM_PROMPT))
        for needle in ("ONE JSON object", "exactly one Lisp", "PRIN1"):
            self.assertIn(needle, ag.TEST_SYSTEM)


class SideBySideTests(unittest.TestCase):
    def test_real_worker_runs_a_tools_tests_concurrently_with_the_same_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", lambda s, u: ag._fake({"action": "stop"}),
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              log_path=Path(tmp) / "l.jsonl")
            codes = ["(+ 1 %d)" % i for i in range(4)]
            t0 = time.perf_counter()
            serial = {c: ag._worker_fn(c)["return_value"] for c in codes}
            t_serial = time.perf_counter() - t0
            t0 = time.perf_counter()
            together = sess._run_side_by_side(codes)
            t_parallel = time.perf_counter() - t0
            self.assertEqual({c: r["return_value"] for c, r in together.items()}, serial)
            self.assertLess(t_parallel, t_serial)

    def test_injected_workers_are_never_parallelised(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", lambda s, u: ag._fake({"action": "stop"}),
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              worker_fn=lambda code: {"ok": True},
                              log_path=Path(tmp) / "l.jsonl")
            self.assertEqual(sess._run_side_by_side(["(a)", "(b)"]), {})


if __name__ == "__main__":
    unittest.main()
