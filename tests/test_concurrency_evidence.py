"""Concurrency evidence: shared registry writes, parallel lanes, mounted apps under
simultaneous requests, and the model-call gate.

Every test starts real threads and joins them with a timeout, so a stuck lane or
request fails loudly instead of hanging. The model and the Lisp worker are
scripted (no network, no SBCL). Five cases found bugs when this file was written;
their comments say what was wrong.
"""
import contextlib
import http.client
import json
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import modelgate  # noqa: E402
import mount  # noqa: E402
import projects  # noqa: E402
import s_expr  # noqa: E402

JOIN_S = 20          # no single wait for a thread is longer than this
SIXTY_S = 40         # the whole file stays under this (see the timings in the report)


def join_all(threads, limit=JOIN_S):
    """Join THREADS within LIMIT seconds in total; a thread still alive fails the test."""
    end = time.time() + limit
    for t in threads:
        t.join(max(0.0, end - time.time()))
    stuck = [t.name for t in threads if t.is_alive()]
    if stuck:
        raise AssertionError("threads still running after %ss: %s" % (limit, ", ".join(stuck)))


def tool(name, body="(* 2 x)", **extra):
    """A saved-tool record as the registry stores it."""
    rec = {"name": name, "description": "d", "definition": "(defun %s (x) %s)" % (name, body),
           "tests": [], "call": "(%s 1)" % name}
    rec.update(extra)
    return rec


# ---------------------------------------------------------------------------
# A. Registry writes
# ---------------------------------------------------------------------------

class RegistryWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "tools.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _writers(self, targets, per_thread, make_name=lambda k, j: "t%d-%d" % (k, j)):
        """Start one writer thread per entry of TARGETS; returns (threads, errors)."""
        errors = []
        lock = threading.Lock()

        def writer(reg, k):
            try:
                for j in range(per_thread):
                    reg.add(tool(make_name(k, j)))
            except Exception as exc:  # noqa: BLE001 - recorded, asserted empty later
                with lock:
                    errors.append("%s: %s" % (type(exc).__name__, exc))
        threads = [threading.Thread(target=writer, args=(reg, k), name="writer-%d" % k)
                   for k, reg in enumerate(targets)]
        for t in threads:
            t.start()
        return threads, errors

    def test_1_eight_threads_adding_to_one_registry_lose_nothing(self):
        reg = ag.ToolRegistry(self.path)
        threads, errors = self._writers([reg] * 8, 25)
        join_all(threads)
        self.assertEqual(errors, [])
        names = [t["name"] for t in json.loads(self.path.read_text(encoding="utf-8"))]
        self.assertEqual(len(names), 200)
        self.assertEqual(len(set(names)), 200)
        self.assertEqual(sorted(t["name"] for t in reg.load()), sorted(names))

    def test_2_two_registry_objects_on_one_file_keep_every_add(self):
        # Was a bug, fixed: two ToolRegistry objects on one path lock only themselves and share one temp file name, so concurrent adds lose tools and fail with PermissionError on replace.
        regs = [ag.ToolRegistry(self.path), ag.ToolRegistry(self.path)]
        threads, errors = self._writers([regs[k % 2] for k in range(4)], 25)
        join_all(threads)
        names = sorted(t["name"] for t in ag.ToolRegistry(self.path).load())
        self.assertEqual(errors, [])
        self.assertEqual(len(names), 100, "lost %d of 100 adds" % (100 - len(names)))
        self.assertEqual(len(set(names)), len(names))

    def test_3_replacing_a_tool_while_others_load_never_tears_or_drops_it(self):
        # Was a bug, fixed: load() takes no lock and _raw() turns a failed read into an empty list, so readers see an empty registry while add() replaces the file, and the writers fail with PermissionError.
        reg = ag.ToolRegistry(self.path)
        reg.add(tool("shared"))
        written = {"(defun shared (x) (* %d x))" % j for j in range(2, 200)}
        written.add("(defun shared (x) (* 1 x))")
        stop = threading.Event()
        bad, errors = [], []

        def reader():
            while not stop.is_set():
                got = [t for t in reg.load() if t["name"] == "shared"]
                if len(got) != 1 or got[0]["definition"] not in written:
                    if len(bad) < 1000:
                        bad.append(len(got))

        def writer(k):
            try:
                for j in range(2 + 60 * k, 2 + 60 * k + 40):
                    reg.add(tool("shared", "(* %d x)" % j))
            except Exception as exc:  # noqa: BLE001
                errors.append("%s: %s" % (type(exc).__name__, exc))
        readers = [threading.Thread(target=reader, name="reader-%d" % k) for k in range(2)]
        writers = [threading.Thread(target=writer, args=(k,), name="writer-%d" % k) for k in range(3)]
        for t in readers + writers:
            t.start()
        join_all(writers)
        stop.set()
        join_all(readers)
        self.assertEqual(errors, [], "writers failed: %d" % len(errors))
        self.assertEqual(bad, [], "%d reads saw no single copy of the tool (got counts %s)"
                         % (len(bad), sorted(set(bad))))
        final = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([t["name"] for t in final].count("shared"), 1)

    def test_4_note_use_from_eight_threads_counts_every_use(self):
        reg = ag.ToolRegistry(self.path)
        reg.add(tool("counted"))

        def user():
            for _ in range(50):
                reg.note_use("counted", "", "")
        threads = [threading.Thread(target=user, name="user-%d" % k) for k in range(8)]
        for t in threads:
            t.start()
        join_all(threads)
        self.assertEqual(reg.load()[0]["uses"], 400)

    def test_5_retire_and_add_at_the_same_time_both_survive(self):
        reg = ag.ToolRegistry(self.path)
        reg.add(tool("old"))
        retired = []

        def adder(k):
            for j in range(25):
                reg.add(tool("n%d-%d" % (k, j)))

        def retirer():
            for _ in range(20):
                retired.extend(reg.retire({"old": "replaced by the n tools"}))
        threads = [threading.Thread(target=adder, args=(k,), name="adder-%d" % k) for k in range(4)]
        threads.append(threading.Thread(target=retirer, name="retirer"))
        for t in threads:
            t.start()
        join_all(threads)
        self.assertEqual(retired, ["old"], "retire must report the change exactly once")
        self.assertEqual(len(reg.load()), 100)
        self.assertNotIn("old", [t["name"] for t in reg.load()])
        gone = next(t for t in reg.load(retired=True) if t["name"] == "old")
        self.assertEqual(gone["retired"], "replaced by the n tools")
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))), 101)


# ---------------------------------------------------------------------------
# B. Lanes changing shared capabilities
# ---------------------------------------------------------------------------

def build(name, body, params="(x)"):
    call = "(%s 1)" % name
    return {"action": "build", "name": name, "description": "d",
            "definition": "(defun %s %s %s)" % (name, params, body),
            "tests": [{"call": call, "expect": "T"}], "call": call}


def ok_worker(bad_names=()):
    """A fake Lisp worker: every check passes except those that call a name in BAD_NAMES."""
    def run(code):
        hit = any(("(gg-check (%s " % n) in code for n in bad_names)
        return {"ok": True, "stdout": "", "error": "", "timed_out": False, "elapsed_ms": 1.0,
                "return_value": "(:GOT 9)" if hit else "T"}
    return run


class Script:
    """A thread-safe scripted model. It answers the planner, each build request and the final call."""

    def __init__(self, plan, bodies, delay=0.01, slow=None, barrier=None, usage=None):
        self.plan, self.bodies = plan, bodies
        self.delay, self.slow = delay, dict(slow or {})
        self.barrier, self.usage = barrier, usage
        self.lock = threading.Lock()
        self.prompts = []

    def __call__(self, system, user):
        with self.lock:
            self.prompts.append(user)
        name = next((n for n in self.bodies if "GOAL: (%s " % n in user), None)
        time.sleep(self.slow.get(name, self.delay))
        if name and ("BUILD exactly this one" in user or "PREVIOUS ATTEMPT FAILED" in user):
            if self.barrier is not None:
                self.barrier.wait(JOIN_S)
            obj = build(name, self.bodies[name])
        elif "All planned helper tools" in user:
            obj = {"action": "use", "call": "(%s 1)" % list(self.bodies)[-1]}
        elif "Split it into" in user:
            obj = {"action": "stop"}
        else:
            obj = self.plan
        out = ag._fake(obj)
        if self.usage is not None:
            out.update(self.usage(user))
        return out


def session(model, prompt, path, parallel=4, worker=None, log=None):
    """A session whose registry and event log both live beside PATH (never in artifacts/)."""
    sess = ag.Session(prompt, model, registry=ag.ToolRegistry(path),
                      worker_fn=worker or ok_worker(),
                      log_path=log or Path(path).parent / "session.jsonl")
    sess.parallel = parallel
    return sess


def run_session(sess, limit=JOIN_S):
    t = threading.Thread(target=sess.run, name="session", daemon=True)
    t.start()
    join_all([t], limit)
    return sess


def at(sess, kind, **match):
    """Position of the first event of KIND whose fields match MATCH."""
    return next(n for n, e in enumerate(sess.events) if e["kind"] == kind and
                all(e.get(k) == v for k, v in match.items()))


def failures(sess):
    return [e for e in sess.events if e["kind"] in ("gave_up", "error")]


SIX = [{"name": "f%d" % k, "spec": "(f%d x) -> %d times x" % (k, k)} for k in range(1, 7)]
SIX_BODIES = {"f%d" % k: "(* %d x)" % k for k in range(1, 7)}
FOUR = SIX[:4]
FOUR_BODIES = {n: b for n, b in SIX_BODIES.items() if n in {"f1", "f2", "f3", "f4"}}


class LaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_6_six_independent_steps_all_save_and_the_log_matches_the_events(self):
        log = self.dir / "l.jsonl"
        sess = session(Script({"action": "plan", "steps": SIX}, SIX_BODIES), "make all-three",
                       self.dir / "t.json", log=log)
        run_session(sess)
        self.assertEqual(sess.state, "done", failures(sess))
        self.assertEqual(sorted(t["name"] for t in sess.registry.load()), sorted(SIX_BODIES))
        raw = json.loads((self.dir / "t.json").read_text(encoding="utf-8"))   # valid JSON
        self.assertEqual(sorted(t["name"] for t in raw), sorted(SIX_BODIES))
        for n, e in enumerate(sess.events):
            if e["kind"] == "promoted":
                self.assertTrue(any(v["kind"] == "verdict" and v.get("lane") == e["lane"]
                                    and v.get("ok") for v in sess.events[:n]),
                                "promoted %s without an ok verdict in its lane" % e["lane"])
        lines = log.read_text(encoding="utf-8").splitlines()
        parsed = [json.loads(x) for x in lines]            # every line is one whole event
        self.assertEqual([e["i"] for e in parsed], [e["i"] for e in sess.events])
        self.assertEqual(parsed, json.loads(json.dumps(sess.events)))

    def test_7_the_same_step_name_twice_is_built_in_order_and_saved_once(self):
        steps = [{"name": "double", "spec": "(double x) -> twice x"},
                 {"name": "double", "spec": "(double x) -> twice x, written again"},
                 {"name": "triple", "spec": "(triple x) -> three times x"}]
        model = Script({"action": "plan", "steps": steps}, {"double": "(* 2 x)", "triple": "(* 3 x)"})
        sess = run_session(session(model, "make all-three", self.dir / "t.json",
                                   log=self.dir / "l.jsonl"))
        # What the code does: duplicate names disable the lanes, so the steps are built
        # one after the other, and the second build of "double" replaces the first.
        self.assertFalse(any(e["kind"] == "parallel" for e in sess.events))
        self.assertEqual(sess.state, "done", failures(sess))
        self.assertFalse(any(e["kind"] == "error" for e in sess.events))
        self.assertEqual(sum(1 for e in sess.events if e["kind"] == "step" and
                             e.get("name") == "double"), 2)
        names = [t["name"] for t in sess.registry.load()]
        self.assertEqual(sorted(names), ["double", "triple"])

    def test_8a_a_lane_waits_for_a_function_it_calls_even_when_its_spec_is_silent(self):
        steps = [{"name": "double", "spec": "(double x) -> twice x"},
                 {"name": "quad", "spec": "(quad x) -> four times x"}]
        model = Script({"action": "plan", "steps": steps},
                       {"double": "(* 2 x)", "quad": "(double (double x))"}, slow={"double": 0.4})
        sess = run_session(session(model, "make all-three", self.dir / "t.json"))
        self.assertEqual(sess.state, "done", failures(sess))
        wait = next(e for e in sess.events if e["kind"] == "step_wait")
        self.assertEqual((wait["name"], wait["on"]), ("quad", ["double"]))
        self.assertLess(at(sess, "promoted", name="double"), at(sess, "verdict", lane="quad"))
        self.assertEqual(sorted(t["name"] for t in sess.registry.load()), ["double", "quad"])

    def test_8b_a_caller_planned_before_its_callee_finishes_without_deadlock(self):
        steps = [{"name": "quad", "spec": "(quad x) -> (double (double x))"},
                 {"name": "double", "spec": "(double x) -> twice x"}]
        model = Script({"action": "plan", "steps": steps},
                       {"double": "(* 2 x)", "quad": "(double (double x))"})
        sess = session(model, "make all-three", self.dir / "t.json")
        started = time.time()
        run_session(sess, limit=10)                       # raises if the lanes deadlock
        self.assertLess(time.time() - started, 10)
        self.assertIn(sess.state, ("done", "failed"))
        self.assertFalse(any(e["kind"] == "error" for e in sess.events))
        # Lanes wait only for EARLIER steps (that is what rules out a cycle), so quad
        # may be tested before double is saved. Both lanes must still end.
        for name in ("quad", "double"):
            self.assertTrue(any(e["kind"] == "step_done" and e["name"] == name for e in sess.events))

    def _seed(self):
        reg = ag.ToolRegistry(self.dir / "t.json")
        reg.add({"name": "double", "description": "twice x", "definition": "(defun double (x) (* 2 x))",
                 "tests": [{"call": "(double 1)", "expect": "2"}], "call": "(double 1)"})
        reg.add({"name": "quad", "description": "four times x",
                 "definition": "(defun quad (x) (double (double x)))",
                 "tests": [{"call": "(quad 1)", "expect": "4"}], "call": "(quad 1)"})
        return reg

    def _replace_double(self, body):
        steps = [{"name": "triple", "spec": "(triple x) -> three times x"},
                 {"name": "double", "spec": "(double x) -> the new double"}]
        model = Script({"action": "plan", "steps": steps},
                       {"triple": "(* 3 x)", "double": body})

        def worker(code):
            # the replacement breaks quad (which needs twice twice x = 4) when it triples;
            # the harness writes the description into the stored definition as a docstring
            broken = re.search(r'\(defun double \(x\)\s+(?:"[^"]*"\s+)?\(\* 3 x\)', code) is not None \
                and "(gg-check (quad " in code
            return {"ok": True, "stdout": "", "error": "", "timed_out": False, "elapsed_ms": 1.0,
                    "return_value": "(:GOT 6)" if broken else "T"}
        self._seed()
        return run_session(session(model, "make all-three", self.dir / "t.json", worker=worker))

    def test_9a_a_replacement_that_breaks_a_caller_is_not_saved(self):
        sess = self._replace_double("(* 3 x)")
        regress = [e for e in sess.events if e["kind"] == "verdict" and e.get("class") == "REGRESSION"]
        self.assertTrue(regress and regress[0].get("lane") == "double", "no regression verdict")
        self.assertEqual(sess.state, "failed")
        tools = {t["name"]: t for t in sess.registry.load()}
        self.assertIn("(* 2 x)", tools["double"]["definition"])
        self.assertNotIn("(* 3 x)", tools["double"]["definition"])
        self.assertIn("(double (double x))", tools["quad"]["definition"])
        self.assertIn("triple", tools)

    def test_9b_a_replacement_that_keeps_the_caller_passing_is_saved(self):
        sess = self._replace_double("(+ x x)")
        self.assertEqual(sess.state, "done", failures(sess))
        tools = {t["name"]: t for t in sess.registry.load()}
        self.assertIn("(+ x x)", tools["double"]["definition"])
        self.assertIn("(double (double x))", tools["quad"]["definition"])
        self.assertFalse(any(e.get("class") == "REGRESSION" for e in sess.events))

    def test_10_costs_and_tokens_add_up_when_four_lanes_reply_at_once(self):
        def usage(user):
            m = re.search(r"GOAL: \(f(\d) ", user)
            if m and "BUILD exactly this one" in user:
                k = int(m.group(1))
                return {"cost_usd": 0.001 * k, "input_tokens": 100 * k + 7, "output_tokens": 10 * k + 3}
            if "All planned helper tools" in user:
                return {"cost_usd": 0.002, "input_tokens": 50, "output_tokens": 5}
            return {"cost_usd": 0.01, "input_tokens": 1000, "output_tokens": 300}
        bodies = {n: b for n, b in FOUR_BODIES.items()}
        model = Script({"action": "plan", "steps": FOUR}, bodies,
                       barrier=threading.Barrier(4), usage=usage)
        sess = run_session(session(model, "make all-three", self.dir / "t.json"))
        self.assertEqual(sess.state, "done", failures(sess))
        replies = [e for e in sess.events if e["kind"] == "model_reply"]
        calls = [e for e in sess.events if e["kind"] == "model_call"]
        self.assertEqual(sess.model_calls, len(calls))
        self.assertEqual(len(replies), 6)                  # plan + 4 lanes + final
        self.assertAlmostEqual(sess.cost_usd, sum(e["cost_usd"] or 0 for e in replies), places=9)
        self.assertEqual(sess.input_tokens, sum(e["input_tokens"] or 0 for e in replies))
        self.assertEqual(sess.output_tokens, sum(e["output_tokens"] or 0 for e in replies))
        self.assertAlmostEqual(sess.cost_usd, 0.022, places=9)
        self.assertEqual((sess.input_tokens, sess.output_tokens), (2078, 417))
        self.assertEqual(next(e for e in sess.events if e["kind"] == "summary")
                         ["concurrency"]["lanes"], 4)


# ---------------------------------------------------------------------------
# C. Mounted application under simultaneous requests
# ---------------------------------------------------------------------------

def rows_app_lisp(code):
    """Fake worker: the rows app's (handle-request request state), written in Python.

    The request plist and the state arrive as Lisp text inside the call that
    mount.handler_call builds; the answer is the printed list that mount.read_response parses.
    """
    m = re.search(r"\(handle-request '(\(:method .*?:nonce \"[^\"]*\"\)) '(.*?)\)\)\)\n  \(list",
                  code, re.S)
    if not m:
        return {"ok": False, "return_value": "", "stdout": "", "error": "fake: no call found",
                "timed_out": False, "elapsed_ms": 0.0}
    fields = dict(zip(*[iter(s_expr.parse(m.group(1)))] * 2))
    state = m.group(2).strip()
    rows = [] if state.upper() == "NIL" else [str(x) for x in s_expr.parse(state)[0][1]]
    if fields[":method"] == "POST" and fields[":path"] == "/add":
        form = dict((str(k), str(v)) for k, v in fields[":form"])
        rows = rows + [form["text"]]
        state_lisp = '(("rows" (%s)))' % " ".join(mount.lisp_string(r) for r in rows)
        printed = '(303 (("Location" "/") ("X-Seq" "%d")) "" 1 %s)' % (
            len(rows), mount.lisp_string(state_lisp))
    else:
        printed = '(200 (("Content-Type" "text/plain")) %s 0 "NIL")' % mount.lisp_string(
            "seen=%d" % len(rows))
    return {"ok": True, "return_value": printed, "stdout": "", "error": "",
            "timed_out": False, "elapsed_ms": 0.5}


class Httpd(ThreadingHTTPServer):
    request_queue_size = 64          # 40 simultaneous connects must not overflow the backlog


class Notes:
    """A rows app served over real HTTP by mount.make_handler, with the Lisp call faked."""

    def __init__(self, directory):
        reg = ag.ToolRegistry(Path(directory) / "tools.json")
        reg.add({"name": "handle-request", "description": "rows app",
                 "definition": "(defun handle-request (request state) nil)"})
        self.log = Path(directory) / "requests.jsonl"
        self.app = mount.MountedApp(reg, mount.StateStore(Path(directory) / "state.sqlite"),
                                    run_lisp=rows_app_lisp, log_path=self.log)
        self.httpd = Httpd(("127.0.0.1", 0), mount.make_handler(self.app))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="httpd", daemon=True)
        self.thread.start()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=JOIN_S)
        try:
            headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read().decode("utf-8")
        finally:
            conn.close()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class MountedAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Notes(self.tmp.name)

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_11_forty_simultaneous_posts_leave_forty_rows_and_a_readable_database(self):
        results, lock = [], threading.Lock()

        def post(k):
            status, _, _ = self.app.request("POST", "/add", ("text=row-%d" % k).encode())
            with lock:
                results.append(status)
        threads = [threading.Thread(target=post, args=(k,), name="post-%d" % k) for k in range(40)]
        for t in threads:
            t.start()
        join_all(threads)
        self.assertEqual(len(results), 40)
        self.assertTrue(all(s in (200, 303) for s in results), sorted(set(results)))
        db = sqlite3.connect(self.tmp.name + "/state.sqlite")
        try:
            self.assertEqual(db.execute("pragma integrity_check").fetchone()[0], "ok")
            value = db.execute("select value from app_state where id = 1").fetchone()[0]
        finally:
            db.close()
        rows = [str(x) for x in s_expr.parse(value)[0][1]]
        self.assertEqual(len(rows), 40, "lost %d of 40 updates" % (40 - len(rows)))
        self.assertEqual(sorted(rows), sorted("row-%d" % k for k in range(40)))
        status, _, body = self.app.request("GET", "/")
        self.assertEqual((status, body), (200, "seen=40"))

    def test_12_no_get_is_answered_from_a_state_older_than_an_earlier_answered_post(self):
        answered, problems, seqs = [0], [], []
        lock = threading.Lock()

        def poster(k):
            for j in range(20):
                status, headers, _ = self.app.request("POST", "/add", ("text=p%d-%d" % (k, j)).encode())
                with lock:
                    if status != 303:
                        problems.append(("post status", status))
                    seq = int(headers.get("X-Seq", "0"))
                    seqs.append(seq)
                    answered[0] = max(answered[0], seq)

        def reader(k):
            for _ in range(40):
                with lock:
                    floor = answered[0]              # every POST answered so far
                status, _, body = self.app.request("GET", "/")
                seen = int(re.search(r"seen=(\d+)", body).group(1))
                if seen < floor:
                    with lock:
                        problems.append(("stale GET", floor, seen))
        threads = [threading.Thread(target=poster, args=(k,), name="poster-%d" % k) for k in range(3)]
        threads += [threading.Thread(target=reader, args=(k,), name="reader-%d" % k) for k in range(3)]
        for t in threads:
            t.start()
        join_all(threads)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(seqs), list(range(1, 61)))
        # Once nothing changes, repeated GETs are answered from the response cache.
        for _ in range(5):
            self.assertEqual(self.app.request("GET", "/")[2], "seen=60")
        tail = self.app.log.read_text(encoding="utf-8").splitlines()[-5:]   # written one at a time
        self.assertGreaterEqual(sum(1 for x in tail if json.loads(x).get("cache")), 4)

    def test_16_request_log_keeps_one_whole_line_per_request_under_concurrency(self):
        # Was a bug, fixed: MountedApp._write_log appends to the request log outside self._lock, so concurrent requests interleave and lose lines, and report() silently skips the torn ones. The fix belongs in mount.MountedApp._write_log: take a lock around the append (or call it under self._lock).
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "requests.jsonl"
            app = mount.MountedApp(None, None, log_path=path)

            def writer(k):
                for j in range(25):
                    app._write_log({"t": 1.0, "method": "POST", "path": "/add", "k": k, "j": j,
                                    "lisp": "x" * 300, "status": 303, "ms": 1.2})
            threads = [threading.Thread(target=writer, args=(k,), name="log-%d" % k) for k in range(40)]
            for t in threads:
                t.start()
            join_all(threads)
            lines = path.read_text(encoding="utf-8").splitlines()
            whole = 0
            for x in lines:
                try:
                    json.loads(x)
                    whole += 1
                except ValueError:
                    pass
            self.assertEqual(len(lines), 1000, "%d lines lost" % (1000 - len(lines)))
            self.assertEqual(whole, 1000, "%d lines torn" % (len(lines) - whole))

    def test_13_four_starts_of_one_project_at_once_give_one_server_and_one_port(self):
        # Was a bug, fixed: MountManager.start checks for an existing mount and stores the new server outside any lock, so concurrent starts each open a server and the extra ones keep listening after stop().
        with tempfile.TemporaryDirectory() as tmp:
            store = projects.ProjectStore(tmp)
            proj, err = store.create("Race app")
            self.assertIsNone(err)
            pid = proj["id"]
            opened = []

            class Tracked(ThreadingHTTPServer):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    opened.append(self)
            mgr = mount.MountManager(tmp, lambda p: ag.ToolRegistry(store.tools_path(p)))
            barrier = threading.Barrier(4)
            out = {}

            def start(k):
                barrier.wait(JOIN_S)
                out[k] = mgr.start(pid)
            threads = [threading.Thread(target=start, args=(k,), name="start-%d" % k) for k in range(4)]
            try:
                with mock.patch.object(mount, "ThreadingHTTPServer", Tracked):
                    for t in threads:
                        t.start()
                    join_all(threads)
                errors = [err for _, err in out.values() if err]
                self.assertEqual(errors, [])
                self.assertEqual(len(opened), 1, "%d servers were opened for one project, on ports %s"
                                 % (len(opened), sorted(s.server_address[1] for s in opened)))
                ports = {info["port"] for info, _ in out.values()}
                self.assertEqual(len(ports), 1, "four starts returned ports %s" % sorted(ports))
                port = ports.pop()
                self.assertEqual(mgr.info(pid)["port"], port)
                self.assertTrue(mgr.stop(pid))
                self.assertIsNone(mgr.info(pid))
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.bind(("127.0.0.1", port))        # the port is free again
            finally:
                for srv in opened:
                    stopper = threading.Thread(target=srv.shutdown, daemon=True)
                    stopper.start()
                    stopper.join(5)
                    srv.server_close()


# ---------------------------------------------------------------------------
# D. The model-call gate
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class GateTests(unittest.TestCase):
    def test_14_the_gate_caps_slots_pauses_for_a_429_and_regrows_after_quiet(self):
        # a) twelve callers, three slots: never more than three inside at once
        gate = modelgate.ModelGate(start=3, clock=FakeClock())
        inside, most, lock = [0], [0], threading.Lock()

        def body():
            with gate.slot():
                with lock:
                    inside[0] += 1
                    most[0] = max(most[0], inside[0])
                time.sleep(0.03)                       # long enough that calls overlap
                with lock:
                    inside[0] -= 1
        threads = [threading.Thread(target=body, name="caller-%d" % k) for k in range(12)]
        for t in threads:
            t.start()
        join_all(threads)
        self.assertLessEqual(most[0], 3)
        self.assertLessEqual(gate.peak, 3)
        self.assertGreaterEqual(most[0], 2)

        # b) a 429 while nine callers wait: nobody enters during the pause, then one at a time
        clock = FakeClock()
        gate = modelgate.ModelGate(start=3, clock=clock)
        stack = contextlib.ExitStack()
        for _ in range(3):
            stack.enter_context(gate.slot())            # the three slots are busy
        entered, inside, most = [], [0], [0]

        def waiter(k):
            with gate.slot():
                with lock:
                    entered.append((k, clock.now))
                    inside[0] += 1
                    most[0] = max(most[0], inside[0])
                time.sleep(0.01)
                with lock:
                    inside[0] -= 1
        waiters = [threading.Thread(target=waiter, args=(k,), name="waiter-%d" % k) for k in range(9)]
        for t in waiters:
            t.start()
        time.sleep(0.05)
        self.assertEqual(gate.throttled(0.2), 1)        # 3 // 2: halved, paused until clock + 0.2
        stack.close()                                   # the busy slots are released
        time.sleep(0.15)                                # real time passes; the fake clock does not
        self.assertEqual(entered, [], "a call entered during the pause")
        self.assertEqual(gate.limit, 1)
        self.assertGreater(gate.snapshot()["paused_s"], 0.0)
        for _ in range(8):                              # successes inside the quiet period
            gate.succeeded()
        self.assertEqual(gate.limit, 1, "limit grew inside the quiet period")
        clock.now += 0.25                               # the pause ends
        join_all(waiters)
        self.assertEqual(len(entered), 9)
        self.assertTrue(all(t >= 1000.0 + 0.2 for _, t in entered))
        self.assertEqual(most[0], 1)                    # limit 1: one call at a time

        # c) after the quiet period, eight successes raise the limit by one, then again
        clock.now += 30
        for _ in range(8):
            gate.succeeded()
        self.assertEqual(gate.limit, 2)
        for _ in range(8):
            gate.succeeded()
        self.assertEqual(gate.limit, 3)


# ---------------------------------------------------------------------------
# E. Extra: project registry writes (found while reading projects.py)
# ---------------------------------------------------------------------------

class ProjectWriteTests(unittest.TestCase):
    def test_15_touch_cannot_undo_a_rename_made_while_it_was_running(self):
        # Was a bug, fixed: ProjectStore.touch writes back the project it read without holding the store lock, so a rename made during the touch is overwritten with the old name.
        with tempfile.TemporaryDirectory() as tmp:
            store = projects.ProjectStore(tmp)
            proj, _ = store.create("Old name")
            pid = proj["id"]
            real_get = store.get
            reading, go = threading.Event(), threading.Event()

            def get(project_id):
                meta = real_get(project_id)
                if threading.current_thread().name == "toucher":
                    reading.set()
                    go.wait(JOIN_S)                    # touch has read; hold it there
                return meta
            store.get = get
            toucher = threading.Thread(target=store.touch, args=(pid,), name="toucher")
            toucher.start()
            self.assertTrue(reading.wait(JOIN_S))
            renamer = threading.Thread(target=store.update, args=(pid,), kwargs={"name": "New name"},
                                       name="renamer")
            renamer.start()
            time.sleep(0.2)
            go.set()
            join_all([toucher, renamer])
            self.assertEqual(store.get(pid)["name"], "New name")


if __name__ == "__main__":
    unittest.main()
