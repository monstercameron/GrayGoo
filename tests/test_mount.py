"""mount: a project's (handle-request request state) served over HTTP, state in SQLite."""
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import mount  # noqa: E402
import s_expr  # noqa: E402

# A tiny app written the way the contract asks: one pure function.
NOTES_APP = """(defun handle-request (request state)
  "Route REQUEST over STATE; returns a response plist."
  (let ((notes (second (assoc "notes" state :test #'string=))))
    (if (and (string= (getf request :method) "POST") (string= (getf request :path) "/add"))
        (list :status 303 :headers '(("Location" "/")) :body ""
              :state (list (list "notes"
                                 (append notes (list (second (assoc "text" (getf request :form)
                                                                    :test #'string=)))))))
        (list :status 200 :headers '(("Content-Type" "text/plain"))
              :body (format nil "~a notes: ~{~a~^, ~}" (length notes) notes)))))"""


class PureHelperTests(unittest.TestCase):
    def test_request_plist_is_valid_lisp_with_every_field(self):
        src = mount.request_plist("POST", "/add?x=1&y=a%20b", {"Cookie": "sid=abc; t=1"},
                                  b"text=he+said+%22hi%22&n=2", now=5, nonce="ff")
        parsed = s_expr.parse(src)
        fields = dict(zip(parsed[0::2], parsed[1::2]))
        self.assertEqual(fields[":method"], "POST")
        self.assertEqual(fields[":path"], "/add")
        self.assertEqual(fields[":query"], [["x", "1"], ["y", "a b"]])
        self.assertEqual(fields[":form"], [["text", 'he said "hi"'], ["n", "2"]])
        self.assertEqual(fields[":cookies"], [["sid", "abc"], ["t", "1"]])
        self.assertEqual((fields[":now"], fields[":nonce"]), (5, "ff"))

    def test_to_lisp_round_trips_data_and_refuses_reader_tricks(self):
        src = '(("posts" (("a \\"q\\"" "b\\\\c"))) ("n" 3) :key sym)'
        self.assertEqual(mount.to_lisp(s_expr.parse(src)), src)
        for bad in ("#.(run-program)", "|x y|"):
            with self.assertRaises((ValueError, s_expr.SExprError)):
                mount.to_lisp(s_expr.parse("(a %s)" % bad))

    def test_read_response_checks_the_contract(self):
        ok = '(200 (("Content-Type" "text/html")) "<p>hi</p>" 1 "((\\"posts\\" ()))")'
        self.assertEqual(mount.read_response(ok),
                         (200, [("Content-Type", "text/html")], "<p>hi</p>", '(("posts" ()))'))
        self.assertIsNone(mount.read_response('(200 NIL "x" 0 "NIL")')[3])    # state untouched
        self.assertEqual(mount.read_response('(200 NIL "x" 1 "NIL")')[3], "nil")
        for bad in ('(999 NIL "x" 0 "NIL")', '(200 NIL 5 0 "NIL")', '"just text"',
                    '(200 (("X" "a\\nSet-Cookie: evil")) "x" 0 "NIL")',
                    '(200 (("Bad Name" "v")) "x" 0 "NIL")'):
            with self.assertRaises(ValueError):
                mount.read_response(bad)

    def test_contract_text_names_the_function_and_the_plists(self):
        for needle in ("(handle-request request state)", ":status", ":state", ":nonce"):
            self.assertIn(needle, mount.CONTRACT)


class MountedAppTests(unittest.TestCase):
    """Real SBCL: the Lisp handler runs for every request."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.reg = ag.ToolRegistry(tmp / "tools.json")
        self.reg.add({"name": "handle-request", "description": "notes app",
                      "definition": NOTES_APP})
        self.log = tmp / "requests.jsonl"
        self.app = mount.MountedApp(self.reg, mount.StateStore(tmp / "state.sqlite"),
                                    log_path=self.log)

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_flows_through_the_pure_handler_and_persists(self):
        status, headers, body = self.app.handle("GET", "/", {})
        self.assertEqual((status, body), (200, "0 notes: "), body)
        self.assertIn(("Content-Type", "text/plain"), headers)
        status, headers, _ = self.app.handle("POST", "/add", {}, b"text=milk+%26+%22eggs%22")
        self.assertEqual((status, headers), (303, [("Location", "/")]))
        self.assertEqual(self.app.handle("GET", "/", {})[2], '1 notes: milk & "eggs"')
        again = mount.MountedApp(self.reg, mount.StateStore(Path(self.tmp.name) / "state.sqlite"))
        self.assertEqual(again.handle("GET", "/", {})[2], '1 notes: milk & "eggs"')   # restart

    def test_a_failing_handler_gives_500_keeps_the_state_and_is_reported(self):
        self.app.handle("POST", "/add", {}, b"text=keep")
        self.reg.add({"name": "handle-request", "description": "broken",
                      "definition": "(defun handle-request (request state) (car 5))"})
        status, _, body = self.app.handle("GET", "/", {})
        self.assertEqual(status, 500)
        self.assertIn("saved state was not changed", body)
        self.assertEqual(self.app.store.load(), '(("notes" ("keep")))')
        rep = mount.report(self.log)
        self.assertEqual((rep["requests"], rep["state_writes"], len(rep["errors"])), (2, 1, 1))

    def test_project_without_a_handler_says_what_to_build(self):
        empty = ag.ToolRegistry(Path(self.tmp.name) / "none.json")
        app = mount.MountedApp(empty, mount.StateStore(Path(self.tmp.name) / "s2.sqlite"))
        status, _, body = app.handle("GET", "/", {})
        self.assertEqual(status, 503)
        self.assertIn("(handle-request request state)", body)

    def test_initial_state_tool_seeds_the_first_request(self):
        self.reg.add({"name": "initial-state", "description": "seed",
                      "definition": "(defun initial-state () '((\"notes\" (\"seeded\"))))"})
        self.assertEqual(self.app.handle("GET", "/", {})[2], "1 notes: seeded")


class ManagerTests(unittest.TestCase):
    def test_start_serves_on_a_port_and_stop_closes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "tools.json").for_mode("live")
            reg.add({"name": "handle-request", "description": "notes app", "definition": NOTES_APP})
            mgr = mount.MountManager(tmp, lambda pid: ag.ToolRegistry(Path(tmp) / "tools.json"))
            info, err = mgr.start("scratch")
            self.assertIsNone(err)
            self.assertEqual(mgr.start("scratch")[0], info)              # idempotent
            with urllib.request.urlopen(info["url"], timeout=30) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.read().decode(), "0 notes: ")
                self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
            self.assertTrue(mgr.stop("scratch"))
            self.assertIsNone(mgr.info("scratch"))
            with self.assertRaises((urllib.error.URLError, OSError)):
                urllib.request.urlopen(info["url"], timeout=3)
            self.assertEqual(json.loads(json.dumps(mgr.report("scratch")))["requests"], 1)


if __name__ == "__main__":
    unittest.main()
