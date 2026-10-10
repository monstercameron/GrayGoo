"""Tests for the provenance audit (provenance.py). Stdlib only.

Temporary projects live in tempfile directories; the one real-registry test
only reads artifacts/agent and writes nothing.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_session
import projects
import provenance
import webkit
from provenance import (AGENT_COMPOSED, AGENT_GENERATED, HARNESS_PRIMITIVE, IMPORTED,
                        audit, classify, main, project_tools, render)


def tool(name, definition, **extra):
    record = {"name": name, "definition": definition, "description": "", "tests": [],
              "mode": "live"}
    record.update(extra)
    return record


def kit_definition(name):
    return next(t["definition"] for t in webkit.KIT if t["name"] == name)


def kit_tool(name, **extra):
    """A kit helper as the harness seeds it (kit flag set, shipped definition)."""
    return tool(name, kit_definition(name), kit=True, session="web-kit", **extra)


class ConstantsTest(unittest.TestCase):
    def test_constants_equal_their_names(self):
        self.assertEqual(HARNESS_PRIMITIVE, "HARNESS_PRIMITIVE")
        self.assertEqual(AGENT_GENERATED, "AGENT_GENERATED")
        self.assertEqual(AGENT_COMPOSED, "AGENT_COMPOSED")
        self.assertEqual(IMPORTED, "IMPORTED")


class ClassifyRulesTest(unittest.TestCase):
    def test_kit_flag_makes_a_harness_primitive(self):
        rec = classify([tool("x", "(defun x () 1)", kit=True)])["x"]
        self.assertEqual(rec["provenance"], HARNESS_PRIMITIVE)
        self.assertFalse(rec["modified_kit"])

    def test_kit_name_with_shipped_definition_is_harness_even_without_flag(self):
        rec = classify([tool("table-rows", kit_definition("table-rows"))])["table-rows"]
        self.assertEqual(rec["provenance"], HARNESS_PRIMITIVE)

    def test_kit_definition_compares_after_collapsing_whitespace(self):
        spaced = "  " + kit_definition("table-rows").replace(" ", "   ").replace("\n", "\n\n")
        rec = classify([tool("table-rows", spaced)])["table-rows"]
        self.assertEqual(rec["provenance"], HARNESS_PRIMITIVE)
        self.assertFalse(rec["modified_kit"])

    def test_rewritten_kit_helper_is_the_models_own_and_flagged(self):
        rewrite = "(defun table-rows (state name) (cdr (assoc name state :test #'equal)))"
        rec = classify([tool("table-rows", rewrite)])["table-rows"]
        self.assertEqual(rec["provenance"], AGENT_GENERATED)
        self.assertTrue(rec["modified_kit"])

    def test_rewritten_kit_helper_that_calls_an_agent_function_is_composed(self):
        rewrite = "(defun table-rows (state) (helper-x state))"
        tools = [tool("table-rows", rewrite), tool("helper-x", "(defun helper-x (s) s)")]
        rec = classify(tools)["table-rows"]
        self.assertEqual(rec["provenance"], AGENT_COMPOSED)
        self.assertTrue(rec["modified_kit"])
        self.assertEqual(rec["calls_agent"], ["helper-x"])

    def test_explicit_provenance_is_kept(self):
        rec = classify([tool("f", "(defun f () 1)", provenance=IMPORTED)])["f"]
        self.assertEqual(rec["provenance"], IMPORTED)
        rec = classify([tool("g", "(defun g () 1)", provenance=AGENT_COMPOSED)])["g"]
        self.assertEqual(rec["provenance"], AGENT_COMPOSED)

    def test_explicit_harness_provenance_on_a_non_kit_tool_is_kept(self):
        rec = classify([tool("h", "(defun h () 1)", provenance=HARNESS_PRIMITIVE)])["h"]
        self.assertEqual(rec["provenance"], HARNESS_PRIMITIVE)

    def test_invalid_explicit_provenance_is_ignored(self):
        rec = classify([tool("h", "(defun h () 1)", provenance="SOMETHING_ELSE")])["h"]
        self.assertEqual(rec["provenance"], AGENT_GENERATED)

    def test_imported_flags_make_an_imported_tool(self):
        self.assertEqual(classify([tool("a", "(defun a () 1)", imported=True)])["a"]["provenance"],
                         IMPORTED)
        self.assertEqual(
            classify([tool("b", "(defun b () 1)", imported_from="elsewhere")])["b"]["provenance"],
            IMPORTED)

    def test_precedence_kit_flag_beats_explicit_provenance(self):
        rec = classify([tool("table-rows", kit_definition("table-rows"), kit=True,
                             provenance=AGENT_GENERATED)])["table-rows"]
        self.assertEqual(rec["provenance"], HARNESS_PRIMITIVE)

    def test_precedence_explicit_provenance_beats_imported_flag(self):
        rec = classify([tool("p", "(defun p () 1)", provenance=AGENT_GENERATED,
                             imported=True)])["p"]
        self.assertEqual(rec["provenance"], AGENT_GENERATED)

    def test_standalone_function_is_generated(self):
        rec = classify([tool("sq", "(defun sq (x) (* x x))")])["sq"]
        self.assertEqual(rec["provenance"], AGENT_GENERATED)
        self.assertEqual(rec["calls_agent"], [])
        self.assertEqual(rec["calls_harness"], [])

    def test_leaning_only_on_harness_primitives_is_generated(self):
        tools = [kit_tool("table-rows"),
                 tool("rows-of", "(defun rows-of (s) (table-rows s \"posts\"))")]
        rec = classify(tools)["rows-of"]
        self.assertEqual(rec["provenance"], AGENT_GENERATED)
        self.assertEqual(rec["calls_harness"], ["table-rows"])
        self.assertEqual(rec["calls_agent"], [])

    def test_calling_another_model_function_is_composed(self):
        tools = [tool("helper-x", "(defun helper-x (s) s)"),
                 tool("outer", "(defun outer (s) (helper-x s))")]
        rec = classify(tools)["outer"]
        self.assertEqual(rec["provenance"], AGENT_COMPOSED)
        self.assertEqual(rec["calls_agent"], ["helper-x"])

    def test_calling_an_imported_function_counts_as_not_harness(self):
        tools = [tool("fmt", "(defun fmt (d) d)", imported=True),
                 tool("outer", "(defun outer (d) (fmt d))")]
        self.assertEqual(classify(tools)["outer"]["provenance"], AGENT_COMPOSED)

    def test_self_call_is_not_another_function(self):
        rec = classify([tool("fact", "(defun fact (n) (if (< n 2) 1 (* n (fact (1- n)))))")])["fact"]
        self.assertEqual(rec["provenance"], AGENT_GENERATED)
        self.assertEqual(rec["calls_agent"], [])

    def test_names_in_strings_and_quoted_data_are_not_calls(self):
        tools = [tool("helper-x", "(defun helper-x (s) s)"),
                 tool("helper-y", "(defun helper-y (s) s)"),
                 tool("helper-z", "(defun helper-z (s) s)"),
                 tool("uses-text",
                      "(defun uses-text (x) (list \"helper-x\" 'helper-z '(helper-y) x))")]
        rec = classify(tools)["uses-text"]
        self.assertEqual(rec["provenance"], AGENT_GENERATED)
        self.assertEqual(rec["calls_agent"], [])

    def test_sharp_quote_and_function_form_are_uses(self):
        tools = [tool("helper-x", "(defun helper-x (s) s)"),
                 tool("helper-y", "(defun helper-y (s) s)"),
                 tool("mapper", "(defun mapper (xs) (mapcar #'helper-x (list (function helper-y) xs)))")]
        rec = classify(tools)["mapper"]
        self.assertEqual(rec["provenance"], AGENT_COMPOSED)
        self.assertEqual(rec["calls_agent"], ["helper-x", "helper-y"])

    def test_local_flet_names_do_not_count_as_saved_calls(self):
        tools = [tool("helper-x", "(defun helper-x (s) s)"),
                 tool("local", "(defun local (s) (flet ((helper-x (a) a)) (helper-x s)))")]
        rec = classify(tools)["local"]
        self.assertEqual(rec["provenance"], AGENT_GENERATED)
        self.assertEqual(rec["calls_agent"], [])

    def test_unparsed_definition_is_generated_with_no_calls(self):
        tools = [tool("helper-x", "(defun helper-x (s) s)"),
                 tool("broken", "(defun broken (x) (helper-x x)")]
        rec = classify(tools)["broken"]
        self.assertEqual(rec["provenance"], AGENT_GENERATED)
        self.assertTrue(rec["unparsed"])
        self.assertEqual(rec["calls_agent"], [])
        self.assertEqual(rec["calls_harness"], [])
        self.assertEqual(rec["chars"], len("(defun broken (x) (helper-x x)"))

    def test_retired_tools_are_ignored_and_their_names_are_not_callees(self):
        tools = [tool("old", "(defun old () 1)", retired="replaced"),
                 tool("caller", "(defun caller () (old))")]
        result = classify(tools)
        self.assertNotIn("old", result)
        self.assertEqual(result["caller"]["calls_agent"], [])

    def test_record_fields(self):
        tools = [kit_tool("table-rows"),
                 tool("f", "(defun f (s) (table-rows s \"p\"))",
                      tests=[{"call": "1"}, {"call": "2"}],
                      auto_fixes=["quoted-spec-example"])]
        rec = classify(tools)["f"]
        self.assertEqual(rec["chars"], len("(defun f (s) (table-rows s \"p\"))"))
        self.assertEqual(rec["tests"], 2)
        self.assertEqual(rec["harness_fixes"], ["quoted-spec-example"])
        self.assertFalse(rec["modified_kit"])
        self.assertNotIn("unparsed", rec)

    def test_harness_fixes_default_to_empty(self):
        self.assertEqual(classify([tool("q", "(defun q () 1)")])["q"]["harness_fixes"], [])


class AuditTest(unittest.TestCase):
    def registry(self):
        """Two kit helpers, four model functions, one imported, one retired."""
        self.h_rows = kit_tool("table-rows")
        self.h_join = kit_tool("join-strings")
        self.a_row = tool("render-row", "(defun render-row (state) (table-rows state \"posts\"))")
        self.a_list = tool("render-list",
                           "(defun render-list (state) (join-strings (mapcar #'render-row (list state)) \" \"))")
        self.a_entry = tool("handle-request", "(defun handle-request (request state) (render-list state))")
        self.a_init = tool("initial-state", "(defun initial-state () '((\"posts\" ())))")
        self.imp = tool("fmt-date", "(defun fmt-date (d) d)", imported=True)
        self.retired = tool("old", "(defun old () 1)", retired="gone")
        return [self.h_rows, self.h_join, self.a_row, self.a_list, self.a_entry,
                self.a_init, self.imp, self.retired]

    def test_exact_numbers_on_a_hand_built_registry(self):
        tools = self.registry()
        result = audit(tools)
        f = result["functions"]
        self.assertEqual(f["total"], 7)
        self.assertEqual(f[HARNESS_PRIMITIVE], 2)
        self.assertEqual(f[AGENT_GENERATED], 2)
        self.assertEqual(f[AGENT_COMPOSED], 2)
        self.assertEqual(f[IMPORTED], 1)

        agent_chars = sum(len(t["definition"]) for t in (self.a_row, self.a_list, self.a_entry, self.a_init))
        total_chars = agent_chars + len(self.h_rows["definition"]) + len(self.h_join["definition"]) \
            + len(self.imp["definition"])
        c = result["chars"]
        self.assertEqual(c["total"], total_chars)
        self.assertEqual(c[HARNESS_PRIMITIVE], len(self.h_rows["definition"]) + len(self.h_join["definition"]))
        self.assertEqual(c[IMPORTED], len(self.imp["definition"]))

        self.assertAlmostEqual(result["agent_share"]["functions"], 4 / 7)
        self.assertAlmostEqual(result["agent_share"]["chars"], agent_chars / total_chars)

    def test_entry_points_harness_reach_and_standing_alone(self):
        result = audit(self.registry())
        self.assertEqual(result["entry_points"], [
            {"name": "handle-request", "provenance": AGENT_COMPOSED},
            {"name": "initial-state", "provenance": AGENT_GENERATED},
        ])
        self.assertEqual(result["harness_reach"]["calls"], 2)
        self.assertEqual(result["harness_reach"]["by_primitive"],
                         {"join-strings": 1, "table-rows": 1})
        self.assertEqual(result["agent_functions_using_harness"], 2)
        self.assertEqual(result["agent_functions_standing_alone"], 1)

    def test_learned_and_not_learned_lists(self):
        result = audit(self.registry())
        self.assertEqual(result["learned"],
                         ["handle-request", "initial-state", "render-list", "render-row"])
        self.assertEqual(result["not_learned"], ["fmt-date", "join-strings", "table-rows"])

    def test_harness_reach_counts_sharp_quote_uses_and_sorts_most_used_first(self):
        tools = [kit_tool("table-rows"), kit_tool("join-strings"),
                 tool("busy", "(defun busy (s) (table-rows s \"a\") (table-rows s \"b\") "
                              "(mapcar #'join-strings (list s)))")]
        reach = audit(tools)["harness_reach"]
        self.assertEqual(reach["calls"], 3)
        self.assertEqual(list(reach["by_primitive"].items()),
                         [("table-rows", 2), ("join-strings", 1)])

    def test_statement_wording_for_the_hand_built_registry(self):
        result = audit(self.registry())
        total_chars = result["chars"]["total"]
        agent_chars = result["chars"][AGENT_GENERATED] + result["chars"][AGENT_COMPOSED]
        pct = round(100 * agent_chars / total_chars)
        self.assertEqual(
            result["statement"],
            "Of 7 functions in this app, 4 were written by the model (%d%% of the code by length), "
            "2 were supplied by the harness and 1 was imported from elsewhere; "
            "the model's functions call the supplied ones 2 times." % pct)

    def test_statement_wording_singular_and_no_imports(self):
        result = audit([tool("sq", "(defun sq (x) (* x x))")])
        self.assertEqual(
            result["statement"],
            "Of 1 function in this app, 1 was written by the model (100% of the code by length), "
            "0 were supplied by the harness; the model's functions call the supplied ones 0 times.")

    def test_empty_registry(self):
        result = audit([])
        self.assertEqual(result["functions"]["total"], 0)
        self.assertIsNone(result["agent_share"]["functions"])
        self.assertIsNone(result["agent_share"]["chars"])
        self.assertEqual(result["entry_points"], [])
        self.assertEqual(result["harness_reach"], {"calls": 0, "by_primitive": {}})
        self.assertEqual(result["learned"], [])
        self.assertEqual(result["not_learned"], [])
        self.assertEqual(result["statement"], "This app has no saved functions yet.")

    def test_render_contains_every_function_name_and_the_statement(self):
        tools = self.registry()
        text = render(audit(tools), classify(tools))
        self.assertIn(audit(tools)["statement"], text)
        for t in tools:
            if not t.get("retired"):
                self.assertIn(t["name"], text)
        for kind in (HARNESS_PRIMITIVE, AGENT_GENERATED, AGENT_COMPOSED, IMPORTED):
            self.assertIn(kind, text)

    def test_render_of_empty_registry_does_not_fail(self):
        text = render(audit([]), {})
        self.assertIn("This app has no saved functions yet.", text)


class ProjectToolsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="prov-test-")
        self.store = projects.ProjectStore(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_tools(self, project_id, tools):
        path = self.store.tools_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(tools), encoding="utf-8")

    def test_unknown_project_raises(self):
        with self.assertRaises(ValueError):
            project_tools("no-such-project", "live", self.dir)

    def test_mode_filter_is_applied_through_the_registry(self):
        project, err = self.store.create("Filter Test")
        self.assertIsNone(err)
        self.write_tools(project["id"], [tool("a", "(defun a () 1)", mode="live"),
                                         tool("b", "(defun b () 1)", mode="demo")])
        self.assertEqual([t["name"] for t in project_tools(project["id"], "live", self.dir)], ["a"])
        self.assertEqual([t["name"] for t in project_tools(project["id"], "demo", self.dir)], ["b"])

    def test_scratch_is_the_builtin_registry(self):
        self.write_tools(projects.BUILTIN, [tool("s", "(defun s () 1)", mode="live")])
        self.assertEqual([t["name"] for t in project_tools(None, "live", self.dir)], ["s"])
        self.assertEqual([t["name"] for t in project_tools("scratch", "live", self.dir)], ["s"])

    def test_missing_registry_file_is_an_empty_list(self):
        self.assertEqual(project_tools(None, "live", self.dir), [])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="prov-main-")
        self.store = projects.ProjectStore(self.dir)
        self.project, _ = self.store.create("Main Test")
        path = self.store.tools_path(self.project["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([
            kit_tool("table-rows"),
            tool("render-row", "(defun render-row (s) (table-rows s \"p\"))", mode="live"),
        ]), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv, agent_dir=self.dir)
        return code, out.getvalue(), err.getvalue()

    def test_json_for_one_project_parses(self):
        code, out, _ = self.run_main([self.project["id"], "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["audit"]["functions"]["total"], 2)
        self.assertEqual(data["functions"]["render-row"]["provenance"], AGENT_GENERATED)

    def test_text_for_one_project_renders(self):
        code, out, _ = self.run_main([self.project["id"]])
        self.assertEqual(code, 0)
        self.assertIn("render-row", out)
        self.assertIn("Of 2 functions in this app", out)

    def test_no_project_prints_one_line_per_project(self):
        code, out, _ = self.run_main([])
        self.assertEqual(code, 0)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)                  # scratch and the one project
        self.assertTrue(lines[0].startswith("scratch"))
        self.assertTrue(any(line.startswith(self.project["id"]) for line in lines))

    def test_no_project_json_parses(self):
        code, out, _ = self.run_main(["--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn(self.project["id"], data)
        self.assertIn("scratch", data)

    def test_unknown_project_is_an_error_message(self):
        code, out, err = self.run_main(["nope-0000"])
        self.assertEqual(code, 2)
        self.assertIn("no such project", err)


class PerformanceTest(unittest.TestCase):
    def test_four_hundred_tools_of_six_hundred_characters_under_three_seconds(self):
        tools = []
        for i in range(400):
            name = "fn-%03d" % i
            pad = "x" * 490
            callee = "fn-%03d" % (i - 1) if i else "table-rows"
            body = "(defun %s (state) \"%s\" (%s state \"posts\") (list state (length state)) (second state))" % (
                name, pad, callee)
            tools.append(tool(name, body))
        self.assertTrue(all(550 <= len(t["definition"]) <= 700 for t in tools))
        started = time.perf_counter()
        result = audit(tools)
        elapsed = time.perf_counter() - started
        self.assertEqual(result["functions"]["total"], 400)
        self.assertLess(elapsed, 3.0, "audit took %.2f s" % elapsed)


class RealRegistryTest(unittest.TestCase):
    """Shape only: the real registries are read, never written."""

    def test_every_real_project_audits_without_raising(self):
        store = projects.ProjectStore(agent_session.AGENT_DIR)
        for meta in store.list():
            tools = project_tools(meta["id"], mode=None)
            result = audit(tools)
            self.assertEqual(set(result["functions"]), {"total", *provenance.PROVENANCES})
            self.assertIsInstance(result["statement"], str)
            render(result, classify(tools))


if __name__ == "__main__":
    unittest.main()
