"""The screenshot check: a finished web app is walked like a visitor would, its pages
are rendered to pictures, the model says what it still sees wrong, and one round of
changes is built and looked at again."""
import http.client
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

import agent_session as ag  # noqa: E402
import visualcheck  # noqa: E402

PNG = bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x02rest"
LOGIN = ('<html><body><form method="POST" action="/login"><input type="hidden" name="nonce" value="n1">'
         '<input type="text" name="username"><input type="password" name="password">'
         '<button type="submit">Sign in</button></form></body></html>')
HOME = ('<html><body><h1>Products</h1><a href="/edit?name=Widget">Edit</a> <a href="/logout">Log out</a>'
        ' <a href="/delete?name=Widget">Delete</a> <a href="https://example.com/x">Out</a>'
        ' <a href="/about">About</a></body></html>')
STATE = '(("products" (("Widget" "10.00" "d"))) ("users" (("user" "password"))) ("sessions" ()))'


class FakeApp:
    """A login-protected site: / needs the sid cookie that POST /login hands out."""

    def __init__(self, state=STATE):
        self.state, self.seen = state, []

    def _state(self):
        return self.state

    def handle(self, method, target, headers, body=b""):
        self.seen.append((method, target, headers.get("Cookie", ""), body.decode()))
        signed = "sid=abc" in headers.get("Cookie", "")
        path = target.split("?")[0]
        if method == "POST" and path == "/login":
            if "username=user" in body.decode() and "password=password" in body.decode() \
                    and "nonce=n1" in body.decode():
                return 303, [("Location", "/"), ("Set-Cookie", "sid=abc; HttpOnly; Path=/")], ""
            return 200, [], LOGIN
        if path == "/login":
            return 200, [], LOGIN
        if not signed:
            return 303, [("Location", "/login")], ""
        if path == "/":
            return 200, [], HOME
        if path in ("/edit", "/about"):
            return 200, [], "<html><body><h1>%s</h1></body></html>" % path
        return 404, [], "<html><body>Not found</body></html>"


class PageWalkTests(unittest.TestCase):
    def test_the_front_page_then_the_page_behind_the_login_then_its_links(self):
        app = FakeApp()
        pages = visualcheck.collect_pages(app, limit=4)
        self.assertEqual([p["label"] for p in pages], [
            "GET / -> /login", "signed in as user: GET /", "signed in: GET /edit?name=Widget",
            "signed in: GET /about"])
        self.assertIn("<form", pages[0]["html"])
        self.assertIn("Products", pages[1]["html"])
        visited = [t for _, t, _, _ in app.seen]
        self.assertFalse(any("logout" in t or "delete" in t or "example.com" in t for t in visited))
        self.assertIn(("POST", "/login", "", "nonce=n1&username=user&password=password"), app.seen)

    def test_the_limit_is_kept_and_a_page_is_shown_once(self):
        self.assertEqual(len(visualcheck.collect_pages(FakeApp(), limit=2)), 2)
        self.assertEqual(len(visualcheck.collect_pages(FakeApp(), limit=1)), 1)

    def test_without_a_user_in_the_state_only_the_public_pages_are_shown(self):
        pages = visualcheck.collect_pages(FakeApp('(("users" ()))'), limit=3)
        self.assertEqual([p["label"] for p in pages], ["GET / -> /login"])

    def test_a_sign_in_that_does_not_work_is_not_reported_as_one(self):
        pages = visualcheck.collect_pages(FakeApp('(("users" (("user" "wrong"))))'), limit=3)
        self.assertEqual([p["label"] for p in pages], ["GET / -> /login"])

    def test_small_helpers(self):
        self.assertEqual(visualcheck.first_user(STATE), ("user", "password"))
        self.assertIsNone(visualcheck.first_user("nil"))
        self.assertIsNone(visualcheck.first_user("((broken"))
        self.assertEqual(visualcheck.login_form(LOGIN)["action"], "/login")
        self.assertIsNone(visualcheck.login_form(HOME))
        self.assertEqual(visualcheck.scan("<a href='/x'>x</a><p>unclosed")[1], ["/x"])

    def test_an_error_page_is_labelled_with_its_status(self):
        app = mock.Mock()
        app._state.return_value = "nil"
        app.handle.return_value = (500, [], "<html><body>boom</body></html>")
        self.assertEqual(visualcheck.collect_pages(app)[0]["label"], "GET / (status 500)")


def worker(code):
    if "prin1-to-string (getf resp :state)" in code:
        return {"ok": True, "elapsed_ms": 1.0, "return_value": '(200 NIL "<h1>ok</h1>" 0 "NIL")'}
    return {"ok": True, "stdout": "", "error": "", "timed_out": False, "elapsed_ms": 1.0,
            "return_value": "T"}


def build(name, body):
    call = "(%s '(:method \"GET\" :path \"/\") '((\"products\" ())))" % name
    return {"action": "build", "name": name, "description": "d",
            "definition": "(defun %s (request state) %s)" % (name, body),
            "tests": [{"call": call, "expect": "T"}], "call": call}


class ReviewLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.shown, self.prompts = [], []
        self.pages = [{"label": "GET / -> /login", "path": "/login", "status": 200, "html": LOGIN},
                      {"label": "signed in as user: GET /", "path": "/", "status": 200, "html": HOME}]
        patcher = mock.patch.object(visualcheck, "collect_pages", lambda app, limit=3: list(self.pages))
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, replies, capture_ok=True):
        it = iter(replies)

        def gen(system, user):
            self.prompts.append((system, user))
            self.shown.append(list(getattr(ag._TEMP, "images", None) or []))
            return ag._fake(next(it))

        def capture(html, out):
            if not capture_ok:
                return {"ok": False, "path": "", "error": "the browser did not finish within 30 s", "ms": 30000.0}
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(PNG)
            return {"ok": True, "path": str(out), "error": "", "ms": 900.0}
        reg = ag.ToolRegistry(Path(self.tmp.name) / "t.json")
        reg.add({"name": "handle-request", "description": "router",
                 "definition": "(defun handle-request (request state) (html-page 200 \"x\"))"})
        sess = ag.Session("simple crm website with tailwind styling", gen, registry=reg,
                          worker_fn=worker, log_path=Path(self.tmp.name) / "l.jsonl")
        sess.visual, sess.capture_fn = True, capture
        sess.shots_dir = Path(self.tmp.name) / "shots" / sess.id
        sess.max_calls = 12
        sess._app = True
        return sess

    def kinds(self, sess):
        return [(e["kind"], e.get("round")) for e in sess.events
                if e["kind"].startswith("visual") or e["kind"] in ("screenshot", "smoke")]

    def test_pages_that_look_right_end_the_job_after_one_look(self):
        sess = self.session([{"done": True, "problems": [], "fix": ""}])
        sess._visual_review()
        self.assertEqual(self.kinds(sess), [("visual_start", 1), ("screenshot", 1), ("screenshot", 1),
                                            ("visual_review", 1)])
        self.assertEqual(len(self.shown[0]), 2)                           # both pictures went with the call
        self.assertTrue(all(Path(p).read_bytes() == PNG for p in self.shown[0]))
        system, user = self.prompts[0]
        self.assertEqual(system, ag.VISUAL_SYSTEM)
        self.assertIn("GOAL: simple crm website with tailwind styling", user)
        self.assertIn("1. GET / -> /login\n2. signed in as user: GET /", user)
        call = next(e for e in sess.events if e["kind"] == "model_call")
        self.assertEqual((call["label"], call["images"]), ("visual-review", 2))
        shot = next(e for e in sess.events if e["kind"] == "screenshot")
        self.assertEqual(shot["url"], "/api/agent/shots/%s/r1-1.png" % sess.id)
        visual = sess._summary()["visual"]
        self.assertEqual((visual["checked"], visual["done"], visual["rounds"], len(visual["shots"])),
                         (True, True, 1, 2))
        self.assertEqual(sess.model_calls, 1)
        self.assertIsNone(getattr(ag._TEMP, "images", None))              # never leaks into the next call

    def test_what_the_pictures_show_wrong_is_built_and_looked_at_again(self):
        sess = self.session([
            {"done": False, "problems": ["the product page has no styling"], "fix": "use the css"},
            {"action": "plan", "steps": [{"name": "render-home", "spec": "(render-home request state) styled"}]},
            build("render-home", "(html-page 200 \"styled\")"),
            {"done": True, "problems": [], "fix": ""}])
        sess._visual_review()
        self.assertEqual(self.kinds(sess), [
            ("visual_start", 1), ("screenshot", 1), ("screenshot", 1), ("visual_review", 1),
            ("visual_fix", 1), ("smoke", None),
            ("visual_start", 2), ("screenshot", 2), ("screenshot", 2), ("visual_review", 2)])
        labels = [e["label"] for e in sess.events if e["kind"] == "model_call"]
        self.assertEqual(labels, ["visual-review", "visual-fix", "step", "visual-review"])
        fix_prompt = self.prompts[1][1]
        self.assertIn("SHOW THESE PROBLEMS: the product page has no styling.", fix_prompt)
        self.assertIn("Suggested fix: use the css.", fix_prompt)
        self.assertEqual(self.shown[1], [])                               # only the looks carry pictures
        self.assertIn("render-home", [t["name"] for t in sess.registry.load()])
        visual = sess._summary()["visual"]
        self.assertEqual((visual["done"], visual["rounds"], visual["problems"]), (True, 2, []))
        self.assertEqual([s["round"] for s in visual["shots"]], [1, 1, 2, 2])

    def test_the_second_look_is_the_last_and_its_verdict_is_reported_as_it_is(self):
        bad = {"done": False, "problems": ["still plain"], "fix": ""}
        sess = self.session([bad, {"action": "plan", "steps": [
            {"name": "render-home", "spec": "(render-home request state) styled"}]},
            build("render-home", "(html-page 200 \"styled\")"), bad])
        sess._visual_review()
        self.assertEqual([e["kind"] for e in sess.events].count("visual_fix"), 1)
        visual = sess._summary()["visual"]
        self.assertEqual((visual["done"], visual["rounds"], visual["problems"]), (False, 2, ["still plain"]))
        self.assertEqual(sess.model_calls, 4)

    def test_done_with_problems_listed_counts_as_not_done(self):
        sess = self.session([{"done": True, "problems": ["the table is empty"], "fix": ""},
                             {"action": "stop"}])
        sess._visual_review()
        self.assertEqual(sess._visual["done"], False)

    def test_it_is_skipped_with_a_reason_when_no_picture_could_be_taken(self):
        sess = self.session([], capture_ok=False)
        sess._visual_review()
        review = next(e for e in sess.events if e["kind"] == "visual_review")
        self.assertEqual(review["skipped"], "no page could be rendered")
        self.assertEqual(sess._summary()["visual"]["skipped"], "no page could be rendered")
        self.assertEqual(sess.model_calls, 0)
        failed = [e for e in sess.events if e["kind"] == "screenshot"]
        self.assertTrue(all(not e["ok"] and e["url"] is None and "did not finish" in e["error"] for e in failed))

    def test_it_does_nothing_when_switched_off_or_when_there_is_no_web_app(self):
        sess = self.session([])
        sess.visual = False
        sess._visual_review()
        self.assertEqual(self.kinds(sess), [])
        other = ag.Session("x", lambda s, u: ag._fake({"action": "stop"}),
                           registry=ag.ToolRegistry(Path(self.tmp.name) / "empty.json"),
                           log_path=Path(self.tmp.name) / "l2.jsonl")
        other.visual = True
        other._visual_review()
        self.assertEqual([e for e in other.events if e["kind"].startswith("visual")], [])

    def test_only_the_live_model_looks_by_default_and_the_switch_reaches_the_session(self):
        reg = ag.ToolRegistry(Path(self.tmp.name) / "d.json")
        self.assertFalse(ag.Session("x", lambda s, u: None, registry=reg,
                                    log_path=Path(self.tmp.name) / "a.jsonl").visual)
        self.assertTrue(ag.Session("x", ag.live_generate, registry=reg,
                                   log_path=Path(self.tmp.name) / "b.jsonl").visual)
        import inspect
        self.assertIn("visual=True", str(inspect.signature(ag.SessionManager.start)))
        self.assertIn("twin.visual = False", inspect.getsource(ag.SessionManager._chain))


class ModelCallTests(unittest.TestCase):
    def test_pictures_are_sent_as_base64_data_uris_in_the_user_message(self):
        import cerebras_client
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "a.png"
            png.write_bytes(PNG)
            done = mock.Mock(choices=[mock.Mock(finish_reason="stop", message=mock.Mock(
                content="{}", reasoning=None))], usage=mock.Mock(
                    prompt_tokens=900, completion_tokens=5, image_tokens=780), model="m", id="r")
            client = mock.Mock()
            client.chat.completions.create.return_value = done
            with mock.patch.object(cerebras_client, "get_client", return_value=client):
                res = cerebras_client.generate("look", system="sys", reasoning_effort="none",
                                               images=[str(png), bytes([255, 216, 255, 224]) + b"jpegdata"])
        messages = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": "sys"})
        parts = messages[1]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "look"})
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,iVBORw0KGgo"))
        self.assertTrue(parts[2]["image_url"]["url"].startswith("data:image/jpeg;base64,/9j/"))
        self.assertEqual(res["image_tokens"], 780)

    def test_a_call_without_pictures_is_sent_exactly_as_before(self):
        import cerebras_client
        done = mock.Mock(choices=[mock.Mock(finish_reason="stop", message=mock.Mock(
            content="{}", reasoning=None))], usage=None, model="m", id="r")
        client = mock.Mock()
        client.chat.completions.create.return_value = done
        with mock.patch.object(cerebras_client, "get_client", return_value=client):
            cerebras_client.generate("plain")
        self.assertEqual(client.chat.completions.create.call_args.kwargs["messages"],
                         [{"role": "user", "content": "plain"}])
        with mock.patch.object(ag, "live_status", return_value={"available": True, "reason": ""}), \
                mock.patch("cerebras_client.generate", return_value={"text": "{}"}) as gen:
            ag.live_generate("s", "u")
            self.assertNotIn("images", gen.call_args.kwargs)
            ag._TEMP.images = ["a.png"]
            try:
                ag.live_generate("s", "u")
            finally:
                ag._TEMP.images = None
            self.assertEqual(gen.call_args.kwargs["images"], ["a.png"])


class ShotRouteTests(unittest.TestCase):
    def test_a_screenshot_is_served_and_nothing_else_under_that_path(self):
        import server as dash
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "shots" / "abc123def0"
            folder.mkdir(parents=True)
            (folder / "r1-1.png").write_bytes(PNG)
            (Path(tmp) / "secret.txt").write_text("no", encoding="utf-8")
            with mock.patch.object(ag, "AGENT_DIR", Path(tmp)):
                srv = dash.ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
                thread = threading.Thread(target=srv.serve_forever, daemon=True)
                thread.start()
                try:
                    def get(path):
                        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
                        conn.request("GET", path)
                        resp = conn.getresponse()
                        data = resp.read()
                        conn.close()
                        return resp.status, resp.getheader("Content-Type"), data
                    self.assertEqual(get("/api/agent/shots/abc123def0/r1-1.png"), (200, "image/png", PNG))
                    for bad in ("/api/agent/shots/abc123def0/r1-2.png",
                                "/api/agent/shots/abc123def0/..%2F..%2Fsecret.txt",
                                "/api/agent/shots/../secret.txt",
                                "/api/agent/shots/abc123def0/secret.txt",
                                "/api/agent/shots/ABC/r1-1.png"):
                        self.assertEqual(get(bad)[0], 404, bad)
                finally:
                    srv.shutdown()
                    srv.server_close()


if __name__ == "__main__":
    unittest.main()
