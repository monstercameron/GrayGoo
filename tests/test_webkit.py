"""The web kit: every tool passes its tests in real SBCL, is seeded once, and composes."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import lispstyle  # noqa: E402
import mount  # noqa: E402
import oracle as orc  # noqa: E402
import webkit  # noqa: E402

# An app written ONLY with kit tools, the way the contract tells the model to.
APP = """(defun handle-request (request state)
  "Route REQUEST over STATE using the web kit; returns a response plist."
  (if (and (string= (request-field request :method) "POST")
           (string= (request-field request :path) "/add"))
      (with-cookie
       (with-state (redirect-to "/")
                   (with-table-rows state "posts"
                                    (append (table-rows state "posts")
                                            (list (list (form-value request "title")
                                                        (request-field request :nonce))))))
       "last" (form-value request "title"))
      (html-page 200 (format nil "<ul>~{<li>~a</li>~}</ul><p>last: ~a</p>"
                             (mapcar (lambda (post) (html-escape (first post)))
                                     (table-rows state "posts"))
                             (html-escape (or (cookie-value request "last") "none"))))))"""


class KitTests(unittest.TestCase):
    def test_every_kit_tool_passes_its_own_tests_in_sbcl(self):
        prelude = "\n".join(t["definition"] for t in webkit.KIT)
        checks = ["(gg-check %s '%s)" % (call, expect)
                  for t in webkit.KIT for call, expect in t["tests"]]
        env = ag._worker_fn("%s\n%s\n(list %s)" % (ag.GG_CHECK, prelude, " ".join(checks)))
        self.assertTrue(env.get("ok"), env.get("error"))
        results = env["return_value"].strip("()").split()
        self.assertEqual(len(checks), 46)          # 28 + 10 for password hashing + 8 for number-from-string
        self.assertEqual(set(results), {"T"}, list(zip(checks, results)))

    def test_every_kit_tool_meets_the_house_rules_and_validates(self):
        for tool in webkit.tools():
            plan = dict(tool, action="build")
            self.assertIsNone(ag.validate_build(ag.normalize_plan(plan)), tool["name"])
            self.assertEqual(lispstyle.purity_problems(tool["definition"]), [], tool["name"])
            self.assertTrue(lispstyle.has_docstring(tool["definition"]), tool["name"])

    def test_seed_adds_missing_tools_once_and_keeps_existing_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json").for_mode("live")
            reg.add({"name": "html-escape", "description": "mine",
                     "definition": "(defun html-escape (s) s)"})
            added = webkit.seed(reg)
            self.assertNotIn("html-escape", added)
            self.assertEqual(len(added), len(webkit.NAMES) - 1)
            self.assertEqual(webkit.seed(reg), [])
            mine = next(t for t in reg.load() if t["name"] == "html-escape")
            self.assertEqual(mine["description"], "mine")

    def test_an_app_written_with_the_kit_runs_when_mounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            webkit.seed(reg)
            reg.add({"name": "handle-request", "description": "app", "definition": APP})
            app = mount.MountedApp(reg, mount.StateStore(Path(tmp) / "s.sqlite"))
            self.assertEqual(app.handle("GET", "/", {})[2], "<ul></ul><p>last: none</p>")
            status, headers, _ = app.handle("POST", "/add", {}, b"title=a+%3Cb%3E")
            self.assertEqual(status, 303)
            self.assertIn(("Set-Cookie", "last=a <b>; HttpOnly; SameSite=Lax; Path=/"), headers)
            status, _, body = app.handle("GET", "/", {"Cookie": "last=a <b>"})
            self.assertEqual(body, "<ul><li>a &lt;b&gt;</li></ul><p>last: a &lt;b&gt;</p>")


class SessionSeedTests(unittest.TestCase):
    def test_web_goal_seeds_the_kit_before_the_first_model_call_and_tells_the_model(self):
        prompts = []

        def gen(system, user):
            prompts.append(user)
            return ag._fake({"action": "stop"})
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            sess = ag.Session("create a ssr blog using sqlite for persistence", gen,
                              registry=reg, log_path=Path(tmp) / "l.jsonl")
            sess.run()
            kinds = [e["kind"] for e in sess.events]
            self.assertLess(kinds.index("kit_seeded"), kinds.index("model_call"))
            self.assertIn("TOOL table-rows (state name)", prompts[0])
            self.assertIn("WEB KIT", prompts[0])
            self.assertIn("never use ASSOC on it", prompts[0])

    def test_plain_goal_does_not_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            sess = ag.Session("square 12", lambda s, u: ag._fake({"action": "stop"}),
                              registry=reg, log_path=Path(tmp) / "l.jsonl")
            sess.run()
            self.assertEqual(reg.load(), [])


class LogDrivenClassifierTests(unittest.TestCase):
    """Failures taken from live session c893707138."""

    def test_keyword_used_as_a_list_gets_the_plist_hint_not_the_flat_data_class(self):
        err = "The value :COOKIES is not of type LIST"
        self.assertIn("plist-as-alist", [k for k, _ in orc.lisp_hints(err)])
        info = {"call": "(get-cookie '(:cookies ((\"sid\" \"abc\"))) \"sid\")", "error": err}
        self.assertFalse(orc.is_flat_data_error(info, ["(table-rows '((\"posts\" ())) \"posts\")"]))

    def test_all_tests_flat_is_caught_using_the_registry_examples(self):
        call = "(get-user '(\"users\" ((\"alice\" \"pw\"))) \"alice\")"
        info = {"call": call, "error": 'The value "users" is not of type LIST', "got": None}
        registry_calls = ["(table-rows '((\"posts\" ((\"Hi\" \"text\"))) (\"users\" ())) \"posts\")"]
        self.assertEqual(orc.failure_class(
            [info], definition="(defun get-user (state name) (table-rows state \"users\"))",
            calls=[call] + registry_calls), "TEST_CALL_INVALID")


class StateShapeTests(unittest.TestCase):
    """Every case is a test call or spec the model wrote in live session 5459fdfe52."""

    def fix(self, call, name, index=0):
        return webkit.fix_state_args(call, name, index)

    def test_single_table_with_outer_parens_dropped(self):
        self.assertEqual(
            self.fix("(session-row '(\"sessions\" ((\"alice\" 100 \"abc\"))) \"abc\")", "session-row"),
            "(session-row '((\"sessions\" ((\"alice\" 100 \"abc\")))) \"abc\")")
        self.assertEqual(self.fix("(render-posts-page '(\"posts\" ()))", "render-posts-page"),
                         "(render-posts-page '((\"posts\" ())))")

    def test_alternating_names_and_rows(self):
        self.assertEqual(
            self.fix("(login-redirect '(\"users\" ((\"alice\" \"pw\")) \"sessions\" ()) \"alice\" 100 \"abc\")",
                     "login-redirect"),
            "(login-redirect '((\"users\" ((\"alice\" \"pw\"))) (\"sessions\" ())) \"alice\" 100 \"abc\")")

    def test_first_table_unwrapped_and_keyword_plist(self):
        self.assertEqual(
            self.fix("(handle-logout '(:cookies ((\"sid\" \"abc\"))) "
                     "'(\"sessions\" ((\"alice\" 100 \"abc\")) (\"users\" ())))", "handle-logout", 1),
            "(handle-logout '(:cookies ((\"sid\" \"abc\"))) "
            "'((\"sessions\" ((\"alice\" 100 \"abc\"))) (\"users\" ())))")
        self.assertEqual(
            self.fix("(handle-login '(:method \"GET\" :path \"/login\") '(:users ((\"a\" \"p\")) :sessions ()))",
                     "handle-login", 1),
            "(handle-login '(:method \"GET\" :path \"/login\") '((\"users\" ((\"a\" \"p\"))) (\"sessions\" ())))")

    def test_call_inside_a_property_test_and_the_request_argument_untouched(self):
        call = ("(let ((r (handle-login '(:method \"POST\" :form ((\"u\" \"a\"))) '(\"users\" ()))))"
                " (and (listp r) (assoc \"Location\" (getf r :headers) :test #'equal)))")
        fixed = self.fix(call, "handle-login", 1)
        self.assertIn("'((\"users\" ()))", fixed)
        self.assertIn("'(:method \"POST\" :form ((\"u\" \"a\")))", fixed)
        self.assertIn(":test #'equal", fixed)

    def test_correct_and_unrecognised_data_is_left_alone(self):
        for call in ("(table-rows '((\"posts\" ((\"Hi\" \"text\"))) (\"users\" ())) \"posts\")",
                     "(table-rows '() \"posts\")", "(table-rows state \"posts\")",
                     "(table-rows '(1 2 3) \"posts\")", "(table-rows (initial-state) \"posts\")"):
            self.assertEqual(self.fix(call, "table-rows"), call)

    def test_normalize_plan_fixes_tests_and_planner_specs_and_logs_it(self):
        plan = ag.normalize_plan({
            "action": "build", "name": "session-row", "description": "find a session",
            "definition": "(defun session-row (state nonce) (table-rows state \"sessions\"))",
            "call": "(session-row '(\"sessions\" ()) \"abc\")",
            "tests": [{"call": "(session-row '(\"sessions\" ()) \"abc\")", "expect": "NIL"}]})
        self.assertEqual(plan["tests"][0]["call"], "(session-row '((\"sessions\" ())) \"abc\")")
        self.assertIn("nested-state-data", plan["auto_fixes"])
        steps = ag.normalize_plan({"action": "plan", "steps": [{
            "name": "current-user",
            "spec": "(current-user request state) -> name or NIL. Example: (current-user "
                    "'(:cookies ((\"sid\" \"abc\"))) '(\"sessions\" ((\"abc\" \"alice\")))) => \"alice\""}]})
        self.assertIn("'((\"sessions\" ((\"abc\" \"alice\"))))", steps["steps"][0]["spec"])

    def test_tools_without_a_state_parameter_are_never_touched(self):
        plan = ag.normalize_plan({
            "action": "build", "name": "pick", "description": "d",
            "definition": "(defun pick (pair) (second pair))", "call": "(pick '(\"a\" (1 2)))",
            "tests": [{"call": "(pick '(\"a\" (1 2)))", "expect": "(1 2)"}]})
        self.assertEqual(plan["tests"][0]["call"], "(pick '(\"a\" (1 2)))")


class MismatchReportTests(unittest.TestCase):
    def test_a_mismatch_printed_over_several_lines_is_reported_as_a_wrong_value(self):
        def worker(code):
            return {"ok": True, "stdout": "", "error": "", "timed_out": False, "elapsed_ms": 1.0,
                    "return_value": "(:GOT\n (:STATUS 200 :BODY\n \"x\"))"}
        replies = iter([{"action": "build", "name": "page", "description": "a page",
                         "definition": "(defun page () (list :status 200 :body \"x\"))",
                         "tests": [{"call": "(page)", "expect": "(:status 404 :body \"x\")"}],
                         "call": "(page)"}, {"action": "stop"}])
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("make a page", lambda s, u: ag._fake(next(replies)),
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              worker_fn=worker, log_path=Path(tmp) / "l.jsonl")
            sess.run()
            verdict = next(e for e in sess.events if e["kind"] == "verdict")
            self.assertEqual(verdict["class"], "IMPLEMENTATION_WRONG")
            self.assertIn('got (:STATUS 200 :BODY "x"), expected (:status 404 :body "x")',
                          verdict["detail"])


if __name__ == "__main__":
    unittest.main()
