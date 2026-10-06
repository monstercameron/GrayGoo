"""Tests for agent_session: prompt -> tool build -> REPL -> registry reuse.

Uses the offline demo generator and a temp registry; the end-to-end test
drives the real SBCL worker (skipped when SBCL is unavailable).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import workers  # noqa: E402


def _sbcl_available():
    try:
        workers.resolve_sbcl()
        return True
    except Exception:  # noqa: BLE001
        return False


def _kinds(sess):
    return [e["kind"] for e in sess.events]


class PureTests(unittest.TestCase):
    def test_extract_json_tolerates_fences_and_prose(self):
        text = 'Sure!\n```json\n{"action":"use","call":"(f \\"}\\")"}\n```'
        self.assertEqual(ag.extract_json(text)["action"], "use")

    def test_extract_json_rejects_garbage(self):
        with self.assertRaises(ValueError):
            ag.extract_json("no braces here")

    def test_validate_build(self):
        good = {"name": "sq", "definition": "(defun sq (x) (* x x))",
                "tests": [{"call": "(sq 2)", "expect": "4"}],
                "call": "(sq 3)"}
        self.assertIsNone(ag.validate_build(good))
        for key, bad in (("name", "Bad Name"), ("definition", "(+ 1 2)"),
                         ("tests", []), ("call", None)):
            plan = dict(good, **{key: bad})
            self.assertIsNotNone(ag.validate_build(plan), key)

    def test_registry_roundtrip_and_prelude(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "tools.json")
            reg.add({"name": "a", "definition": "(defun a () 1)"})
            reg.add({"name": "b", "definition": "(defun b () 2)"})
            reg.add({"name": "a", "definition": "(defun a () 3)"})
            self.assertEqual([t["name"] for t in reg.load()], ["b", "a"])
            self.assertIn("(defun a () 3)", reg.prelude())

    def test_manager_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = ag.SessionManager(ag.ToolRegistry(Path(tmp) / "t.json"))
            self.assertEqual(mgr.start("  ")[1], "empty prompt")
            self.assertEqual(mgr.start("x", mode="nope")[1], "unknown mode")


class QuoteLiteralsTests(unittest.TestCase):
    def test_quotes_bare_numeric_lists(self):
        q = ag.quote_literals
        self.assertEqual(q("(sum-of-squares (3 4 5))"), "(sum-of-squares '(3 4 5))")
        self.assertEqual(q("(f (-3 4) (1 2))"), "(f '(-3 4) '(1 2))")

    def test_leaves_code_and_quoted_data_alone(self):
        q = ag.quote_literals
        for text in ("(+ 1 2)", "(- 5 3)", "(list 1 2)", "(square 3)",
                     "(f '(1 (2 3)))", "(f (quote (1 2)))", '(f "(1 2)")',
                     "(f nil)", "(square (square 3))"):
            self.assertEqual(q(text), text, text)

    def test_normalize_plan_fixes_calls_and_tests(self):
        plan = {"call": "(f (1 2))", "tests": [{"call": "(f (3 4))", "expect": "7"}]}
        ag.normalize_plan(plan)
        self.assertEqual(plan["call"], "(f '(1 2))")
        self.assertEqual(plan["tests"][0]["call"], "(f '(3 4))")


class EvidenceTests(unittest.TestCase):
    def test_heldout_oracle_answers_are_independent_python(self):
        tasks = ag.heldout_tasks()
        self.assertEqual([t["expected"] for t in tasks],
                         ["35", "5", "12", '"desserts"', "9", "120", "7", "210", "5", "12"])
        self.assertEqual(len(tasks), 10)
        demo_prompts = {"write a function that squares a number, then square 12"}
        self.assertFalse(demo_prompts & {t["prompt"] for t in tasks})

    def test_shipped_repeat_runs_are_complete_and_budgeted(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = ag.SessionManager(ag.ToolRegistry(Path(tmp) / "t.json"))
            rep = [s for s in mgr.snapshots() if s.get("runs")]
            self.assertTrue(rep, "dashboard/evidence/live-repeat-runs.json missing")
            snap = rep[0]
            self.assertFalse(snap["partial"])
            self.assertEqual(len(snap["runs"]), 3)
            for run in snap["runs"]:
                mains = [r for r in run["rows"] if r["arm"] == "main"]
                twins = {r["pair"] for r in run["rows"] if r["arm"] == "nomem"}
                self.assertEqual(len(mains), 17)          # 7 guided + 10 held-out
                self.assertTrue(all(r["session_id"] in twins for r in mains))
                self.assertFalse(any(r["estimated"] for r in run["rows"]))
            spend = sum(r["cost_usd"] for run in snap["runs"] for r in run["rows"])
            self.assertLess(spend, 0.15)                  # inside the approved cap

    def test_shipped_recorded_run_is_listed_and_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = ag.SessionManager(ag.ToolRegistry(Path(tmp) / "t.json"))
            recorded = [s for s in mgr.snapshots() if s["kind"] == "recorded"]
            self.assertTrue(recorded, "dashboard/evidence/live-guided-run.json missing")
            snap = recorded[0]
            self.assertFalse(snap["estimated"])
            main = [r for r in snap["rows"] if r["arm"] == "main" and not r.get("oracle")]
            held = [r for r in snap["rows"] if r["arm"] == "main" and r.get("oracle")]
            twins = {r["pair"] for r in snap["rows"] if r["arm"] == "nomem"}
            self.assertEqual(len(main), 7)
            self.assertEqual(len(held), 10)
            self.assertTrue(all(r["session_id"] in twins for r in main))
            self.assertTrue(all(r["expected_ok"] for r in main))
            # the recording keeps its one honest miss instead of hiding it
            self.assertEqual(sum(1 for r in held if r.get("expected_ok")), 9)
            self.assertTrue(all(r.get("tool") for r in main), "rows carry the tool used")


class CompleteParensTests(unittest.TestCase):
    def test_completes_small_deficits_only(self):
        c = ag.complete_parens
        self.assertEqual(c("(defun f (x) (* x x"), "(defun f (x) (* x x))")
        self.assertEqual(c("(defun f (x) x)"), "(defun f (x) x)")
        self.assertEqual(c("(f x))"), "(f x))")                    # over-closed
        self.assertEqual(c("((((("), "(((((")                        # too far off
        self.assertEqual(c('(f "a (b")'), '(f "a (b")')            # string parens
        self.assertEqual(c("(f 1) ; open (\n(g"), "(f 1) ; open (\n(g)")

    def test_plan_definition_is_completed(self):
        plan = {"definition": "(defun f (x) (* x x", "tests": [], "call": "(f 2)"}
        ag.normalize_plan(plan)
        self.assertEqual(plan["definition"], "(defun f (x) (* x x))")


class SafeCallTests(unittest.TestCase):
    NAMES = {"square", "sum-of-squares"}

    def test_accepts_plain_calls(self):
        for text in ("(square 7)", "(sum-of-squares '(1 2 3))",
                     "(square (square 3))", '(sum-of-squares (list 1 2))'):
            self.assertIsNone(ag.safe_call_check(text, self.NAMES), text)

    def test_rejects_everything_else(self):
        for text in ("(run-program \"cmd\" nil)", "(square (sb-ext:run-program 1))",
                     "(+ 1 2)", "(open \"x\")", "square", "", "(square 1",
                     "(square #.(+ 1 2))", "(square (read))"):
            self.assertIsNotNone(ag.safe_call_check(text, self.NAMES), text)


class ScriptedWorkerTests(unittest.TestCase):
    """Pipeline wiring with a fake worker (no SBCL)."""

    def _worker(self, mapping):
        def fn(code):
            value = mapping(code)
            return {"ok": value is not None, "stdout": "",
                    "return_value": value or "", "error": "" if value else "boom",
                    "timed_out": False, "elapsed_ms": 1.0}
        return fn

    def test_failed_build_repairs_twice_then_fails(self):
        replies = iter([
            {"action": "build", "name": "bad", "description": "d",
             "definition": "(defun bad (x) x)",
             "tests": [{"call": "(bad 1)", "expect": "2"}],
             "call": "(bad 1)"},
        ] * 3)
        gen = lambda s, u: ag._fake(next(replies))  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("make bad", gen,
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              worker_fn=self._worker(lambda c: "1"),
                              log_path=Path(tmp) / "log.jsonl")
            sess.run()
            self.assertEqual(sess.state, "failed")
            self.assertIn("repair", _kinds(sess))
            self.assertNotIn("promoted", _kinds(sess))
            self.assertEqual(sess.model_calls, 3)

    def test_demo_cube_fails_then_repairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            sess = ag.Session("write a function to cube a number",
                              ag.demo_generate, registry=reg,
                              log_path=Path(tmp) / "log.jsonl")
            sess.run()
            self.assertEqual(sess.state, "done", sess.events)
            verdicts = [e for e in sess.events if e["kind"] == "verdict"]
            self.assertEqual([v["ok"] for v in verdicts], [False, True])
            self.assertEqual(
                [e for e in sess.events if e["kind"] == "result"][0]["value"],
                "64")

    def test_failed_answer_call_gets_one_repair(self):
        replies = iter([
            {"action": "use", "call": "(sq 1 2 3)"},         # wrong arity
            {"action": "use", "call": "(sq '(1 2))"},
        ])
        gen = lambda s, u: ag._fake(next(replies))  # noqa: E731

        def worker(code):
            bad = "(sq 1 2 3)" in code
            return {"ok": not bad, "stdout": "",
                    "return_value": "" if bad else "5",
                    "error": "illegal function call" if bad else "",
                    "timed_out": False, "elapsed_ms": 1.0}

        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            reg.add({"name": "sq", "description": "sum of squares",
                     "definition": "(defun sq (xs) 5)"})
            sess = ag.Session("anything", gen, registry=reg, worker_fn=worker,
                              log_path=Path(tmp) / "log.jsonl")
            sess.run()
            self.assertEqual(sess.state, "done", sess.events)
            self.assertEqual(sess.model_calls, 2)
            self.assertIn("repair", _kinds(sess))
            self.assertEqual(
                [e for e in sess.events if e["kind"] == "result"][-1]["value"],
                "5")

    def test_answer_call_must_start_with_a_saved_tool(self):
        replies = iter([
            {"action": "use", "call": "(12)"},                # bogus literal
            {"action": "use", "call": "(sq 12)"},
        ])
        gen = lambda s, u: ag._fake(next(replies))  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            reg.add({"name": "sq", "description": "square", "definition": "(defun sq (x) 1)"})
            sess = ag.Session("anything", gen, registry=reg,
                              worker_fn=self._worker(lambda c: "144"),
                              log_path=Path(tmp) / "log.jsonl")
            sess.run()
            self.assertEqual(sess.state, "done", sess.events)
            results = [e for e in sess.events if e["kind"] == "result"]
            self.assertFalse(results[0]["ok"])
            self.assertTrue(results[-1]["ok"])
            self.assertEqual(sess.model_calls, 2)

    def test_budget_caps_model_calls(self):
        gen = lambda s, u: ag._fake({"action": "wat"})  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", gen,
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              worker_fn=self._worker(lambda c: "1"),
                              log_path=Path(tmp) / "log.jsonl")
            sess.run()
            self.assertEqual(sess.state, "error")
            self.assertEqual(sess.model_calls, 1)


@unittest.skipUnless(_sbcl_available(), "SBCL not available")
class EndToEndTests(unittest.TestCase):
    def test_build_then_compose_then_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")

            def run(prompt):
                s = ag.Session(prompt, ag.demo_generate, registry=reg,
                               log_path=Path(tmp) / "log.jsonl")
                s.run()
                return s

            s1 = run("write a function to square a number")
            self.assertEqual(s1.state, "done", s1.events)
            self.assertIn("promoted", _kinds(s1))
            self.assertEqual(
                [e for e in s1.events if e["kind"] == "result"][0]["value"],
                "144")
            s2 = run("sum of squares of a list")
            self.assertEqual(s2.state, "done", s2.events)
            self.assertEqual(
                [e for e in s2.events if e["kind"] == "result"][0]["value"],
                "50")
            self.assertEqual([t["name"] for t in reg.load()],
                             ["square", "sum-of-squares"])
            s3 = run("sum of squares again")
            direct = ag.SessionManager(reg).call_tool("(sum-of-squares '(1 2 3))")
            self.assertTrue(direct["ok"], direct)
            self.assertEqual(direct["value"], "14")
            self.assertEqual(direct["tokens"], 0)
            self.assertFalse(ag.SessionManager(reg).call_tool("(open \"x\")")["ok"])
            s4 = run("Sum of squares of a LIST!")
            self.assertEqual(s4.state, "done", s4.events)
            self.assertEqual(s4.model_calls, 0)
            self.assertEqual(s4.input_tokens + s4.output_tokens, 0)
            self.assertEqual(
                [e for e in s4.events if e["kind"] == "decision"][0]["action"],
                "cache")
            self.assertLess(s3.input_tokens + s3.output_tokens,
                            s2.input_tokens + s2.output_tokens)
            self.assertNotIn("promoted", _kinds(s3))
            self.assertEqual(
                [e for e in s3.events if e["kind"] == "decision"][0]["action"],
                "use")


if __name__ == "__main__":
    unittest.main()
