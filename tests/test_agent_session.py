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

    def test_strips_nested_quotes_inside_quoted_data(self):
        q = ag.quote_literals
        self.assertEqual(q("(f '((0 0 0 1 '(1 1 1))))"), "(f '((0 0 0 1 (1 1 1))))")
        self.assertEqual(q("(f '(1 '(2 3)) 'x)"), "(f '(1 (2 3)) 'x)")
        self.assertEqual(q("(f '(1 2) '(3 4))"), "(f '(1 2) '(3 4))")   # separate quotes stay

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


class PlannerTests(unittest.TestCase):
    def test_tolerant_check_accepts_floats_for_rationals(self):
        """0.6 must satisfy an expected 3/5 (the live failure that motivated it)."""
        plan = {"name": "norm", "description": "d",
                "definition": "(defun norm (v) (mapcar (lambda (x) (/ x 5.0)) v))",
                "tests": [{"call": "(norm '(3 4 0))", "expect": "(3/5 4/5 0)"}],
                "call": "(norm '(3 4 0))"}
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", lambda s, u: None,
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              log_path=Path(tmp) / "l.jsonl")
            self.assertTrue(sess._rehearse(plan, "")["ok"])

    def test_wrong_value_still_fails_with_actual_vs_expected(self):
        plan = {"name": "norm", "description": "d",
                "definition": "(defun norm (v) v)",
                "tests": [{"call": "(norm '(1 2))", "expect": "(1 3)"}],
                "call": "(norm '(1 2))"}
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", lambda s, u: None,
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              log_path=Path(tmp) / "l.jsonl")
            out = sess._rehearse(plan, "")
            self.assertFalse(out["ok"])
            self.assertIn("got (1 2), expected (1 3)", out["detail"])

    def test_demo_ray_tracer_plans_builds_helpers_and_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            sess = ag.Session("write a ray tracer", ag.demo_generate,
                              registry=reg, log_path=Path(tmp) / "l.jsonl")
            sess.run()
            self.assertEqual(sess.state, "done", [e for e in sess.events if e["kind"] in ("error", "gave_up")])
            kinds = _kinds(sess)
            self.assertIn("plan", kinds)
            self.assertEqual(kinds.count("step"), 4)
            self.assertEqual([t["name"] for t in reg.load()],
                             ["vec-dot", "vec-sub", "ray-hits-sphere-p", "render-sphere-ascii"])
            art = [e for e in sess.events if e["kind"] == "result"][-1]["value"]
            self.assertEqual(art.count("#"), ag._sphere_hits(9))
            self.assertEqual(len(art.splitlines()), 9)

    def test_malformed_plan_is_rejected_not_executed(self):
        gen = lambda s, u: ag._fake({"action": "plan", "steps": "nope"})  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("big thing", gen,
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              log_path=Path(tmp) / "l.jsonl")
            sess.run()
            self.assertEqual(sess.state, "failed")
            self.assertIn("gave_up", _kinds(sess))


class JsonRetryTests(unittest.TestCase):
    def test_truncated_json_is_retried_once_not_fatal(self):
        replies = iter([{"__raw": '{"action":"use","call":"(sq 2'},     # cut off
                        {"action": "use", "call": "(sq 2)"}])

        def gen(system, user):
            r = next(replies)
            return {"text": r["__raw"], "input_tokens": 1, "output_tokens": 1,
                    "cost_usd": 0.0} if "__raw" in r else ag._fake(r)
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            reg.add({"name": "sq", "description": "square", "definition": "(defun sq (x) (* x x))"})
            sess = ag.Session("anything", gen, registry=reg,
                              log_path=Path(tmp) / "l.jsonl")
            sess.run()
            self.assertEqual(sess.state, "done", sess.events)
            self.assertIn("json_retry", _kinds(sess))
            self.assertEqual(sess.model_calls, 2)


class GuessedExpectationTests(unittest.TestCase):
    def test_setf_hint(self):
        self.assertIn("flat place/value pairs",
                      ag.lisp_hint("odd number of args to SETF: (SETF H 1 (H 2))"))

    def test_stuck_expectation_is_called_out_and_property_test_passes(self):
        prompts = []

        def build(expect, call):
            return {"action": "build", "name": "h", "description": "hash",
                    "definition": "(defun h (s) (mod (* 31 (length s)) 1000))",
                    "call": '(h "abc")',
                    "tests": [{"call": call, "expect": expect}]}
        replies = iter([
            build("123456", '(h "abc")'),                   # guessed exact value
            build("123456", '(h "abc")'),                   # same code, same guess
            build("T", '(let ((x (h "abc"))) (and (integerp x) (= x (h "abc"))))'),
        ])

        def gen(system, user):
            prompts.append(user)
            return ag._fake(next(replies))
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            sess = ag.Session("make a hash", gen, registry=reg,
                              log_path=Path(tmp) / "l.jsonl")
            sess.run()
            self.assertEqual(sess.state, "done",
                             [e for e in sess.events if e["kind"] in ("error", "gave_up")])
            self.assertTrue(any("probably guesses" in p for p in prompts))
            self.assertEqual([t["name"] for t in reg.load()], ["h"])


class SetfAndRescueTests(unittest.TestCase):
    def test_fix_setf_flattens_the_wrapped_pair_slip(self):
        bad = "(setf hash (logxor hash c) (hash (mod (* hash 3) 100)))"
        self.assertEqual(ag.fix_setf(bad),
                         "(setf hash (logxor hash c) hash (mod (* hash 3) 100))")

    def test_fix_setf_leaves_valid_forms_alone(self):
        for ok in ("(setf a 1 b 2)", "(setf a (list 1 2))", "(setf (car x) 5)",
                   "(defun f () (setf a 1) (setf b 2) a)"):
            self.assertEqual(ag.fix_setf(ok), ok, ok)

    def test_plan_with_the_setf_slip_now_builds_and_runs(self):
        plan = {"name": "mix", "description": "d",
                "definition": ("(defun mix (c) (let ((h 1)) (dolist (x c) "
                               "(setf h (+ h x) (h (* h 2)))) h))"),
                "tests": [{"call": "(mix '(1 2))", "expect": "12"}],
                "call": "(mix '(1 2))"}
        ag.normalize_plan(plan)
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", lambda s, u: None,
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              log_path=Path(tmp) / "l.jsonl")
            self.assertTrue(sess._rehearse(plan, "")["ok"])

    def test_stuck_guessed_values_are_rescued_with_property_tests(self):
        definition = "(defun h (s) (mod (* 31 (length s)) 1000))"

        def build(expect, call):
            return {"action": "build", "name": "h", "description": "hash",
                    "definition": definition, "call": '(h "abc")',
                    "tests": [{"call": call, "expect": expect}]}
        guessed = build("123456", '(h "abc")')
        replies = iter([guessed, guessed,
                        build("T", '(= (h "abc") (h "abc"))')])
        gen = lambda s, u: ag._fake(next(replies))  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            sess = ag.Session("make a hash", gen, registry=reg,
                              log_path=Path(tmp) / "l.jsonl")
            sess.run()
            self.assertEqual(sess.state, "done",
                             [e for e in sess.events if e["kind"] in ("error", "gave_up")])
            self.assertIn("rescue", _kinds(sess))
            self.assertEqual([t["name"] for t in reg.load()], ["h"])


FNV_OK = ("(defun fnv1a-hash (s) (let ((h 2166136261)) (loop for c across s do "
          "(setf h (mod (* (logxor h (char-code c)) 16777619) 4294967296))) h))")


def _session(tmp, **kw):
    return ag.Session(kw.pop("prompt", "x"), kw.pop("gen", lambda s, u: None),
                      registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                      log_path=Path(tmp) / "l.jsonl", **kw)


class OracleLoopTests(unittest.TestCase):
    def _fnv_plan(self, definition=FNV_OK, tests=None):
        return {"name": "fnv1a-hash", "description": "FNV-1a hash of a string",
                "definition": definition, "call": '(fnv1a-hash "abc")',
                "tests": tests or [
                    {"call": '(fnv1a-hash "abc")', "expect": "3201059673"},   # guessed
                    {"call": '(fnv1a-hash "a")', "expect": "3387592795"}]}   # guessed

    def test_reference_oracle_corrects_guessed_vectors_when_code_is_right(self):
        plan = self._fnv_plan()
        with tempfile.TemporaryDirectory() as tmp:
            sess = _session(tmp)
            out = sess._rehearse(plan, "")
            self.assertTrue(out["ok"], out)
            self.assertEqual(plan["tests"][0]["expect"], "440920331")
            self.assertEqual(plan["tests"][1]["expect"], "3826002220")
            self.assertIn("independent reference", plan["tests"][0]["source"])
            self.assertEqual(_kinds(sess).count("oracle_corrected"), 2)

    def test_reference_oracle_overrules_wrong_code_and_freezes_the_value(self):
        bad = FNV_OK.replace("2166136261", "2166136260")           # wrong offset basis
        plan = self._fnv_plan(bad)
        with tempfile.TemporaryDirectory() as tmp:
            sess = _session(tmp)
            out = sess._rehearse(plan, "")
            self.assertFalse(out["ok"])
            self.assertIn("independent fnv1a32 reference says 440920331", out["detail"])
            self.assertIn("oracle_reference", _kinds(sess))
            # the true value is now frozen: the model may not "fix" the test back
            changed = self._fnv_plan(bad, tests=[
                {"call": '(fnv1a-hash "abc")', "expect": "3201059673"}])
            self.assertIn("SPEC_INCONSISTENT", ag.validate_build(changed, sess._frozen))

    def test_contradictory_tests_in_one_plan_are_rejected(self):
        plan = self._fnv_plan(tests=[{"call": "(f 1)", "expect": "1"},
                                     {"call": "(f 1)", "expect": "2"}])
        self.assertIn("SPEC_INCONSISTENT", ag.validate_build(plan))

    def test_passed_tests_are_frozen(self):
        plan = {"name": "sq", "description": "d", "definition": "(defun sq (x) (* x x))",
                "call": "(sq 3)", "tests": [{"call": "(sq 3)", "expect": "9"}]}
        with tempfile.TemporaryDirectory() as tmp:
            sess = _session(tmp)
            self.assertTrue(sess._rehearse(plan, "")["ok"])
            self.assertEqual(sess._frozen["(sq 3)"], "9")
            drifted = dict(plan, tests=[{"call": "(sq 3)", "expect": "10"}])
            self.assertIn("frozen", ag.validate_build(drifted, sess._frozen))

    def test_failure_is_classified_and_low_confidence_means_test_wrong(self):
        guessed = {"name": "h", "description": "d", "definition": "(defun h (s) 7)",
                   "call": '(h "a")', "tests": [{"call": '(h "a")', "expect": "3201059673"}]}
        crash = {"name": "g", "description": "d", "definition": "(defun g (x) (car x))",
                 "call": "(g 5)", "tests": [{"call": "(g 5)", "expect": "1"}]}
        wrong = {"name": "w", "description": "d", "definition": "(defun w (x) (+ x 1))",
                 "call": "(w 1)", "tests": [{"call": "(w 1)", "expect": "5"}]}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_session(tmp)._rehearse(guessed, "")["class"], "TEST_WRONG")
            self.assertEqual(_session(tmp)._rehearse(crash, "")["class"], "RUNTIME_ERROR")
            self.assertEqual(_session(tmp)._rehearse(wrong, "")["class"], "IMPLEMENTATION_WRONG")

    def test_changing_an_expected_value_between_attempts_is_flagged_as_drift(self):
        code = "(defun d (x) (+ x 5))"

        def plan(expect):
            return {"name": "d", "description": "d", "definition": code,
                    "call": "(d 1)", "tests": [{"call": "(d 1)", "expect": expect}]}
        with tempfile.TemporaryDirectory() as tmp:
            sess = _session(tmp)
            first = sess._rehearse(plan("7"), "")           # code gives 6
            second = sess._rehearse(plan("8"), "")          # model changed its mind
            self.assertEqual(first["class"], "IMPLEMENTATION_WRONG")
            self.assertEqual(second["class"], "AMBIGUOUS")
            self.assertEqual(second["drift"], ["(d 1)"])

    def test_compile_error_gets_a_syntax_only_repair_and_feeds_lessons(self):
        prompts = []
        broken = {"action": "build", "name": "f", "description": "d",
                  "definition": "(defun f (x) (let ((y 0)) (setf y) y))",   # odd SETF
                  "call": "(f 2)", "tests": [{"call": "(f 2)", "expect": "2"}]}
        fixed = dict(broken, definition="(defun f (x) (let ((y 0)) (setf y x) y))")
        replies = iter([broken, fixed])

        def gen(system, user):
            prompts.append(user)
            return ag._fake(next(replies))
        lessons = ag.orc.LessonStore()
        with tempfile.TemporaryDirectory() as tmp:
            sess = _session(tmp, gen=gen, lessons=lessons)
            sess.run()
            self.assertEqual(sess.state, "done",
                             [e for e in sess.events if e["kind"] in ("error", "gave_up")])
            verdicts = [e for e in sess.events if e["kind"] == "verdict"]
            self.assertEqual(verdicts[0]["class"], "COMPILER_ERROR")
            self.assertIn("Fix ONLY the compile error", prompts[1])
            self.assertEqual(lessons.counts().get("setf-pairs"), 1)

    def test_recurring_lessons_are_injected_into_later_prompts(self):
        lessons = ag.orc.LessonStore()
        lessons.note(["setf-pairs", "setf-pairs"])
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            sess = _session(tmp, lessons=lessons,
                            gen=lambda s, u: (seen.append(u), ag._fake({"action": "use", "call": "(zz)"}))[1])
            sess._user_prompt()
            self.assertIn("RECURRING SLIPS TO AVOID", sess._user_prompt())
            self.assertIn("flat place/value pairs", sess._user_prompt())

    def test_identical_failed_candidate_is_skipped_not_rerun(self):
        calls = {"n": 0}

        def worker(code):
            calls["n"] += 1
            return {"ok": True, "stdout": "", "return_value": "(:GOT 1)", "error": "",
                    "timed_out": False, "elapsed_ms": 1.0}
        bad = {"action": "build", "name": "k", "description": "d",
               "definition": "(defun k (x) x)", "call": "(k 1)",
               "tests": [{"call": "(k 1)", "expect": "2"}]}
        good = dict(bad, tests=[{"call": "(k 1)", "expect": "T"}])
        replies = iter([bad, bad, good])
        gen = lambda s, u: ag._fake(next(replies))  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            sess = _session(tmp, gen=gen, worker_fn=worker)
            sess.run()
            self.assertIn("repeat_candidate", _kinds(sess))
            # one run for the first bad attempt (the repeat never reached the worker)
            reps = [e for e in sess.events if e["kind"] == "verdict" and e.get("stage") == "repeat"]
            self.assertEqual(len(reps), 1)

    def test_efficiency_and_postmortem_for_a_failed_session(self):
        bad = {"action": "build", "name": "k", "description": "d",
               "definition": "(defun k (x) (+ x 1))", "call": "(k 1)",
               "tests": [{"call": "(k 1)", "expect": "50"}]}
        variants = [dict(bad, definition="(defun k (x) (+ x %d))" % i) for i in range(1, 6)]
        replies = iter(variants)
        gen = lambda s, u: ag._fake(next(replies))  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            sess = _session(tmp, gen=gen, postmortem_dir=Path(tmp) / "pm")
            sess.run()
            self.assertEqual(sess.state, "failed")
            summ = [e for e in sess.events if e["kind"] == "summary"][0]
            self.assertGreaterEqual(summ["efficiency"]["failed_attempts"], 3)
            self.assertGreaterEqual(summ["efficiency"]["repeated_errors"], 1)
            files = list((Path(tmp) / "pm").glob("*.json"))
            self.assertEqual(len(files), 1)
            pm = json.loads(files[0].read_text())
            self.assertEqual(pm["outcome"], "failed")
            self.assertIn(pm["root_cause"], ("IMPLEMENTATION_WRONG", "RUNTIME_ERROR", "TEST_WRONG"))
            self.assertTrue(pm["failures"])
            self.assertIn("efficiency", pm)

    def test_saved_tools_carry_oracle_provenance(self):
        plan = {"name": "sq", "description": "d", "definition": "(defun sq (x) (* x x))",
                "call": "(sq 3)", "tests": [{"call": "(sq 3)", "expect": "9"},
                                            {"call": "(= (sq 2) (sq 2))", "expect": "T"}]}
        with tempfile.TemporaryDirectory() as tmp:
            sess = _session(tmp)
            self.assertTrue(sess._rehearse(plan, "")["ok"])
            sess._promote(plan)
            saved = sess.registry.load()[0]["tests"]
            self.assertEqual(saved[0]["confidence"], "high")
            self.assertIn("model-written", saved[0]["source"])
            self.assertIn("property", saved[1]["source"])


class FailedRunReplayTests(unittest.TestCase):
    """The hash-password run that failed live: wrapped SETF pairs plus guessed vectors."""

    LIVE_DEFINITION = (
        "(defun fnv1a-hash (str) (let ((hash 2166136261) (prime 16777619) "
        "(mask #xFFFFFFFF)) (dotimes (i (length str)) (setf hash "
        "(logand mask (logxor hash (char-code (char str i)))) "
        "(hash (mod (* hash prime) #x100000000)))) hash))")

    def test_the_exact_live_failure_now_succeeds_in_one_attempt(self):
        plan = {"action": "build", "name": "fnv1a-hash",
                "description": "32-bit FNV-1a hash of a string",
                "definition": self.LIVE_DEFINITION, "call": '(fnv1a-hash "abc")',
                "tests": [
                    {"call": '(fnv1a-hash "")', "expect": "2166136261"},
                    {"call": '(fnv1a-hash "a")', "expect": "3387592795"},        # guessed
                    {"call": '(fnv1a-hash "abc")', "expect": "3201059673"},      # guessed
                    {"call": '(fnv1a-hash "foobar")', "expect": "1116502948"}]}  # guessed
        replies = iter([plan])
        gen = lambda s, u: ag._fake(next(replies))  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            sess = ag.Session("write a password hasher", gen, registry=reg,
                              log_path=Path(tmp) / "l.jsonl")
            sess.run()
            self.assertEqual(sess.state, "done",
                             [e for e in sess.events if e["kind"] in ("error", "gave_up")])
            self.assertEqual(sess.model_calls, 1)            # no repair rounds at all
            self.assertEqual(_kinds(sess).count("oracle_corrected"), 3)
            saved = reg.load()[0]
            self.assertEqual([t["expect"] for t in saved["tests"]],
                             ["2166136261", "3826002220", "440920331", "3214735720"])
            self.assertTrue(all("independent reference" in t["source"]
                                for t in saved["tests"][1:]))
            res = [e for e in sess.events if e["kind"] == "result"][-1]
            self.assertEqual(res["value"], "440920331")      # the real FNV-1a of "abc"
            summ = [e for e in sess.events if e["kind"] == "summary"][0]
            self.assertEqual(summ["oracle_fixes"], 3)
            self.assertEqual(summ["efficiency"]["failed_attempts"], 0)


class SummaryTests(unittest.TestCase):
    def test_planned_build_ends_with_a_success_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("write a ray tracer", ag.demo_generate,
                              registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              log_path=Path(tmp) / "l.jsonl")
            sess.run()
            summ = [e for e in sess.events if e["kind"] == "summary"][0]
            self.assertEqual(summ["outcome"], "success")
            self.assertEqual(summ["flow"], "plan")
            self.assertEqual([b["name"] for b in summ["built"]],
                             ["vec-dot", "vec-sub", "ray-hits-sphere-p", "render-sphere-ascii"])
            self.assertEqual(summ["planned"], 4)
            self.assertGreater(summ["tests_passed"], 4)
            self.assertEqual(summ["answer"]["tool"], "render-sphere-ascii")
            self.assertEqual(sess.events[-1]["kind"], "done")        # summary comes first

    def test_failed_run_summary_lists_partial_progress_and_why(self):
        good = {"action": "build", "name": "dbl", "description": "d",
                "definition": "(defun dbl (x) (* 2 x))", "call": "(dbl 2)",
                "tests": [{"call": "(dbl 2)", "expect": "4"}]}
        bad = {"action": "build", "name": "big", "description": "d",
               "definition": '(defun big (x) (error "boom"))', "call": "(big 2)",
               "tests": [{"call": "(big 2)", "expect": "4"}]}
        replies = iter([{"action": "plan", "steps": [
            {"name": "dbl", "spec": "dbl"}, {"name": "big", "spec": "big"}]},
            good, bad, bad, bad, bad, {"action": "build"}])
        gen = lambda s, u: ag._fake(next(replies))  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              log_path=Path(tmp) / "l.jsonl")
            sess.run()
            summ = [e for e in sess.events if e["kind"] == "summary"][0]
            self.assertEqual(summ["outcome"], "failed")
            self.assertEqual([b["name"] for b in summ["built"]], ["dbl"])
            self.assertTrue(summ["stopped"]["detail"])


class SplitTests(unittest.TestCase):
    @staticmethod
    def _build(name, definition, call, expect):
        return {"action": "build", "name": name, "description": name,
                "definition": definition, "call": call,
                "tests": [{"call": call, "expect": expect}]}

    def test_failed_step_is_split_then_retried_with_helpers(self):
        bad = self._build("big", '(defun big (x) (error "boom"))', "(big 2)", "4")
        replies = iter([
            {"action": "plan", "steps": [{"name": "big", "spec": "big (x)"}]},
            bad, bad, bad, bad,                       # first try + 3 repairs fail
            {"action": "plan", "steps": [{"name": "dbl", "spec": "dbl (x)"}]},
            self._build("dbl", "(defun dbl (x) (* 2 x))", "(dbl 2)", "4"),
            self._build("big", "(defun big (x) (dbl x))", "(big 2)", "4"),
            {"action": "use", "call": "(big 3)"},
        ])
        gen = lambda s, u: ag._fake(next(replies))  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            sess = ag.Session("write a big thing", gen, registry=reg,
                              log_path=Path(tmp) / "l.jsonl")
            sess.run()
            self.assertEqual(sess.state, "done",
                             [e for e in sess.events if e["kind"] in ("error", "gave_up")])
            self.assertIn("replan", _kinds(sess))
            self.assertEqual([t["name"] for t in reg.load()], ["dbl", "big"])
            res = [e for e in sess.events if e["kind"] == "result"][-1]
            self.assertEqual(res["value"], "6")

    def test_split_that_also_fails_gives_up_cleanly(self):
        bad = self._build("big", '(defun big (x) (error "boom"))', "(big 2)", "4")
        replies = iter([{"action": "plan", "steps": [{"name": "big", "spec": "big"}]},
                        bad, bad, bad, bad,
                        {"action": "build"}])        # split reply is not a plan
        gen = lambda s, u: ag._fake(next(replies))  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            sess = ag.Session("x", gen, registry=ag.ToolRegistry(Path(tmp) / "t.json"),
                              log_path=Path(tmp) / "l.jsonl")
            sess.run()
            self.assertEqual(sess.state, "failed")
            self.assertIn("gave_up", _kinds(sess))


class LispHintTests(unittest.TestCase):
    def test_unbound_variable_gets_let_star_hint(self):
        h = ag.lisp_hint("(f 1): The variable SQRT-DISC is unbound.")
        self.assertIn("LET*", h)
        self.assertEqual(ag.lisp_hint("(f 1): got 2, expected 3"), "")


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

    def test_failed_build_repairs_thrice_then_fails(self):
        replies = iter([
            {"action": "build", "name": "bad", "description": "d",
             "definition": "(defun bad (x) x)",
             "tests": [{"call": "(bad 1)", "expect": "2"}],
             "call": "(bad 1)"},
        ] * 4)
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
            self.assertEqual(sess.model_calls, 4)

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
