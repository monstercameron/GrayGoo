"""Context compaction and the all-REPL evaluation path.

Measured on four live builds of a 36-function app: a third of all input tokens was
the REGISTRY block, repairs sent whole functions back to change a line, and every
regression check started a fresh SBCL process (69 of them, 122 ms each)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import webkit  # noqa: E402


def tool(name, args="(request state)", desc=None, example=True, kit=False):
    t = {"name": name, "description": desc or ("Renders the %s part of the page with all its markup" % name),
         "definition": "(defun %s %s nil)" % (name, args), "session": "web-kit" if kit else "abc123"}
    if example:
        t["tests"] = [{"call": "(%s '(:method \"GET\" :path \"/\" :cookies ((\"sid\" \"abc\"))) "
                               "'((\"products\" ((\"Widget\" \"10.00\" \"d\")))))" % name,
                       "expect": "(:status 200 :body \"...\")"}]
    return t


def big_registry():
    own = [tool("render-part-%d" % n) for n in range(22)]
    kit = [tool(n, "(request name)", "Value of NAME in the REQUEST plist, or NIL", kit=True)
           for n in ("form-value", "query-value", "cookie-value", "request-field", "html-escape", "html-page")]
    return kit + own


class RegistryCompactionTests(unittest.TestCase):
    def test_a_small_registry_is_sent_exactly_as_before(self):
        tools = [tool("a"), tool("b")]
        text, level = ag.compact_registry(tools, "use a", ag.REGISTRY_BUDGET)
        self.assertEqual((text, level), (ag.registry_text(tools, "use a"), 0))
        self.assertEqual(ag.compact_registry([], "x", 10), ("(no tools yet)", 0))
        self.assertEqual(ag.compact_registry(tools, "x", None)[1], 0)

    def test_a_working_call_keeps_what_it_names_in_full_and_shrinks_the_rest(self):
        tools = big_registry()
        focus = "(render-page request state) calls render-part-3 and html-page"
        plain = ag.registry_text(tools, focus)
        text, level = ag.compact_registry(tools, focus, ag.REGISTRY_BUDGET)
        self.assertGreater(len(plain), ag.REGISTRY_BUDGET)
        self.assertLessEqual(len(text), ag.REGISTRY_BUDGET)
        self.assertEqual(level, 1)
        self.assertLess(len(text), 0.6 * len(plain))                      # the point of it
        named = [l for l in text.splitlines() if l.startswith("TOOL ")]
        self.assertEqual(sorted(l.split()[1] for l in named), ["html-page", "render-part-3"])
        self.assertTrue(all("e.g." in l for l in named))                  # with their examples
        for t in tools:                                                   # nothing disappears:
            self.assertIn("%s (" % t["name"], text)                       # every tool stays callable
        self.assertIn("OTHER SAVED TOOLS (callable the same way): ", text)
        self.assertIn("KIT TOOLS (harness helpers, callable the same way): ", text)
        self.assertIn("render-part-7 (request state)", text)
        self.assertNotIn("render-part-7 (request state):", text)          # ...without its description

    def test_the_levels_give_up_examples_then_descriptions(self):
        tools = big_registry()
        focus = " ".join("render-part-%d" % n for n in range(12))         # a call that names a lot
        sizes = []
        for budget in (100000, 2200, 600):
            text, level = ag.compact_registry(tools, focus, budget)
            sizes.append((level, len(text)))
        self.assertEqual([lv for lv, _ in sizes], [0, 2, 3])
        self.assertTrue(all(a[1] >= b[1] for a, b in zip(sizes, sizes[1:])))
        last, _ = ag.compact_registry(tools, focus, 1)                     # over budget even at the end:
        self.assertIn("render-part-0 (request state)", last)              # still returned, still usable

    def test_a_planning_call_sees_every_tool_without_examples(self):
        tools = big_registry()
        plain = ag.registry_text(tools, None)
        text, level = ag.compact_registry(tools, None, ag.REGISTRY_BUDGET_PLAN)
        self.assertEqual(level, 1)
        self.assertLess(len(text), 0.6 * len(plain))
        self.assertNotIn("e.g.", text)
        for n in range(22):
            self.assertIn("TOOL render-part-%d (request state): Renders the render-part-%d part" % (n, n), text)
        self.assertIn("KIT TOOLS (harness helpers, callable the same way): form-value (request name); ", text)
        tiny, lvl = ag.compact_registry(tools, None, 1500)
        self.assertEqual(lvl, 2)
        self.assertLess(len(tiny), len(text))


def worker(code):
    return {"ok": True, "stdout": "", "error": "", "timed_out": False, "elapsed_ms": 1.0,
            "return_value": "(:GOT 9)" if "(gg-check (page " in code and "FIXED" not in code else "T"}


class Run:
    """A session over a big registry with scripted replies; keeps the prompts."""

    def __init__(self, replies, project_note="", run_worker=worker, prompt="make page"):
        self.prompts, it = [], iter(replies)

        def gen(system, user):
            self.prompts.append((system, user))
            return ag._fake(next(it))
        self.tmp = tempfile.TemporaryDirectory()
        reg = ag.ToolRegistry(Path(self.tmp.name) / "t.json")
        for t in big_registry():
            reg.add(t)
        self.sess = ag.Session(prompt, gen, registry=reg, worker_fn=run_worker,
                               log_path=Path(self.tmp.name) / "l.jsonl")
        self.sess.project_note = project_note
        self.sess.max_calls = 8

    def go(self):
        self.sess.run()
        self.events = list(self.sess.events)
        self.tools = {t["name"]: t for t in self.sess.registry.load()}
        self.tmp.cleanup()
        return self


PAGE = ('(defun page (request state) (list :status 200 :body (concatenate (quote string) '
        '"<html><body><h1>Products</h1><table>%s</table><p>BROKEN</p></body></html>")))' % ("<tr><td>x</td></tr>" * 30))
BUILD = {"action": "build", "name": "page", "description": "the page", "definition": PAGE,
         "tests": [{"call": "(page 1 2)", "expect": "T"}], "call": "(page 1 2)"}
NOTE = ("PROJECT: pcrm - a small CRM. Its saved tools are in the REGISTRY below. To ADD a feature, "
        "build new tools that call the existing ones. To CHANGE a tool, build it again under the SAME "
        "name: it replaces the old version only if the tools that call it still pass their tests. "
        "Keep every argument shape used by the existing tools.")


class PromptCompactionTests(unittest.TestCase):
    def test_every_call_logs_what_was_sent_and_what_compaction_saved(self):
        run = Run([BUILD, {"action": "stop"}], project_note=NOTE).go()
        calls = [e for e in run.events if e["kind"] == "model_call"]
        plan, repair = calls[0], calls[1]
        self.assertEqual(plan["context"]["chars"], len(run.prompts[0][1]))
        self.assertGreater(plan["context"]["saved"], 1000)                 # the planner's registry
        self.assertGreater(repair["context"]["saved"], 1000)
        self.assertIn(repair["context"]["level"], (1, 2, 3))
        summary = next(e for e in run.events if e["kind"] == "summary")["compaction"]
        self.assertEqual(summary["prompt_chars"], sum(c["context"]["chars"] for c in calls))
        self.assertEqual(summary["saved_chars"], sum(c["context"]["saved"] for c in calls))

    def test_a_working_call_gets_one_sentence_of_the_project_note_and_the_planner_all_of_it(self):
        run = Run([BUILD, {"action": "stop"}], project_note=NOTE).go()
        self.assertTrue(run.prompts[0][1].startswith(NOTE))
        work = run.prompts[1][1]
        self.assertTrue(work.startswith("PROJECT: pcrm - a small CRM. Change a saved tool by building it "
                                        "again under the SAME name, keeping its argument shapes.\nREGISTRY:"))
        self.assertNotIn("To ADD a feature", work)

    def test_a_schema_failure_is_said_once_with_advice_for_that_failure(self):
        bad = dict(BUILD, tests=[{"call": "(page 1 2", "expect": "#(1 2)"}])
        run = Run([bad, {"action": "stop"}]).go()
        repair = run.prompts[1][1]
        self.assertEqual(repair.count("expected values cannot contain '#'"), 1)
        self.assertNotIn("Failing tests:", repair)
        self.assertIn("Fix exactly what the message above says and change nothing else.", repair)
        self.assertNotIn("Trace the FIRST failing test", repair)

    def test_a_wrong_result_still_gets_the_tracing_advice_and_both_lines(self):
        run = Run([BUILD, {"action": "stop"}]).go()
        repair = run.prompts[1][1]
        self.assertIn("Failing tests: (page 1 2): got 9, expected T (this call returns that value, not T", repair)
        self.assertIn("Trace the FIRST failing test", repair)
        self.assertIn(ag.EDIT_OFFER % "page", repair)


class ShortReplyTests(unittest.TestCase):
    def test_a_repair_sent_as_a_small_edit_is_applied_tested_and_saved(self):
        edit = {"action": "edit", "name": "page", "edits": [{"old": "<p>BROKEN</p>", "new": "<p>FIXED</p>"}]}
        run = Run([BUILD, edit, {"action": "use", "call": "(page 1 2)"}]).go()
        self.assertEqual(run.sess.state, "done", [e for e in run.events if e["kind"] in ("error", "gave_up")])
        self.assertIn("<p>FIXED</p>", run.tools["page"]["definition"])
        self.assertEqual(run.tools["page"]["tests"][0]["call"], "(page 1 2)")     # the tests were kept
        ev = next(e for e in run.events if e["kind"] == "edit")
        self.assertEqual((ev["name"], ev["edits"]), ("page", 1))
        self.assertGreater(ev["saved_chars"], 500)                                 # most of the page was not resent
        self.assertEqual(run.sess._summary()["compaction"]["edits"], 1)
        self.assertEqual(run.sess.model_calls, 2)

    def test_an_edit_that_cannot_be_applied_costs_one_call_not_the_attempt(self):
        edit = {"action": "edit", "name": "page", "edits": [{"old": "<p>NOT THERE</p>", "new": "x"}]}
        fixed = dict(BUILD, definition=PAGE.replace("BROKEN", "FIXED"))
        run = Run([BUILD, edit, fixed, {"action": "use", "call": "(page 1 2)"}]).go()
        self.assertEqual(run.sess.state, "done")
        failed = next(e for e in run.events if e["kind"] == "edit_failed")
        self.assertIn("was not found in the definition", failed["reason"])
        again = run.prompts[2][1]
        self.assertIn("YOUR EDIT COULD NOT BE APPLIED", again)
        self.assertIn("Reply with the complete build JSON instead.", again)
        self.assertEqual([e["attempt"] for e in run.events if e["kind"] == "repair"], [1])   # one repair round
        self.assertEqual(run.sess._summary()["compaction"]["edit_failures"], 1)

    def test_a_repair_may_leave_out_the_tests_it_does_not_change(self):
        partial = {"action": "build", "name": "page", "definition": PAGE.replace("BROKEN", "FIXED")}
        run = Run([BUILD, partial, {"action": "use", "call": "(page 1 2)"}]).go()
        self.assertEqual(run.sess.state, "done")
        self.assertEqual(run.tools["page"]["tests"][0]["call"], "(page 1 2)")
        kept = next(e for e in run.events if e["kind"] == "tests_kept")
        self.assertEqual((kept["name"], kept["tests"]), ("page", 1))
        self.assertIn("tests", kept["kept"])

    def test_a_test_repair_asks_for_tests_only(self):
        self.assertIn("Send ONLY the tests", ag.TEST_SYSTEM)
        self.assertNotIn('"definition"', ag.TEST_SYSTEM.replace("\\", ""))
        bad = dict(BUILD, definition=PAGE.replace("BROKEN", "FIXED"),
                   tests=[{"call": "(page ((((1)", "expect": "T"}])
        only_tests = {"action": "build", "name": "page", "tests": [{"call": "(page 1 2)", "expect": "T"}]}
        run = Run([bad, only_tests, {"action": "use", "call": "(page 1 2)"}]).go()
        self.assertEqual(run.sess.state, "done")
        self.assertIn("with ONLY the corrected tests", run.prompts[1][1])
        self.assertIn("do NOT send it back", run.prompts[1][1])
        self.assertIn("<p>FIXED</p>", run.tools["page"]["definition"])              # the definition was kept

    def test_thinking_is_untouched_by_all_this(self):
        self.assertEqual((ag.MAX_DEEP_CALLS, ag.THINK_TOKENS["low"], ag.THINK_TOKENS_REPAIR,
                          ag.THINK_PLAN_WORDS), (8, 8000, 5000, 12))


class WarmReplTests(unittest.TestCase):
    """Real SBCL: regression checks and answer calls run in the session's one warm process."""

    def _session(self, tmp):
        reg = ag.ToolRegistry(Path(tmp) / "t.json")
        reg.add({"name": "base", "description": "d", "definition": "(defun base (x) (* 2 x))",
                 "tests": [{"call": "(base 2)", "expect": "4"}]})
        reg.add({"name": "top", "description": "d", "definition": "(defun top (x) (+ 1 (base x)))",
                 "tests": [{"call": "(top 2)", "expect": "5"}, {"call": "(top 0)", "expect": "1"}]})
        return ag.Session("x", lambda s, u: ag._fake({"action": "stop"}), registry=reg,
                          log_path=Path(tmp) / "l.jsonl")

    def tearDown(self):
        ag.lispserver.close_all()

    def test_regression_checks_run_warm_find_the_break_and_put_the_saved_version_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._session(tmp)
            good = {"name": "base", "definition": "(defun base (x) (+ x x))"}
            bad = {"name": "base", "definition": "(defun base (x) (* 3 x))"}
            self.assertEqual(sess._regressions(good), [])
            broken = sess._regressions(bad)
            self.assertEqual(broken, ["(top 2) no longer gives 5"])
            evals = [e for e in sess.events if e["kind"] == "repl" and e["label"] == "regression"]
            self.assertEqual(len(evals), 4)
            self.assertTrue(all(e.get("warm") for e in evals))
            self.assertTrue(all(e["code"].startswith("(gg-check (top ") for e in evals))   # only the form
            self.assertLess(sorted(e["elapsed_ms"] for e in evals)[1], 30)                # not a process each
            self.assertEqual(sess._summary()["compaction"]["warm_evals"], 4)
            # the candidate did not stay loaded: the saved version answers again
            self.assertEqual(sess._session_repl().eval("(top 2)")["return_value"], "5")

    def test_an_answer_call_runs_warm_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._session(tmp)
            sess._finish_call("(top 20)", sess.registry.prelude())
            ev = [e for e in sess.events if e["kind"] == "repl" and e["label"] == "answer"][0]
            self.assertEqual((ev["value"], ev.get("warm"), ev["code"]), ("41", True, "(top 20)"))

    def test_injected_workers_keep_the_fresh_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            sess = ag.Session("x", lambda s, u: None, registry=reg, worker_fn=worker,
                              log_path=Path(tmp) / "l.jsonl")
            self.assertIsNone(sess._session_repl())
            env = sess._repl("(+ 1 2)", "answer", form="(+ 1 2)")
            self.assertEqual(env["return_value"], "T")
            self.assertNotIn("warm", [e for e in sess.events if e["kind"] == "repl"][0])


if __name__ == "__main__":
    unittest.main()
