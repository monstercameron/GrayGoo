"""Tests for autonomy_experiment: specs, reference answers, arm wiring, cap, resume, CLI.

Nothing here calls a paid model. Every build uses a scripted generator, and the
entry point is checked to refuse a live run without --live and --max-usd.
"""

import contextlib
import heapq
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import autonomy_experiment as ae  # noqa: E402

BANNED_WORDS = re.compile(r"\b(blog|todo|task|product|customer|crm|login|user)s?\b", re.I)
REQUIREMENT_SHAPES = [
    re.compile(r'^scenario: \S.*$'),
    re.compile(r'^(GET|POST) /\S*( with \S.*?)?( (shows|does not show) "[^"]*"| answers \d{3}'
               r'| redirects to /\S*| then GET /\S* (shows|does not show) "[^"]*")$'),
    re.compile(r'^run "[^"]*"( then run "[^"]*")?( (prints|does not print) "[^"]*")$'),
]


def _snapshot(root):
    """Every file under ROOT with its mtime and size, to prove a run wrote nothing there."""
    out = {}
    if root.exists():
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                p = os.path.join(dirpath, name)
                st = os.stat(p)
                out[p] = (st.st_mtime_ns, st.st_size)
    return out


# -- reference computations, written independently of the module -----------

def cheapest(edges, src, dst):
    """Dijkstra over directed (from, to, weight) edges: (total, [node names]) or None."""
    graph = {}
    for a, b, w in edges:
        graph.setdefault(a, []).append((b, w))
    best, prev, heap = {src: 0}, {}, [(0, src)]
    while heap:
        d, node = heapq.heappop(heap)
        if node == dst:
            break
        if d > best.get(node, float("inf")):
            continue
        for nxt, w in graph.get(node, []):
            if d + w < best.get(nxt, float("inf")):
                best[nxt], prev[nxt] = d + w, node
                heapq.heappush(heap, (d + w, nxt))
    if dst not in best:
        return None
    path, node = [dst], dst
    while node != src:
        node = prev[node]
        path.append(node)
    return best[dst], list(reversed(path))


def simulate(service, arrivals):
    """Single server, first come first served: (waits, busy time, time last customer leaves)."""
    free, waits, busy, last = 0, [], 0, 0
    for arrival in arrivals:
        start = max(arrival, free)
        waits.append(start - arrival)
        free = start + service
        busy += service
        last = free
    return waits, busy, last


def earliest(people, length, day=(9, 17)):
    """Earliest whole-hour start H in the day such that everyone is free H .. H+length."""
    free = []
    for busy in people:
        hours = set(range(day[0], day[1]))
        for a, b in busy:
            hours -= set(range(a, b))
        free.append(hours)
    common = set.intersection(*free)
    for h in range(day[0], day[1]):
        if h + length <= day[1] and all(h + k in common for k in range(length)):
            return h
    return None


def expr_parse(text):
    """Tokens to a tree of (op, left, right) and ints, with usual precedence, left associative."""
    toks = re.findall(r"\d+|[()+\-*/]", text)
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def take():
        pos[0] += 1
        return toks[pos[0] - 1]

    def primary():
        t = peek()
        if t is None:
            raise ValueError("unexpected end of input")
        if t == "(":
            take()
            node = additive()
            if peek() != ")":
                raise ValueError("expected )")
            take()
            return node
        if t.isdigit():
            return int(take())
        raise ValueError("unexpected " + t)

    def multiplicative():
        node = primary()
        while peek() in ("*", "/"):
            op = take()
            node = (op, node, primary())
        return node

    def additive():
        node = multiplicative()
        while peek() in ("+", "-"):
            op = take()
            node = (op, node, multiplicative())
        return node

    tree = additive()
    if peek() is not None:
        raise ValueError("unexpected " + peek())
    return tree


def expr_show(tree):
    return str(tree) if isinstance(tree, int) else "(%s %s %s)" % (
        expr_show(tree[1]), tree[0], expr_show(tree[2]))


def expr_value(tree):
    if isinstance(tree, int):
        return tree
    op, a, b = tree[0], expr_value(tree[1]), expr_value(tree[2])
    if op == "/" and b == 0:
        raise ZeroDivisionError("division by zero")
    return {"+": a + b, "-": a - b, "*": a * b, "/": a // b}[op]


def _requirement_units(text):
    """Top-level lines: each is one requirement, and a scenario block counts once."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.startswith(" ")]


def _spec(spec_id):
    return next(s for s in ae.SPECS if s["id"] == spec_id)


def _costly(system, user):
    reply = ae.scripted_generate(system, user)
    reply["cost_usd"] = 0.5
    return reply


class SpecTests(unittest.TestCase):

    def test_four_specs_two_web_two_cli_three_prompts_each(self):
        ids = [s["id"] for s in ae.SPECS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(s["kind"] for s in ae.SPECS), ["cli", "cli", "web", "web"])
        for s in ae.SPECS:
            self.assertEqual(len(s["prompts"]), 3, s["id"])

    def test_no_banned_words_in_specs(self):
        for s in ae.SPECS:
            text = " ".join([s["title"]] + s["prompts"] + [s["requirements"]])
            self.assertIsNone(BANNED_WORDS.search(text), s["id"])
            # "POST" in the requirement grammar is upper case; a lower-case post is banned
            self.assertIsNone(re.search(r"\bpost\b", text), s["id"])

    def test_requirement_lines_have_the_grammar_shape(self):
        for s in ae.SPECS:
            for line in s["requirements"].splitlines():
                if not line.strip():
                    continue
                body = line.strip()
                self.assertTrue(any(p.match(body) for p in REQUIREMENT_SHAPES),
                                "%s: %r" % (s["id"], line))

    def test_four_to_six_requirements_per_spec(self):
        for s in ae.SPECS:
            units = _requirement_units(s["requirements"])
            self.assertTrue(4 <= len(units) <= 6, "%s has %d" % (s["id"], len(units)))

    def test_requirements_parse_with_the_module(self):
        try:
            req = importlib.import_module("requirements")
        except ImportError:
            self.skipTest("requirements module is not present")
        for s in ae.SPECS:
            reqs, errors = req.parse(s["requirements"])
            self.assertEqual(errors, [], s["id"])
            self.assertEqual(len(reqs), len(_requirement_units(s["requirements"])), s["id"])


class ReferenceTests(unittest.TestCase):

    def test_graph_cheapest_route_matches_hand_value(self):
        edges = [("A", "B", 4), ("A", "C", 1), ("C", "B", 2), ("B", "D", 5)]
        total, path = cheapest(edges, "A", "D")
        self.assertEqual((total, path), (8, ["A", "C", "B", "D"]))
        self.assertIn("A -> C -> B -> D (total 8)", ae.GRAPH_REQ)
        self.assertIn("A -> C -> B -> D (total 8)", _spec("graph")["prompts"][0])
        self.assertIsNone(cheapest(edges, "D", "A"))
        self.assertIn('shows "no path"', ae.GRAPH_REQ)

    def test_queue_waits_average_and_utilisation(self):
        waits, busy, last = simulate(4, [0, 2, 3, 7])
        self.assertEqual(waits, [0, 2, 5, 5])
        self.assertEqual("average wait %.2f" % (sum(waits) / len(waits)), "average wait 3.00")
        self.assertEqual(max(waits), 5)
        self.assertIn('"average wait 3.00"', ae.QUEUE_REQ)
        self.assertIn('"longest wait 5"', ae.QUEUE_REQ)
        waits, busy, last = simulate(4, [0, 10])
        self.assertEqual("average wait %.2f" % (sum(waits) / len(waits)), "average wait 0.00")
        self.assertEqual(busy * 100 // last, 57)
        self.assertIn('"utilisation 57%"', ae.QUEUE_REQ)
        self.assertIn("utilisation 57%", _spec("queue")["prompts"][1])

    def test_meetings_earliest_slots(self):
        people = [[(9, 10), (13, 14)], [(10, 12)]]
        self.assertEqual(earliest(people, 1), 12)
        self.assertEqual(earliest(people, 2), 14)
        self.assertIsNone(earliest(people, 4))
        self.assertIn('shows "earliest 12"', ae.MEETINGS_REQ)
        self.assertIn('shows "earliest 14"', ae.MEETINGS_REQ)
        self.assertIn('shows "no common slot"', ae.MEETINGS_REQ)

    def test_expression_steps_and_values(self):
        tree = expr_parse("2 + 3 * 4")
        self.assertEqual(expr_show(tree), "(2 + (3 * 4))")
        self.assertEqual(expr_value(tree), 14)
        self.assertEqual(expr_show(expr_parse("(2 + 3) * 4")), "((2 + 3) * 4)")
        self.assertEqual(expr_value(expr_parse("(2 + 3) * 4")), 20)
        self.assertEqual(expr_value(expr_parse("2 - 3 - 4")), -5)
        with self.assertRaises(ZeroDivisionError):
            expr_value(expr_parse("7 / 0"))
        with self.assertRaisesRegex(ValueError, "unexpected end of input"):
            expr_parse("2 +")
        for text in ('"parse: (2 + (3 * 4))"', '"result 14"', '"result 20"', '"result -5"',
                     '"error: division by zero"', '"error: unexpected end of input"'):
            self.assertIn(text, ae.EXPR_REQ)


class ArmTests(unittest.TestCase):

    def test_arm_definitions(self):
        arms = {a["id"]: a for a in ae.ARMS}
        self.assertEqual(sorted(arms), ["A", "B", "C", "D", "K"])
        flags = {k: (v["kit"], v["advice"], v["lessons"], v["memory"]) for k, v in arms.items()}
        self.assertEqual(flags, {"A": (False, False, False, False),
                                 "B": (False, False, False, True),
                                 "C": (False, False, True, True),
                                 "D": (True, True, True, True),
                                 "K": (True, False, False, True)})

    def test_each_arm_configures_its_sessions(self):
        spec = _spec("queue")
        for arm in ae.ARMS:
            with self.subTest(arm=arm["id"]):
                seen = []
                original = ag.Session.run

                def spy(sess, _seen=seen, _orig=original):
                    _seen.append({"path": str(sess.registry.path), "kit": sess.use_kit,
                                  "advice": sess.use_advice, "lessons": sess.lessons,
                                  "visual": sess.visual,
                                  "start_empty": sess.registry.load() == []})
                    return _orig(sess)

                work = Path(tempfile.mkdtemp(prefix="ae-arm-"))
                store = ae.orc.LessonStore(work / "shared-lessons.json") if arm["lessons"] else None
                try:
                    with mock.patch.object(ag.Session, "run", spy):
                        record = ae.run_cell(spec, arm, ae.scripted_generate, work, mode="demo",
                                             lessons=store)
                finally:
                    shutil.rmtree(work, ignore_errors=True)
                self.assertEqual(len(seen), 3)
                self.assertTrue(all(s["kit"] == arm["kit"] for s in seen))
                self.assertTrue(all(s["advice"] == arm["advice"] for s in seen))
                self.assertTrue(all(s["visual"] is False for s in seen))
                if arm["lessons"]:
                    self.assertTrue(all(s["lessons"] is store for s in seen))
                else:
                    self.assertTrue(all(s["lessons"] is None for s in seen))
                paths = [s["path"] for s in seen]
                if arm["memory"]:
                    self.assertEqual(len(set(paths)), 1, "one registry shared by the prompts")
                else:
                    self.assertEqual(len(set(paths)), 3, "a fresh registry per prompt")
                    self.assertTrue(all(s["start_empty"] for s in seen))
                    self.assertIn("note", record)
                if arm["memory"]:
                    # the first prompt's saved function is what the later prompts start with
                    self.assertTrue(all(not s["start_empty"] for s in seen[1:]))
                self.assertEqual(record["arm"], arm["id"])
                self.assertEqual(len(record["prompts"]), 3)

    def test_cell_record_has_the_agreed_fields(self):
        work = Path(tempfile.mkdtemp(prefix="ae-rec-"))
        try:
            record = ae.run_cell(_spec("expr"), ae.ARMS[0], ae.scripted_generate, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        for key in ("spec", "arm", "repeat", "prompts", "completed", "requirements",
                    "provenance", "functions_agent", "functions_harness", "totals"):
            self.assertIn(key, record)
        self.assertEqual(set(record["prompts"][0]),
                         {"state", "model_calls", "cost_usd", "input_tokens", "output_tokens",
                          "seconds", "built", "harness"})
        self.assertEqual(set(record["totals"]), {"model_calls", "cost_usd", "tokens", "seconds"})


class ExperimentTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ae-exp-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_spend_cap_refuses_a_cell_that_would_exceed_it(self):
        calls = []

        def counting(system, user):
            calls.append(1)
            return ae.scripted_generate(system, user)

        result = ae.run_experiment([_spec("queue")], [ae.ARMS[0]], counting, self.tmp / "out",
                                   max_usd=0.5, repeats=1, progress=lambda line: None)
        self.assertEqual(result["cells"], [])
        self.assertEqual(calls, [])
        self.assertIn("spend cap", result["stopped"])
        self.assertFalse(result["complete"])
        self.assertTrue((self.tmp / "out" / "results.json").exists())

    def test_cap_stops_before_the_next_cell(self):
        lines = []
        result = ae.run_experiment([_spec("queue")], [ae.ARMS[0], ae.ARMS[1]], _costly,
                                   self.tmp / "out", max_usd=2.0, repeats=1,
                                   progress=lines.append)
        self.assertEqual(len(result["cells"]), 1)
        self.assertGreaterEqual(result["spent_usd"], 0.8)
        self.assertGreater(result["spent_usd"] + 3 * ae.WORST_BUILD_USD, 2.0)
        self.assertIn("spend cap", result["stopped"])
        self.assertTrue(any(line.startswith("STOP") for line in lines))

    def test_results_written_after_every_cell_and_resume_skips_done_cells(self):
        out = self.tmp / "out"
        calls = [0]

        def counting(system, user):
            calls[0] += 1
            return ae.scripted_generate(system, user)

        seen = []

        def progress(line):
            data = json.loads((out / "results.json").read_text(encoding="utf-8"))
            seen.append(len(data["cells"]))

        first = ae.run_experiment([_spec("queue")], [ae.ARMS[0], ae.ARMS[1]], counting, out,
                                  repeats=1, progress=progress)
        self.assertEqual(seen, [1, 2], "the file already holds each cell when its line prints")
        self.assertTrue(first["complete"])
        calls_after_first = calls[0]
        self.assertGreater(calls_after_first, 0)
        again_lines = []
        second = ae.run_experiment([_spec("queue")], [ae.ARMS[0], ae.ARMS[1]], counting, out,
                                   repeats=1, progress=again_lines.append)
        self.assertEqual(again_lines, [], "no cell is run again")
        self.assertEqual(calls[0], calls_after_first, "the resumed run made no model call")
        self.assertEqual(len(second["cells"]), 2)

    def test_resume_refuses_a_different_plan(self):
        out = self.tmp / "out"
        ae.run_experiment([_spec("queue")], [ae.ARMS[0]], ae.scripted_generate, out,
                          progress=lambda line: None)
        with self.assertRaises(ValueError):
            ae.run_experiment([_spec("queue")], [ae.ARMS[1]], ae.scripted_generate, out,
                              progress=lambda line: None)

    def test_lessons_store_is_shared_across_an_arm_and_absent_elsewhere(self):
        seen = []
        original = ae.run_cell

        def spy(*args, **kwargs):
            seen.append((kwargs["lessons"] if "lessons" in kwargs else None,
                         args[1]["id"]))
            return original(*args, **kwargs)

        with mock.patch.object(ae, "run_cell", side_effect=spy):
            ae.run_experiment([_spec("queue"), _spec("expr")],
                              [ae.ARMS[0], ae.ARMS[2]], ae.scripted_generate, self.tmp / "out",
                              progress=lambda line: None)
        arm_c = [store for store, arm in seen if arm == "C"]
        self.assertEqual(len(arm_c), 2)
        self.assertIsNotNone(arm_c[0])
        self.assertIs(arm_c[0], arm_c[1])
        self.assertTrue(all(store is None for store, arm in seen if arm == "A"))


class TableTests(unittest.TestCase):

    def _result(self, repeats=1):
        cells = [
            {"arm": "A", "spec": "graph", "completed": True,
             "requirements": {"met": 1, "total": 5},
             "totals": {"model_calls": 3, "cost_usd": 0.10, "tokens": 1000, "seconds": 10.0},
             "functions_agent": 2, "functions_harness": 0},
            {"arm": "A", "spec": "queue", "completed": False,
             "requirements": {"met": 2, "total": 6},
             "totals": {"model_calls": 5, "cost_usd": 0.30, "tokens": 2000, "seconds": 20.0},
             "functions_agent": 1, "functions_harness": 0},
            {"arm": "B", "spec": "graph", "completed": True,
             "requirements": {"met": 4, "total": 5},
             "totals": {"model_calls": 4, "cost_usd": 0.20, "tokens": 1500, "seconds": 12.0},
             "functions_agent": 3, "functions_harness": 0},
            {"arm": "B", "spec": "queue", "completed": True,
             "requirements": {"met": 6, "total": 6},
             "totals": {"model_calls": 6, "cost_usd": 0.40, "tokens": 2500, "seconds": 25.0},
             "functions_agent": 2, "functions_harness": 0},
        ]
        return {"arms": ["A", "B"], "repeats": repeats, "cells": cells}

    def test_contrast_sentences_are_computed_from_the_rows(self):
        text = ae.table(self._result())
        self.assertIn("Memory (B vs A): completed 2/2 vs 1/2, requirements met 10/11 vs 3/11, "
                      "mean model calls 5.0 vs 4.0, mean cost $0.30 vs $0.20 per cell.", text)
        self.assertIn("Lessons (C vs B): not run, one of the arms is not in this result.", text)
        self.assertIn("Kit (K vs B): not run, one of the arms is not in this result.", text)
        self.assertIn("Advice (D vs K): not run, one of the arms is not in this result.", text)

    def test_arm_row_values(self):
        row = [ln for ln in ae.table(self._result()).splitlines() if ln.startswith("A ")][0]
        for part in ("2", "1/2", "3/11", "4.0", "1500", "0.2000", "15.0", "1.5"):
            self.assertIn(part, row.split())

    def test_caution_only_with_one_repeat(self):
        self.assertIn("Caution: each cell ran once", ae.table(self._result(repeats=1)))
        self.assertNotIn("Caution", ae.table(self._result(repeats=3)))

    def test_unknown_requirements_show_as_not_applicable(self):
        result = self._result()
        for c in result["cells"]:
            c["requirements"] = None
        self.assertIn("n/a", ae.table(result).splitlines()[2])


class EstimateTests(unittest.TestCase):

    def test_one_repeat(self):
        plan = ae.estimate(ae.SPECS, ae.ARMS, 1)
        self.assertEqual((plan["cells"], plan["builds"]), (20, 60))
        self.assertEqual(plan["worst_case_usd"], 24.00)
        self.assertEqual(plan["typical_usd"], 7.20)
        self.assertEqual(plan["per_build_worst_usd"], ag.MAX_SESSION_USD)

    def test_three_repeats(self):
        plan = ae.estimate(ae.SPECS, ae.ARMS, 3)
        self.assertEqual((plan["cells"], plan["builds"]), (60, 180))
        self.assertEqual(plan["worst_case_usd"], 72.00)
        self.assertEqual(plan["typical_usd"], 21.60)


class ProvenanceAndAdapterTests(unittest.TestCase):

    def test_fallback_counts_use_the_kit_flag(self):
        tools = [{"name": "a"}, {"name": "k", "kit": True}, {"name": "w", "session": "web-kit"}]
        self.assertEqual(ae._function_counts(tools, None), (1, 2, "registry-flag"))

    def test_provenance_counts_group_imported_with_harness(self):
        prov = {"functions": {"HARNESS_PRIMITIVE": 4, "AGENT_GENERATED": 2,
                              "AGENT_COMPOSED": 1, "IMPORTED": 1, "total": 8}}
        self.assertEqual(ae._function_counts([], prov), (3, 5, "provenance"))

    def test_missing_requirements_module_records_none(self):
        work = Path(tempfile.mkdtemp(prefix="ae-req-"))
        try:
            with mock.patch.object(ae, "_module", lambda name: None):
                summary, errors = ae._evaluate(_spec("queue"), ag.ToolRegistry(work / "t.json"),
                                               work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self.assertIsNone(summary)
        self.assertIsNone(errors)

    def test_requirements_module_is_called_with_factories(self):
        fake = types.SimpleNamespace()
        fake.parse = lambda text: ([{"id": 1}], [])
        seen = {}

        def run(reqs, app=None, command=None):
            seen["app"], seen["command"] = app, command
            return [{"id": 1, "ok": True}]

        fake.run = run
        fake.summarize = lambda results: {"total": len(results), "met": 1}
        work = Path(tempfile.mkdtemp(prefix="ae-fake-"))
        real = ae._module

        def module(name):
            return fake if name == "requirements" else real(name)

        try:
            with mock.patch.object(ae, "_module", module):
                summary, errors = ae._evaluate(_spec("graph"),
                                               ag.ToolRegistry(work / "t.json"), work)
            self.assertEqual(summary, {"total": 1, "met": 1})
            self.assertIsNone(errors)
            self.assertTrue(callable(seen["app"]) and callable(seen["command"]))
            self.assertIsNot(seen["app"](), seen["app"](), "each factory call is fresh")
        finally:
            shutil.rmtree(work, ignore_errors=True)


class MainTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ae-main-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ae.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_estimate_prints_both_figures_and_exits_zero(self):
        code, out, _ = self._run_main(["--estimate"])
        self.assertEqual(code, 0)
        self.assertIn("= 20 cells, 60 app builds.", out)
        self.assertIn("typical $7.20", out)
        self.assertIn("worst case $24.00", out)
        code, out, _ = self._run_main(["--estimate", "--repeats", "3"])
        self.assertIn("typical $21.60", out)
        self.assertIn("worst case $72.00", out)

    def test_dry_run_exits_zero_and_never_touches_artifacts(self):
        artifacts = ROOT / "artifacts"
        before = _snapshot(artifacts)
        code, out, _ = self._run_main(["--dry-run", "--specs", "queue,expr", "--arms", "A,D",
                                       "--out", str(self.tmp / "dry")])
        after = _snapshot(artifacts)
        self.assertEqual(code, 0)
        self.assertEqual(before, after, "a dry run must not write under artifacts/")
        self.assertIn("Memory (B vs A): not run", out)
        self.assertTrue((self.tmp / "dry" / "results.json").exists())

    def test_dry_run_never_calls_the_live_generator(self):
        def boom(*args, **kwargs):
            raise AssertionError("live_generate must not be called without --live")

        with mock.patch.object(ag, "live_generate", boom), \
                mock.patch.object(ag, "live_status", boom):
            code, _, _ = self._run_main(["--dry-run", "--specs", "queue", "--arms", "A",
                                         "--out", str(self.tmp / "dry")])
        self.assertEqual(code, 0)

    def test_live_without_max_usd_is_refused(self):
        def boom(*args, **kwargs):
            raise AssertionError("nothing may run when the live flags are incomplete")

        with mock.patch.object(ag, "live_generate", boom), \
                mock.patch.object(ag, "live_status", boom):
            code, _, err = self._run_main(["--live"])
        self.assertEqual(code, 2)
        self.assertIn("--max-usd", err)

    def test_live_with_a_non_positive_cap_is_refused(self):
        code, _, err = self._run_main(["--live", "--max-usd", "0"])
        self.assertEqual(code, 2)
        self.assertIn("--max-usd", err)

    def test_live_combined_with_dry_run_is_refused(self):
        code, _, err = self._run_main(["--live", "--dry-run", "--max-usd", "1"])
        self.assertEqual(code, 2)
        self.assertIn("choose one", err)

    def test_no_mode_is_refused(self):
        code, _, err = self._run_main([])
        self.assertEqual(code, 2)
        self.assertIn("nothing to do", err)

    def test_unknown_spec_is_refused(self):
        code, _, err = self._run_main(["--estimate", "--specs", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("nope", err)

    def test_importing_the_module_does_not_load_the_model_client(self):
        code = ("import sys; import autonomy_experiment; "
                "print('cerebras_client' in sys.modules)")
        done = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
