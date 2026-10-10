"""Fixes and features from the todo-list run (session fb7f91a815) and this round:
impure clock rejected, LET -> LET*, tolerant JSON, cheap rewrites, command-line
apps, function metadata for the graph, response cache, the active-run endpoint."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

import agent_session as ag  # noqa: E402
import lispstyle  # noqa: E402
import mount  # noqa: E402
import oracle as orc  # noqa: E402
import server  # noqa: E402
import toolmeta  # noqa: E402
import webkit  # noqa: E402


class PurityTests(unittest.TestCase):
    def test_reading_the_clock_or_random_numbers_is_refused(self):
        clock = ("(defun now-timestamp () (multiple-value-bind (s m h d mo y) "
                 "(get-decoded-time) (encode-universal-time s m h d mo y)))")
        self.assertIn("GET-DECODED-TIME reads the clock", lispstyle.purity_problems(clock)[0])
        self.assertIn("RANDOM", lispstyle.purity_problems("(defun pick (xs) (nth (random 3) xs))")[0])
        self.assertEqual(lispstyle.purity_problems(
            "(defun stamp (task now) (append task (list now)))"), [])
        self.assertEqual(lispstyle.purity_problems(
            '(defun note (x) (format nil "random ~a" x))'), [])


class LetStarTests(unittest.TestCase):
    LIVE = ("(defun add-task (state title)\n"
            "  (let ((tasks (get-tasks state))\n"
            "        (id (new-id tasks))\n"
            "        (now 5))\n"
            "    (list (cons :tasks (append tasks (list (make-task id title now)))))))")

    def test_the_live_unbound_variable_slip_is_fixed(self):
        fixed = lispstyle.let_to_let_star(self.LIVE)
        self.assertIn("(let* ((tasks (get-tasks state))", fixed)
        self.assertEqual(lispstyle.let_to_let_star(fixed), fixed)        # idempotent

    def test_independent_bindings_and_parameter_names_are_left_alone(self):
        same = "(defun f (x) (let ((a (g x)) (b (h x))) (+ a b)))"
        self.assertEqual(lispstyle.let_to_let_star(same), same)
        shadow = "(defun f (x) (let ((x (g x)) (y (h x))) (+ x y)))"     # y means the PARAMETER x
        self.assertEqual(lispstyle.let_to_let_star(shadow), shadow)
        text = '(defun f (s) (let ((a 1) (b "uses a")) (list a b s)))'   # only inside a string
        self.assertEqual(lispstyle.let_to_let_star(text), text)

    def test_normalize_plan_applies_it_and_logs_it(self):
        plan = ag.normalize_plan({"action": "build", "name": "add-task", "description": "d",
                                  "definition": self.LIVE, "call": "(add-task '() \"a\")",
                                  "tests": [{"call": "(add-task '() \"a\")", "expect": "T"}]})
        self.assertIn("(let* ((tasks", plan["definition"])
        self.assertIn("let-to-let-star", plan["auto_fixes"])


class JsonAndHintTests(unittest.TestCase):
    def test_literal_newline_inside_a_json_string_is_accepted(self):
        raw = '{"action":"build","name":"f","definition":"(defun f (x)\n  x)"}'
        with self.assertRaises(ValueError):
            json.loads(raw)
        self.assertEqual(ag.extract_json(raw)["definition"], "(defun f (x)\n  x)")

    def test_one_level_of_parentheses_off_gets_the_cdr_versus_second_hint(self):
        detail = "(get-tasks '((:tasks ((1 \"A\"))))): got (((1 \"A\"))), expected ((1 \"A\"))"
        self.assertIn("extra-nesting", [k for k, _ in orc.lisp_hints(detail)])
        self.assertIn("extra-nesting", [k for k, _ in orc.lisp_hints(
            "(f): got (NIL), expected ()")])
        self.assertNotIn("extra-nesting", [k for k, _ in orc.lisp_hints(
            "(f): got 3, expected 4")])
        store = orc.LessonStore()
        store.record(["extra-nesting"])
        self.assertIn("(SECOND pair)", store.advice(session_keys=["extra-nesting"]))


class NewlineAndStringExpectationTests(unittest.TestCase):
    """Live session 136ab9d7ac: a CORRECT join-lines was rejected."""

    def test_bare_multiword_expected_value_becomes_a_string(self):
        self.assertEqual(ag.quote_bare_string("[ ] 1. A"), '"[ ] 1. A"')
        self.assertEqual(ag.quote_bare_string("No tasks."), '"No tasks."')
        for same in ('"already"', "42", "NIL", "T", "(1 2 3)", "symbol", ":kw", "3.5", "#\\a"):
            self.assertEqual(ag.quote_bare_string(same), same)

    def test_backslash_n_inside_a_string_becomes_a_line_break(self):
        self.assertEqual(ag.newline_escapes('"a\\nb"'), '"a\nb"')
        self.assertEqual(ag.newline_escapes("(f \\n)"), "(f \\n)")        # outside strings: untouched

    def test_returned_line_breaks_are_shown_not_hidden(self):
        self.assertEqual(ag.one_line('(:GOT "a\nb")'), '(:GOT "a\\nb")')
        self.assertEqual(ag.one_line('(:GOT\n (:STATUS 200\n  :BODY "x"))'),
                         '(:GOT (:STATUS 200 :BODY "x"))')

    def test_the_live_join_lines_now_passes_in_sbcl(self):
        plan = ag.normalize_plan({
            "action": "build", "name": "join-lines", "description": "join with newlines",
            "definition": '(defun join-lines (lines) (if (null lines) "" (format nil "~{~a~^~%~}" lines)))',
            "call": "(join-lines '(\"a\" \"b\"))",
            "tests": [{"call": "(join-lines nil)", "expect": '""'},
                      {"call": "(join-lines '(\"a\" \"b\"))", "expect": '"a~%b"'},
                      {"call": "(join-lines '(\"x\" \"y\"))", "expect": '"x\\ny"'}]})
        self.assertIsNone(ag.validate_build(plan))
        checks = " ".join("(gg-check %s '%s)" % (t["call"], t["expect"]) for t in plan["tests"])
        env = ag._worker_fn("%s\n%s\n(list %s (gg-check (join-lines '(\"a\" \"b\")) \"a b\"))"
                            % (ag.GG_CHECK, plan["definition"], checks))
        self.assertTrue(env.get("ok"), env.get("error"))
        results = ag.one_line(env["return_value"])
        self.assertTrue(results.startswith("(T T T (:GOT"), results)   # a wrong value still fails
        self.assertIn("\\n", results)

    def test_new_hints(self):
        keys = lambda text: [k for k, _ in orc.lisp_hints(text)]
        self.assertIn("constant-name", keys("COMMON-LISP:T names a defined constant, and cannot be used"))
        self.assertIn("loop-collect", keys("The function COMMON-LISP-USER::COLLECT is undefined."))
        self.assertIn("~%", lispstyle.STYLE_GUIDE)


class ProjectHygieneTests(unittest.TestCase):
    """Live session 8313d66655: a leftover get-tasks (old data layout) made every
    new function lose the existing tasks; the version using table-rows passed."""

    LEFTOVERS = [
        {"name": "now-timestamp", "description": "clock",
         "definition": "(defun now-timestamp () (get-universal-time))", "tests": []},
        {"name": "get-tasks", "description": "tasks",
         "definition": "(defun get-tasks (state) (cadr (assoc :tasks state)))",
         "tests": [{"call": "(get-tasks '((:tasks ((1 \"A\")))))", "expect": "((1 \"A\"))"}]},
        {"name": "count-tasks", "description": "n",
         "definition": "(defun count-tasks (state) (length (get-tasks state)))", "tests": []},
        {"name": "find-task", "description": "find",
         "definition": "(defun find-task (tasks id) (find id tasks :key #'car))",
         "tests": [{"call": "(find-task '((1 \"A\")) 1)", "expect": "(1 \"A\")"}]},
        {"name": "task-count", "description": "good",
         "definition": "(defun task-count (state) (length (table-rows state \"tasks\")))",
         "tests": [{"call": "(task-count '((\"tasks\" ((1 \"A\")))))", "expect": "1"}]},
    ]

    def test_stale_tools_are_found_with_reasons_and_good_ones_kept(self):
        reasons = ag.stale_tools(webkit.tools() + self.LEFTOVERS)
        self.assertEqual(sorted(reasons), ["count-tasks", "get-tasks", "now-timestamp"])
        self.assertIn("pure functions", reasons["now-timestamp"])
        self.assertIn("by keyword", reasons["get-tasks"])
        self.assertEqual(reasons["count-tasks"], "calls get-tasks, which was retired")

    def test_a_cli_run_retires_them_before_planning_and_keeps_them_on_disk(self):
        prompts = []
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            for t in self.LEFTOVERS:
                reg.add(t)

            def gen(system, user):
                prompts.append(user)
                return ag._fake({"action": "stop"})
            sess = ag.Session("a cli todolist with sqlite", gen, registry=reg,
                              log_path=Path(tmp) / "l.jsonl")
            sess.run()
            retired = next(e for e in sess.events if e["kind"] == "retired")
            self.assertEqual(sorted(t["name"] for t in retired["tools"]),
                             ["count-tasks", "get-tasks", "now-timestamp"])
            names = [t["name"] for t in reg.load()]
            self.assertNotIn("get-tasks", names)
            self.assertIn("find-task", names)
            self.assertEqual(len(reg.load(retired=True)), len(reg.load()) + 3)   # nothing erased
            self.assertNotIn("get-tasks", prompts[0])
            self.assertNotIn("(get-tasks", reg.prelude())

    def test_a_rebuilt_tool_replaces_its_retired_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            reg.add(self.LEFTOVERS[1])
            reg.retire({"get-tasks": "old layout"})
            self.assertEqual(reg.load(), [])
            reg.add({"name": "get-tasks", "description": "new",
                     "definition": "(defun get-tasks (state) (table-rows state \"tasks\"))"})
            self.assertEqual([t["description"] for t in reg.load()], ["new"])
            self.assertEqual(len(reg.load(retired=True)), 1)

    def test_a_plain_prompt_never_retires_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            reg.add(self.LEFTOVERS[0])
            ag.Session("square 12", lambda s, u: ag._fake({"action": "stop"}), registry=reg,
                       log_path=Path(tmp) / "l.jsonl").run()
            self.assertEqual(len(reg.load()), 1)


class SplitStepTests(unittest.TestCase):
    def test_a_step_built_by_its_own_split_is_not_rebuilt(self):
        tool = lambda name, body: {"action": "build", "name": name, "description": "d",
                                   "definition": "(defun %s (x) %s)" % (name, body),
                                   "tests": [{"call": "(%s 1)" % name, "expect": "1"}],
                                   "call": "(%s 1)" % name}
        bad = [tool("big", "(car %s)" % ("(list %s)" % " ".join(["x"] * n))) for n in (1, 2, 3, 4)]
        replies = iter([
            {"action": "plan", "steps": [{"name": "big", "spec": "(big x) -> x"}]},
            bad[0], bad[1], bad[2], bad[3],                       # step + 3 repairs: all fail
            {"action": "stop"},                                   # the property-test rescue declines
            {"action": "plan", "steps": [{"name": "helper", "spec": "(helper x) -> x"},
                                         {"name": "big", "spec": "(big x) -> x"}]},
            tool("helper", "x"), tool("big", "(helper x)"),
            {"action": "use", "call": "(big 1)"}])
        labels = []

        def gen(system, user):
            return ag._fake(next(replies))

        def worker(code):
            fails = "(car (list" in code.split("(gg-check")[0].split("(defun big")[-1]
            return {"ok": True, "stdout": "", "error": "", "timed_out": False, "elapsed_ms": 1.0,
                    "return_value": "(:GOT 9)" if fails else "T"}
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("make big", gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              worker_fn=worker, log_path=Path(tmp) / "l.jsonl")
            sess.run()
            labels = [e["label"] for e in sess.events if e["kind"] == "model_call"]
            self.assertNotIn("retry step", labels)
            self.assertEqual(sess.state, "done", [e for e in sess.events if e["kind"] in ("gave_up", "error")])


class CheapRewriteTests(unittest.TestCase):
    def test_rewrites_vary_temperature_without_thinking_except_the_last_attempt(self):
        build = lambda body: {"action": "build", "name": "f", "description": "d",
                              "definition": "(defun f (x) %s)" % body,
                              "tests": [{"call": "(f 1)", "expect": "2"}], "call": "(f 1)"}
        replies = iter([build("x"), build("(+ x 0)"), build("(* x 1)"), build("(- x 0)"),
                        {"action": "stop"}])
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("make f", lambda s, u: ag._fake(next(replies)),
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              worker_fn=lambda c: {"ok": True, "stdout": "", "error": "",
                                                   "return_value": "(:GOT 9)",
                                                   "timed_out": False, "elapsed_ms": 1.0},
                              log_path=Path(tmp) / "l.jsonl")
            sess.max_calls = 6
            sess.run()
            calls = [e for e in sess.events if e["kind"] == "model_call"]
            rewrites = [c for c in calls if c["label"] == "rewrite"]
            # a step's LAST rewrite thinks (and samples); the one before is plain, at temperature 0
            self.assertEqual([(c["deep"], c["effort"], c["temperature"]) for c in rewrites],
                             [(False, "none", 0.0), (True, "low", 0.7)])
            self.assertGreaterEqual(ag.MAX_DEEP_CALLS, 1)

    def test_a_crash_is_never_rescued_into_property_tests(self):
        plan = {"action": "build", "name": "f", "description": "d",
                "definition": "(defun f (x) (car x))", "call": "(f 1)",
                "tests": [{"call": "(f 1)", "expect": "1"}]}
        replies = iter([plan, plan, plan, plan, {"action": "stop"}])
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("make f", lambda s, u: ag._fake(next(replies)),
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              worker_fn=lambda c: {"ok": False, "stdout": "", "return_value": "",
                                                   "error": "The value 1 is not of type LIST",
                                                   "timed_out": False, "elapsed_ms": 1.0},
                              log_path=Path(tmp) / "l.jsonl")
            sess.max_calls = 6
            sess.run()
            self.assertNotIn("rescue", [e["kind"] for e in sess.events])


CLI_APP = """(defun handle-command (args state now)
  "Run one command over STATE at time NOW; returns (:output text :state new-state)."
  (let ((tasks (table-rows state "tasks")))
    (cond ((equal (first args) "add")
           (list :output (format nil "added ~a" (second args))
                 :state (with-table-rows state "tasks"
                          (append tasks (list (list (1+ (length tasks)) (second args) now))))))
          ((equal (first args) "list")
           (list :output (format nil "~{~a~^~%~}"
                                 (mapcar (lambda (task) (format nil "~a. ~a" (first task) (second task)))
                                         tasks))))
          (t (list :output "usage: add TITLE | list")))))"""


class CommandLineAppTests(unittest.TestCase):
    def test_cli_goal_gets_the_contract_and_only_the_state_helpers(self):
        goal = "createa cli todolist with sqlite, full crud, cli interface, tui interface aswell"
        needs = [g["need"] for g in ag.capability_gaps(goal)]
        self.assertEqual(needs, ["database", "command line"])
        self.assertIn("(handle-command args state now)", ag.capability_note(ag.capability_gaps(goal)))
        prompts = []
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")

            def gen(system, user):
                prompts.append(user)
                return ag._fake({"action": "stop"})
            ag.Session(goal, gen, registry=reg, log_path=Path(tmp) / "l.jsonl").run()
            self.assertEqual(sorted(t["name"] for t in reg.load()), sorted(webkit.STATE_NAMES))
        self.assertIn("COMMAND-LINE APP CONTRACT", prompts[0])
        self.assertIn("never read the clock", prompts[0])

    def test_commands_run_in_sbcl_and_the_state_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            webkit.seed(reg, webkit.STATE_NAMES)
            reg.add({"name": "handle-command", "description": "todo", "definition": CLI_APP})
            app = mount.MountedApp(reg, mount.StateStore(Path(tmp) / "s.sqlite"),
                                   log_path=Path(tmp) / "r.jsonl")
            self.assertEqual(app.run_command(["add", 'Buy "milk"'], now=7),
                             {"ok": True, "output": 'added Buy "milk"', "error": ""})
            app.run_command(["add", "Walk dog"], now=8)
            again = mount.MountedApp(reg, mount.StateStore(Path(tmp) / "s.sqlite"))
            self.assertEqual(again.run_command(["list"])["output"], '1. Buy "milk"\n2. Walk dog')
            self.assertEqual(again.run_command(["nope"])["output"], "usage: add TITLE | list")
            self.assertEqual(mount.report(Path(tmp) / "r.jsonl")["state_writes"], 2)

    def test_project_without_the_tool_explains_what_to_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = mount.MountedApp(ag.ToolRegistry(Path(tmp) / "t.json"),
                                   mount.StateStore(Path(tmp) / "s.sqlite"))
            out = app.run_command(["list"])
            self.assertFalse(out["ok"])
            self.assertIn("(handle-command args state now)", out["error"])


class ToolMetaTests(unittest.TestCase):
    def setUp(self):
        self.tools = webkit.tools() + [
            {"name": "add-post", "description": "add a post",
             "definition": "(defun add-post (request state) (with-state (redirect-to \"/\") "
                           "(with-table-rows state \"posts\" (append (table-rows state \"posts\") "
                           "(list (list (form-value request \"title\") (request-field request :now)))))))"},
            {"name": "handle-request", "description": "router",
             "definition": "(defun handle-request (request state) (add-post request state))"},
            {"name": "double", "description": "twice", "eval_ms": 1.25,
             "definition": "(defun double (x) (* 2 x))"}]
        self.meta = toolmeta.describe_all(self.tools)

    def keys(self, name):
        return {e["key"]: e["via"] for e in self.meta[name]["effects"]}

    def test_a_plain_function_is_pure_with_cpu_latency_only(self):
        self.assertEqual(self.keys("double"), {"pure": None})
        self.assertEqual(self.meta["double"]["latency"], {"kinds": ["cpu"], "cpu_ms": 1.25})

    def test_kit_tools_are_marked_and_describe_their_own_effect(self):
        self.assertIn("kit", self.keys("html-escape"))
        self.assertEqual(self.keys("with-state")["writes-state"], None)
        self.assertEqual(self.keys("with-cookie")["sets-cookie"], None)
        self.assertIn("reads-state", self.keys("table-rows"))

    def test_effects_are_inherited_through_callees_with_the_callee_named(self):
        k = self.keys("add-post")
        self.assertEqual(k["writes-state"], "with-state")
        self.assertEqual(k["http-response"], "redirect-to")
        self.assertEqual(k["uses-time"], None)                       # reads :now itself
        self.assertNotIn("kit", k)                                   # kit-ness is not inherited
        self.assertNotIn("pure", k)
        self.assertEqual(self.meta["add-post"]["latency"]["kinds"], ["cpu", "disk", "network"])

    def test_entry_point_and_call_lists(self):
        self.assertIn("entry-point", self.keys("handle-request"))
        self.assertEqual(self.meta["handle-request"]["calls"], ["add-post"])
        self.assertEqual(self.meta["add-post"]["callers"], ["handle-request"])
        self.assertIn("with-state", self.meta["add-post"]["calls"])

    def test_tools_api_carries_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = ag.SessionManager(ag.ToolRegistry(Path(tmp) / "tools.json"))
            mgr.registry.add({"name": "double", "description": "twice",
                              "definition": "(defun double (x) (* 2 x))"})
            tool = mgr.tools()[0]
            self.assertEqual(tool["meta"]["effects"][0]["key"], "pure")
            self.assertEqual(tool["meta"]["calls"], [])


class ResponseCacheTests(unittest.TestCase):
    def _app(self, tmp, definition):
        reg = ag.ToolRegistry(Path(tmp) / "t.json")
        reg.add({"name": "handle-request", "description": "app", "definition": definition})
        calls = []

        def run(code):
            calls.append(code)
            return {"ok": True, "elapsed_ms": 1.0,
                    "return_value": '(200 NIL "page %d" 0 "NIL")' % len(calls)}
        return mount.MountedApp(reg, mount.StateStore(Path(tmp) / "s.sqlite"), run_lisp=run), calls

    def test_repeat_gets_are_served_without_running_lisp(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, calls = self._app(tmp, "(defun handle-request (request state) state)")
            first = app.handle("GET", "/", {})
            self.assertEqual(app.handle("GET", "/", {}), first)
            self.assertEqual(len(calls), 1)
            app.handle("GET", "/other", {})                       # different request
            app.handle("GET", "/", {"Cookie": "sid=1"})           # different cookie
            app.handle("POST", "/", {}, b"a=1")                   # never cached
            self.assertEqual(len(calls), 4)
            app.store.save('(("posts" ()))')                      # state changed
            app.handle("GET", "/", {})
            self.assertEqual(len(calls), 5)

    def test_a_handler_that_uses_the_time_or_nonce_is_never_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, calls = self._app(
                tmp, "(defun handle-request (request state) (getf request :now))")
            app.handle("GET", "/", {})
            app.handle("GET", "/", {})
            self.assertEqual(len(calls), 2)


class RouteTests(unittest.TestCase):
    def test_active_and_command_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ag.SessionManager(ag.ToolRegistry(Path(tmp) / "tools.json"))
            ctx = SimpleNamespace(root=ROOT, manager=None, agent=agent)
            self.assertEqual(server.dispatch("GET", "/api/agent/active", b"", ctx),
                             (200, {"session_id": None}))
            status, out = server.dispatch("POST", "/api/agent/projects/scratch/command",
                                          json.dumps({"args": ["list"]}).encode(), ctx)
            self.assertEqual(status, 200)
            self.assertFalse(out["ok"])
            self.assertIn("handle-command", out["error"])
            status, out = server.dispatch("POST", "/api/agent/projects/scratch/command",
                                          json.dumps({"args": "list"}).encode(), ctx)
            self.assertIn("list of strings", out["error"])


if __name__ == "__main__":
    unittest.main()
