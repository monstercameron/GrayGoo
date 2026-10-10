"""The user's own requirements are checked as written against the finished app (real SBCL)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

import agent_session as ag  # noqa: E402
import projects  # noqa: E402
import requirements  # noqa: E402

PROMPT = "a command line greeter: the command hello NAME prints a greeting"


def command(body):
    return {"action": "build", "name": "handle-command", "description": "Runs one command",
            "definition": '(defun handle-command (args state now) "Runs one command." '
                          '(declare (ignore state now)) (list :output %s))' % body,
            "call": "(getf (handle-command '(\"help\") '() 0) :output)",
            "tests": [{"call": "(stringp (getf (handle-command '(\"help\") '() 0) :output))", "expect": "T"}]}


FIRST = command('(if (equal (first args) "hello") "hi" "commands: hello, help")')
FIXED = command('(if (equal (first args) "hello") (format nil "Hello, ~a!" (second args)) '
                '"commands: hello, help")')
REQS = 'run "help" prints "commands: hello"\nrun "hello Ann" prints "Hello, Ann!"\n'


class Script:
    def __init__(self, fixes=True):
        self.fixes, self.prompts, self.fixing = fixes, [], False

    def __call__(self, system, user):
        self.prompts.append(user)
        if "THE FINISHED APP WAS TRIED AND THESE CHECKS FAILED" in user:
            self.fixing = True
            return ag._fake({"action": "plan", "steps": [
                {"name": "handle-command", "spec": "(handle-command args state now) -> plist"}]})
        if "GOAL: (handle-command " in user:
            return ag._fake(FIXED if self.fixing and self.fixes else FIRST)
        return ag._fake({"action": "plan", "steps": [
            {"name": "handle-command", "spec": "(handle-command args state now) -> plist"}]})


def run(script, text):
    tmp = tempfile.TemporaryDirectory()
    sess = ag.Session(PROMPT, script, registry=ag.ToolRegistry(Path(tmp.name) / "t.json"),
                      log_path=Path(tmp.name) / "l.jsonl")
    sess.visual, sess.requirements_text = False, text
    sess.run()
    return sess, tmp


class SessionRequirementTests(unittest.TestCase):
    def test_an_unmet_requirement_is_fixed_and_then_reported_met(self):
        script = Script()
        sess, tmp = run(script, REQS)
        self.addCleanup(tmp.cleanup)
        checks = [e for e in sess.events if e["kind"] == "requirements"]
        self.assertEqual([(c["met"], c["unmet"]) for c in checks][:1], [(1, 1)])   # the first build misses one
        self.assertEqual((checks[-1]["met"], checks[-1]["unmet"], checks[-1]["unchecked"]), (2, 0, 0))
        fix = next(p for p in script.prompts if "THE FINISHED APP WAS TRIED" in p)
        self.assertIn('the user\'s requirement \'run "hello Ann" prints "Hello, Ann!"\' is not met', fix)
        v = sess._summary()["verification"]["requirements"]
        self.assertEqual((v["total"], v["met"], v["unmet"]), (2, 2, 0))
        self.assertEqual(v["fingerprint"], requirements.fingerprint(REQS))        # judged as written
        self.assertEqual(sess._summary()["missing_features"], [])

    def test_a_requirement_that_stays_unmet_is_reported_and_never_dropped(self):
        script = Script(fixes=False)
        sess, tmp = run(script, REQS)
        self.addCleanup(tmp.cleanup)
        summary = sess._summary()
        self.assertEqual(summary["verification"]["requirements"]["unmet"], 1)
        missing = " ".join(summary["missing_features"])
        self.assertIn('your requirement R2, not met: run "hello Ann" prints "Hello, Ann!"', missing)
        self.assertEqual(sess.requirements_text, REQS)                             # untouched
        self.assertEqual(sess._reqs["fingerprint"], requirements.fingerprint(REQS))

    def test_the_planner_is_told_the_requirements_and_a_bad_line_is_not_guessed_at(self):
        script = Script()
        text = REQS + "the greeting should feel friendly\n"
        sess, tmp = run(script, text)
        self.addCleanup(tmp.cleanup)
        self.assertIn('USER REQUIREMENTS - the finished app is checked against these exactly as written', script.prompts[0])
        self.assertIn('run "hello Ann" prints "Hello, Ann!"', script.prompts[0])
        v = sess._summary()["verification"]["requirements"]
        self.assertEqual((v["total"], v["unchecked"]), (3, 1))                     # kept, reported as not checked
        self.assertTrue(any("could not be checked" in m for m in sess._summary()["missing_features"]))

    def test_no_requirements_means_no_check(self):
        script = Script()
        sess, tmp = run(script, "")
        self.addCleanup(tmp.cleanup)
        self.assertNotIn("requirements", [e["kind"] for e in sess.events])
        self.assertIsNone(sess._summary()["verification"]["requirements"])


class ProjectRequirementTests(unittest.TestCase):
    def test_a_project_keeps_the_text_exactly_as_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = projects.ProjectStore(Path(tmp) / "projects")
            proj, _ = store.create("Greeter")
            self.assertEqual(store.requirements(proj["id"]), "")
            text = 'run "help" prints "x"\r\n  # note\r\n'
            self.assertEqual(store.set_requirements(proj["id"], text), (True, None))
            self.assertEqual(store.requirements(proj["id"]), text)
            self.assertEqual(store.set_requirements(proj["id"], "x" * 9000)[0], False)
            self.assertEqual(store.set_requirements("nope-0000", "x"), (False, "unknown project"))
            self.assertEqual(store.set_requirements(proj["id"], 5)[0], False)

    def test_the_api_saves_and_reports_lines_that_do_not_parse(self):
        import json
        import server
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            agent = ag.SessionManager(registry=ag.ToolRegistry(Path(tmp) / "tools.json"))
            proj, _ = agent.projects.create("Greeter")
            ctx = SimpleNamespace(root=ROOT, manager=None, agent=agent)
            path = "/api/agent/projects/%s/requirements" % proj["id"]
            status, data = server.dispatch("POST", path, json.dumps({"text": REQS + "make it nice\n"}).encode(), ctx)
            self.assertEqual((status, data["count"], len(data["errors"])), (200, 3, 1))
            self.assertEqual(data["fingerprint"], requirements.fingerprint(REQS + "make it nice\n"))
            status, data = server.dispatch("GET", path, b"", ctx)
            self.assertEqual((status, data["text"]), (200, REQS + "make it nice\n"))
            self.assertEqual(server.dispatch("GET", "/api/agent/projects/none-0000/requirements", b"", ctx)[0], 404)


if __name__ == "__main__":
    unittest.main()
