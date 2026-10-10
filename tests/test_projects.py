"""Projects: CRUD, per-project tool registries, safe refinement, HTTP routes."""
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
import projects  # noqa: E402
import server  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = projects.ProjectStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_builtin_always_exists_and_cannot_change(self):
        self.assertEqual([p["id"] for p in self.store.list()], ["scratch"])
        self.assertEqual(self.store.update("scratch", name="x")[1],
                         "the built-in project cannot be renamed")
        self.assertEqual(self.store.delete("scratch")[1],
                         "the built-in project cannot be deleted")
        self.assertEqual(self.store.tools_path("scratch"), Path(self.tmp.name) / "tools.json")

    def test_create_update_list_delete(self):
        blog, err = self.store.create("  My   Blog ", "SSR blog")
        self.assertIsNone(err)
        self.assertEqual(blog["name"], "My Blog")
        self.assertTrue(blog["id"].startswith("my-blog-"))
        self.assertEqual(self.store.create("my blog")[1], "a project named my blog already exists")
        self.assertEqual(self.store.create("   ")[1], "a project needs a name")
        self.assertIn("too long", self.store.create("x" * 61)[1])
        renamed, err = self.store.update(blog["id"], name="Blog")
        self.assertEqual((renamed["name"], renamed["description"]), ("Blog", "SSR blog"))
        self.assertEqual([p["name"] for p in self.store.list()], ["Scratchpad", "Blog"])
        ok, err = self.store.delete(blog["id"])
        self.assertTrue(ok)
        self.assertFalse(self.store.exists(blog["id"]))
        trashed = list((Path(self.tmp.name) / "projects" / ".trash").iterdir())
        self.assertEqual(len(trashed), 1)                       # moved, not erased
        self.assertTrue((trashed[0] / "project.json").is_file())

    def test_unknown_or_hostile_ids_fall_back_to_builtin(self):
        for bad in ("nope", "../x", "", None, "a/b", "..", "C:\\x"):
            self.assertEqual(self.store.resolve(bad), "scratch", bad)
            self.assertFalse(self.store.exists(bad) and bad != "scratch")


def _worker_ok(code):
    return {"ok": True, "stdout": "", "return_value": "T", "error": "",
            "timed_out": False, "elapsed_ms": 1.0}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = ag.SessionManager(ag.ToolRegistry(Path(self.tmp.name) / "tools.json"))
        self.blog = self.mgr.projects.create("Blog", "a blog")[0]

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_project_has_its_own_tools(self):
        self.mgr.registry_for(self.blog["id"]).add(
            {"name": "ssr-blog", "description": "d", "definition": "(defun ssr-blog (db) db)"})
        self.mgr.registry.add({"name": "square", "description": "d",
                               "definition": "(defun square (x) (* x x))"})
        self.assertEqual([t["name"] for t in self.mgr.tools(project=self.blog["id"])], ["ssr-blog"])
        self.assertEqual([t["name"] for t in self.mgr.tools()], ["square"])
        self.assertEqual([t["name"] for t in self.mgr.tools(project="unknown-id")], ["square"])
        self.assertEqual(self.mgr.projects.get(self.blog["id"])["tools"], 1)

    def test_the_scripted_demo_model_is_refused_outside_scratchpad(self):
        # A demo ray tracer run inside a real project used to be filed under that project.
        sid, err = self.mgr.start("write a ray tracer", mode="demo", project=self.blog["id"])
        self.assertIsNone(sid)
        self.assertIn("only works in Scratchpad", err)
        self.assertFalse(self.mgr.busy())
        self.assertEqual(self.mgr.tools(project=self.blog["id"]), [])
        self.assertEqual(self.mgr._sessions, {})                 # nothing was started

    def test_a_project_counts_its_own_functions_per_model(self):
        reg = self.mgr.registry_for(self.blog["id"])
        reg.for_mode("live").add({"name": "home-page", "description": "d",
                                  "definition": "(defun home-page (s) s)"})
        reg.for_mode("live").add({"name": "old-page", "description": "d",
                                  "definition": "(defun old-page (s) s)"})
        reg.for_mode("live").retire({"old-page": "is a leftover"})
        webkit_tool = {"name": "html-page", "description": "d", "kit": True,
                       "definition": "(defun html-page (s b) b)"}
        reg.for_mode("live").add(webkit_tool)
        reg.for_mode("demo").add({"name": "vec-dot", "description": "d",
                                  "definition": "(defun vec-dot (a b) 0)"})
        got = self.mgr.projects.get(self.blog["id"])["counts"]
        self.assertEqual(got, {"demo": 1, "live": 1})        # kit and retired are not the project's own
        self.assertEqual(self.mgr.projects.get("scratch")["counts"], {"demo": 0, "live": 0})

    def test_project_note_tells_the_model_how_to_refine(self):
        note = self.mgr.project_note(self.blog["id"])
        self.assertIn("PROJECT: Blog - a blog", note)
        self.assertIn("SAME name", note)
        self.assertEqual(self.mgr.project_note("scratch"), "")

    def test_session_in_a_project_sees_its_tools_and_logs_the_project(self):
        prompts = []

        def gen(system, user):
            prompts.append(user)
            return ag._fake({"action": "build", "name": "title-of", "description": "post title",
                             "definition": "(defun title-of (post) (first post))",
                             "tests": [{"call": "(title-of '(\"a\" \"b\"))", "expect": "\"a\""}],
                             "call": "(title-of '(\"a\" \"b\"))"})
        reg = self.mgr.registry_for(self.blog["id"])
        log = Path(self.tmp.name) / "s.jsonl"
        sess = ag.Session("add a title helper", gen, registry=reg, worker_fn=_worker_ok,
                          log_path=log)
        sess.project, sess.project_note = self.blog["id"], self.mgr.project_note(self.blog["id"])
        sess.run()
        self.assertEqual(sess.state, "done", sess.events)
        self.assertTrue(prompts[0].startswith("PROJECT: Blog"))
        self.assertEqual(ag.row_from_log(log)["project"], self.blog["id"])
        self.assertEqual([t["name"] for t in self.mgr.tools(project=self.blog["id"])], ["title-of"])
        self.assertEqual(self.mgr.tools(), [])


class RegressionGuardTests(unittest.TestCase):
    """A follow-up may replace a tool only if the tools that call it still pass."""

    def _session(self, tmp, replies, worker):
        reg = ag.ToolRegistry(Path(tmp) / "t.json")
        reg.add({"name": "render-post", "description": "html for a post",
                 "definition": "(defun render-post (title body) (list title body))",
                 "tests": [{"call": "(render-post \"a\" \"b\")", "expect": "(\"a\" \"b\")"}]})
        reg.add({"name": "ssr-blog", "description": "page",
                 "definition": "(defun ssr-blog (posts) (mapcar (lambda (p) (render-post (first p) (second p))) posts))",
                 "tests": [{"call": "(ssr-blog '((\"a\" \"b\")))", "expect": "((\"a\" \"b\"))"}]})
        it = iter([{"action": "none"}] + replies)      # first call is the quick-reuse check
        return ag.Session("change render-post", lambda s, u: ag._fake(next(it)),
                          registry=reg, worker_fn=worker, log_path=Path(tmp) / "l.jsonl"), reg

    NEW = {"action": "build", "name": "render-post", "description": "html for a post",
           "definition": "(defun render-post (title body) (list body title))",
           "tests": [{"call": "(render-post \"a\" \"b\")", "expect": "(\"b\" \"a\")"}],
           "call": "(render-post \"a\" \"b\")"}

    def test_replacement_that_breaks_a_caller_is_refused(self):
        def worker(code):                       # the caller's old expectation now fails
            bad = "(gg-check (ssr-blog" in code
            return dict(_worker_ok(code), return_value="(:GOT x)" if bad else "T")
        with tempfile.TemporaryDirectory() as tmp:
            sess, reg = self._session(tmp, [self.NEW, {"action": "stop"}], worker)
            sess.run()
            verdicts = [e for e in sess.events if e["kind"] == "verdict"]
            self.assertEqual(verdicts[-1]["class"], "REGRESSION")
            self.assertIn("ssr-blog", verdicts[-1]["detail"])
            kept = next(t for t in reg.load() if t["name"] == "render-post")
            self.assertIn("(list title body)", kept["definition"])      # old version kept

    def test_replacement_that_keeps_callers_passing_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess, reg = self._session(tmp, [self.NEW], _worker_ok)
            sess.run()
            self.assertEqual(sess.state, "done", sess.events)
            new = next(t for t in reg.load() if t["name"] == "render-post")
            self.assertIn("(list body title)", new["definition"])
            self.assertIn("regression", [e.get("label") for e in sess.events if e["kind"] == "repl"])


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        agent = ag.SessionManager(ag.ToolRegistry(Path(self.tmp.name) / "tools.json"))
        self.ctx = SimpleNamespace(root=ROOT, manager=None, agent=agent)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        return server.dispatch(method, path, json.dumps(body).encode() if body is not None else b"",
                               self.ctx)

    def test_crud_over_http(self):
        status, out = self.call("POST", "/api/agent/projects", {"name": "Blog", "description": "x"})
        self.assertEqual(status, 200)
        pid = out["project"]["id"]
        self.assertEqual(self.call("POST", "/api/agent/projects", {"name": ""})[0], 400)
        names = [p["name"] for p in self.call("GET", "/api/agent/projects")[1]["projects"]]
        self.assertEqual(names, ["Scratchpad", "Blog"])
        status, out = self.call("POST", "/api/agent/projects/" + pid, {"description": "new"})
        self.assertEqual((status, out["project"]["description"]), (200, "new"))
        self.assertEqual(self.call("POST", "/api/agent/projects/nope", {"name": "a"})[0], 404)
        self.assertEqual(self.call("DELETE", "/api/agent/projects/scratch")[0], 400)
        self.assertEqual(self.call("DELETE", "/api/agent/projects/" + pid), (200, {"ok": True}))
        self.assertEqual(self.call("DELETE", "/api/agent/projects/" + pid)[0], 404)

    def test_tools_history_and_reset_are_scoped_to_the_project(self):
        pid = self.call("POST", "/api/agent/projects", {"name": "Blog"})[1]["project"]["id"]
        self.ctx.agent.registry_for(pid).add(
            {"name": "ssr-blog", "description": "d", "definition": "(defun ssr-blog (db) db)"})
        self.ctx.agent.registry.add({"name": "square", "description": "d",
                                     "definition": "(defun square (x) (* x x))"})
        tools = lambda q: [t["name"] for t in self.call("GET", "/api/agent/tools" + q)[1]["tools"]]
        self.assertEqual(tools("?project=" + pid), ["ssr-blog"])
        self.assertEqual(tools(""), ["square"])
        self.assertEqual(self.call("GET", "/api/agent/history?project=" + pid)[0], 200)
        self.call("POST", "/api/agent/reset", {"project": pid})
        self.assertEqual(tools("?project=" + pid), [])
        self.assertEqual(tools(""), ["square"])


class StyleTests(unittest.TestCase):
    def test_docstring_is_added_from_the_description_once(self):
        out = lispstyle.ensure_docstring("(defun sq (x) (* x x))", 'square "x"')
        self.assertEqual(out, "(defun sq (x)\n  \"square 'x'\" (* x x))")
        self.assertEqual(lispstyle.ensure_docstring(out, "other"), out)
        const = '(defun greeting () "hi")'                       # a string RESULT is not a docstring
        self.assertIn('"says hi" "hi")', lispstyle.ensure_docstring(const, "says hi"))

    def test_purity_rules(self):
        ok = "(defun top (xs) (first (sort (copy-list xs) #'>)))"
        self.assertEqual(lispstyle.purity_problems(ok), [])
        self.assertIn("SORT destroys its argument xs",
                      lispstyle.purity_problems("(defun top (xs) (first (sort xs #'>)))")[0])
        self.assertIn("global state",
                      lispstyle.purity_problems("(defvar *n* 0) (defun f () *n*)")[0])
        self.assertIn("modifies the global *count*",
                      lispstyle.purity_problems("(defun f () (incf *count*))")[0])
        self.assertIn("prints", lispstyle.purity_problems("(defun f (x) (print x) x)")[0])
        self.assertEqual(lispstyle.purity_problems('(defun f (x) (format nil "~a (print x)" x))'), [])

    def test_secret_comparisons_must_be_exact(self):
        loose = "(defun pw-match (stored given) (string-equal stored given))"
        self.assertIn("ignores letter case", lispstyle.security_problems("pw-match", "", loose)[0])
        self.assertEqual(lispstyle.security_problems(
            "pw-match", "", "(defun pw-match (a b) (string= a b))"), [])
        self.assertEqual(lispstyle.security_problems(
            "name-match", "compare user names", "(defun name-match (a b) (string-equal a b))"), [])

    def test_validate_build_enforces_the_hard_rules_and_prompt_carries_the_guide(self):
        plan = {"action": "build", "name": "pw-match", "description": "compare passwords",
                "definition": "(defun pw-match (a b) (string-equal a b))",
                "tests": [{"call": "(pw-match \"a\" \"a\")", "expect": "T"}],
                "call": "(pw-match \"a\" \"a\")"}
        self.assertIn("unsafe secret comparison", ag.validate_build(plan))
        plan2 = dict(plan, name="top", description="largest",
                     definition="(defun top (xs) (first (sort xs #'>)))",
                     tests=[{"call": "(top '(1 2))", "expect": "2"}], call="(top '(1 2))")
        self.assertIn("not a pure function", ag.validate_build(plan2))
        self.assertIn("PURE function", ag.SYSTEM_PROMPT)

    def test_style_notes_are_soft(self):
        loopy = ("(defun esc (s) (let ((out \"\")) (loop for c across s do "
                 "(setf out (concatenate 'string out (string c)))) out))")
        self.assertTrue(any("SETF in a loop" in n for n in lispstyle.style_notes(loopy)))


if __name__ == "__main__":
    unittest.main()
