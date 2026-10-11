"""Controlled comparison of GrayGoo's harness support on unfamiliar app specs.

Four arms differ only in harness support (ready-made kit, hand-written advice,
learned lessons, procedural memory); a fifth arm K isolates the kit against B.
Each cell runs one spec's prompts, in order, through agent_session.Session.

    uv run python autonomy_experiment.py --estimate
    uv run python autonomy_experiment.py --dry-run
    uv run python autonomy_experiment.py --live --max-usd 10

A live run needs BOTH --live and --max-usd; without them the entry point
refuses. --dry-run uses a scripted generator that answers every call with one
trivial build, so it costs nothing and never touches the network.

Output: <out>/results.json, rewritten atomically after every cell, so an
interrupted run keeps its finished cells and a second run resumes. Per-cell
logs and registries live under <out>/work. Live runs default to
artifacts/agent/autonomy; dry runs default to a temporary directory.
"""

import argparse
import importlib
import itertools
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path

import agent_session as ag
import oracle as orc

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "artifacts" / "agent" / "autonomy"

TYPICAL_BUILD_USD = 0.12          # mean of logged app builds, as given for the plan
WORST_BUILD_USD = ag.MAX_SESSION_USD   # the live app's per-build spend limit (0.40)
DEFAULT_LIMITS = {"max_usd": ag.MAX_SESSION_USD, "max_calls": ag.MAX_MODEL_CALLS,
                  "max_seconds": ag.MAX_SESSION_SECONDS}

# Each arm sets these Session attributes before run(): use_kit, use_advice and
# lessons (a LessonStore or None). Memory decides whether the prompts of one
# spec share one registry file (True) or each start from an empty one (False).
ARMS = [
    {"id": "A", "label": "generic tools only", "kit": False, "advice": False,
     "lessons": False, "memory": False},
    {"id": "B", "label": "A + procedural memory", "kit": False, "advice": False,
     "lessons": False, "memory": True},
    {"id": "C", "label": "B + learned lessons", "kit": False, "advice": False,
     "lessons": True, "memory": True},
    {"id": "D", "label": "full harness (kit, advice, lessons, memory)", "kit": True,
     "advice": True, "lessons": True, "memory": True},
    {"id": "K", "label": "B + kit, advice off", "kit": True, "advice": False,
     "lessons": False, "memory": True},
]

# A scenario block shares state and counts as ONE requirement (it passes only when
# every indented step passes). Lines outside a scenario each start from a fresh app.
GRAPH_REQ = '''scenario: cheapest route
  POST /nodes with name=A answers 303
  POST /nodes with name=B answers 303
  POST /nodes with name=C answers 303
  POST /nodes with name=D answers 303
  POST /edges with from=A, to=B, weight=4 answers 303
  POST /edges with from=A, to=C, weight=1 answers 303
  POST /edges with from=C, to=B, weight=2 answers 303
  POST /edges with from=B, to=D, weight=5 answers 303
  POST /route with from=A, to=D shows "A -> C -> B -> D (total 8)"
scenario: no path
  POST /nodes with name=A answers 303
  POST /nodes with name=D answers 303
  POST /route with from=D, to=A shows "no path"
scenario: weight rule
  POST /nodes with name=A answers 303
  POST /nodes with name=B answers 303
  POST /edges with from=A, to=B, weight=0 shows "weight must be positive"
GET / shows "Graph editor"
scenario: node count
  POST /nodes with name=A answers 303
  POST /nodes with name=B answers 303
  POST /nodes with name=C answers 303
  POST /nodes with name=D answers 303
  GET /nodes shows "4 nodes"'''

QUEUE_REQ = '''run "simulate 4 0 2 3 7" prints "average wait 3.00"
run "simulate 4 0 2 3 7" prints "longest wait 5"
run "simulate 4 0 10" prints "utilisation 57%"
run "simulate 4 0 10" prints "average wait 0.00"
run "simulate 4" prints "usage: simulate"
run "simulate 4 5 2" prints "error: arrivals must be in order"'''

MEETINGS_REQ = '''scenario: one-hour slot
  POST /people with name=Ann, busy="9-10,13-14" answers 303
  POST /people with name=Ben, busy="10-12" answers 303
  POST /find with length=1 shows "earliest 12"
scenario: two-hour slot
  POST /people with name=Ann, busy="9-10,13-14" answers 303
  POST /people with name=Ben, busy="10-12" answers 303
  POST /find with length=2 shows "earliest 14"
scenario: no common slot
  POST /people with name=Ann, busy="9-10,13-14" answers 303
  POST /people with name=Ben, busy="10-12" answers 303
  POST /find with length=4 shows "no common slot"
GET / shows "Meeting finder"
POST /people with name=Ann, busy="9-10,13-14" then GET /people shows "Ann: 9-10,13-14"
POST /people with name=Cy, busy="17-18" shows "bad interval"'''

EXPR_REQ = '''run "expr 2 + 3 * 4" prints "parse: (2 + (3 * 4))"
run "expr 2 + 3 * 4" prints "result 14"
run "expr (2 + 3) * 4" prints "result 20"
run "expr 2 - 3 - 4" prints "result -5"
run "expr 7 / 0" prints "error: division by zero"
run "expr 2 +" prints "error: unexpected end of input"'''

SPECS = [
    {
        "id": "graph",
        "title": "Directed weighted graph with cheapest route",
        "kind": "web",
        "prompts": [
            "Build a web app for a directed, weighted graph. GET / shows a page whose "
            "heading is Graph editor. POST /nodes with name adds a node called name, then "
            "redirects to / with status 303. POST /edges with from, to and weight adds a "
            "directed edge from node from to node to, then redirects to / with status 303. "
            "The weight must be a whole number above zero; if it is not, POST /edges shows "
            "the text weight must be positive and adds nothing. POST /route with from and to "
            "shows the cheapest path from node from to node to: the node names joined by "
            "' -> ', then ' (total N)' where N is the sum of the weights. If no path exists "
            "it shows the text no path. Example: nodes A, B, C and D; edges A to B weight 4, "
            "A to C weight 1, C to B weight 2, B to D weight 5. The cheapest path from A to D "
            "is A -> C -> B -> D (total 8).",
            "Also add GET /nodes, which lists every node name, one per line, in the order "
            "the nodes were added. Keep everything else as it is.",
            "Also make GET /nodes end with a line of the form N nodes, for example 4 nodes. "
            "Keep everything else as it is.",
        ],
        "requirements": GRAPH_REQ,
    },
    {
        "id": "queue",
        "title": "Single-server queue simulation",
        "kind": "cli",
        "prompts": [
            "Build a command-line app that simulates a single-server queue in which every "
            "job needs the same fixed service time. The command is simulate SERVICE "
            "ARRIVAL...: the first number is the service time and the rest are the arrival "
            "times in order, for example simulate 4 0 2 3 7. Jobs are served first come, "
            "first served. For each job print one line with its number, arrival, start, "
            "end and wait. Then print average wait N.NN (two decimals) and longest wait N. For "
            "that example the waits are 0, 2, 5 and 5, so the report ends with average wait "
            "3.00 and longest wait 5. With no arrival times print usage: simulate SERVICE "
            "ARRIVAL...",
            "Also print a last line utilisation N%, where N is the busy time divided by the "
            "time the last job leaves, rounded down. For simulate 4 0 10 the server is "
            "busy for 8 units and the last job leaves at time 14, so that run ends with "
            "the line utilisation 57%.",
            "Also reject bad input. If the arrival times are not in order, print error: "
            "arrivals must be in order and nothing else.",
        ],
        "requirements": QUEUE_REQ,
    },
    {
        "id": "meetings",
        "title": "Group meeting finder",
        "kind": "web",
        "prompts": [
            "Build a web app that finds the earliest meeting time for a group of people. "
            "GET / shows a page whose heading is Meeting finder, with a form to add a person "
            "and a form to search. POST /people with name and busy adds a person. busy is a "
            "comma-separated list of busy hours written start-end, for example 9-10,13-14, "
            "which means the person cannot meet from 9 to 10 and from 13 to 14. Then the "
            "request redirects to / with status 303. POST /find with length looks for the "
            "earliest start hour H, from 9 to 17, such that every person is free for the whole "
            "stretch from H to H plus length, and shows the text earliest H. If no such hour "
            "exists it shows the text no common slot. Example: Ann is busy 9-10,13-14 and Ben "
            "is busy 10-12. Both are free for the hour 12 to 13 and for every hour from 14 to "
            "17. So with length 1 the answer is earliest 12, with length 2 it is earliest 14, "
            "and with length 4 it is no common slot.",
            "Also make GET /people list one line per person in the form name: busy list, for "
            "example Ann: 9-10,13-14.",
            "Also reject a busy interval that ends before it starts or runs outside 9 to 17. "
            "POST /people then shows the text bad interval and adds nobody.",
        ],
        "requirements": MEETINGS_REQ,
    },
    {
        "id": "expr",
        "title": "Arithmetic expression playground",
        "kind": "cli",
        "prompts": [
            "Build a command-line app that is a playground for arithmetic expressions. The "
            "command is expr followed by the rest of the line as one expression made of "
            "numbers, the operators + - * / and parentheses. Tokenise the expression, parse "
            "it with the usual precedence (* and / before + and -, and left to right for "
            "operators of equal precedence), then evaluate it. Print one line per step: "
            "tokens: followed by the tokens separated by single spaces, then parse: followed "
            "by the tree written fully parenthesised, then result followed by the value. "
            "Example: expr 2 + 3 * 4 prints tokens: 2 + 3 * 4, then parse: (2 + (3 * 4)), "
            "then result 14.",
            "Also accept parentheses, so expr (2 + 3) * 4 prints parse: ((2 + 3) * 4) and "
            "result 20. Division by zero prints error: division by zero and nothing else.",
            "Also reject an incomplete expression: expr 2 + prints error: unexpected end of "
            "input.",
        ],
        "requirements": EXPR_REQ,
    },
]

# Scripted reply for --dry-run: every build is one trivial function that passes its test.
DRY_BUILD = {"action": "build", "name": "ping", "description": "Returns ok",
             "definition": '(defun ping () "ok")',
             "tests": [{"call": "(ping)", "expect": '"ok"'}], "call": "(ping)"}


def scripted_generate(system, user):
    """Offline stand-in for the model: no network, no cost. Used only by --dry-run and tests."""
    obj = {"action": "none"} if system == ag.SHORT_SYSTEM else DRY_BUILD
    text = json.dumps(obj)
    return {"text": text, "input_tokens": (len(system) + len(user)) // 4,
            "output_tokens": len(text) // 4, "estimated": True, "cost_usd": 0.0,
            "latency_ms": 0.0, "model": "dry-run-script", "request_id": None,
            "finish_reason": "stop"}


# -- estimates -------------------------------------------------------------

def estimate(specs, arms, repeats, per_build_usd=WORST_BUILD_USD):
    """Cells, app builds and the two dollar figures for a plan."""
    cells = len(specs) * len(arms) * repeats
    builds = sum(len(s["prompts"]) for s in specs) * len(arms) * repeats
    return {"cells": cells, "builds": builds,
            "worst_case_usd": round(builds * per_build_usd, 2),
            "typical_usd": round(builds * TYPICAL_BUILD_USD, 2),
            "per_build_worst_usd": per_build_usd,
            "per_build_typical_usd": TYPICAL_BUILD_USD}


def _plan_text(specs, arms, repeats):
    plan = estimate(specs, arms, repeats)
    lines = ["Plan: %d specs x %d arms x %d repeat(s) = %d cells, %d app builds."
             % (len(specs), len(arms), repeats, plan["cells"], plan["builds"])]
    lines += ["  spec %-9s %s (%s, %d prompts)" % (s["id"], s["title"], s["kind"], len(s["prompts"]))
              for s in specs]
    lines += ["  arm  %s  %s" % (a["id"], a["label"]) for a in arms]
    lines.append("Cost: typical $%.2f (%.2f per build), worst case $%.2f (%.2f per build, the "
                 "live per-build spend limit)."
                 % (plan["typical_usd"], TYPICAL_BUILD_USD, plan["worst_case_usd"], WORST_BUILD_USD))
    return "\n".join(lines)


# -- optional modules: absent until their owners land them -----------------

def _module(name):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _function_counts(tools, prov):
    """(agent_written, harness_supplied, source) for a registry's functions. Harness
    supplied means HARNESS_PRIMITIVE plus IMPORTED, the two kinds provenance does not
    count as learned."""
    if prov and isinstance(prov.get("functions"), dict):
        f = prov["functions"]
        return (f.get("AGENT_GENERATED", 0) + f.get("AGENT_COMPOSED", 0),
                f.get("HARNESS_PRIMITIVE", 0) + f.get("IMPORTED", 0), "provenance")
    kit = sum(1 for t in tools if bool(t.get("kit")) or t.get("session") == "web-kit")
    return len(tools) - kit, kit, "registry-flag"


def _evaluate(spec, registry, cell):
    """(summary, parse_errors). Both None when the requirements module is missing.

    requirements.run takes zero-argument factories and makes a fresh app, or a fresh
    command runner, for each requirement, so each factory here builds a new mounted
    app on its own state file inside the cell's directory."""
    req = _module("requirements")
    if req is None:
        return None, None
    reqs, errors = req.parse(spec["requirements"])
    mount = importlib.import_module("mount")
    counter = itertools.count(1)
    logs = cell / "logs" / "app.jsonl"

    def fresh_app():
        store = mount.StateStore(cell / "state" / ("state-%d.sqlite" % next(counter)))
        return mount.MountedApp(registry, store, log_path=logs)

    def make_app():
        return fresh_app()

    def make_command():
        app = fresh_app()

        def run_words(words):
            out = app.run_command(list(words))
            return out["output"] if out["ok"] else "error: " + out["error"]
        return run_words

    results = req.run(reqs, app=make_app, command=make_command)
    return req.summarize(results), (errors or None)


# -- one cell --------------------------------------------------------------

PROOF_FLAGS = ("write_integration", "goal_check")   # the checks a live project build gets


def _configure_proof(sess, save, saved_text):
    """Turn on the proof checks of a live project build: write_integration, goal_check,
    and save_integration (which keeps the text in the cell). The hidden requirements stay
    out of requirements_text, and pause_on_spend stays off, since nothing would acknowledge
    a pause. A Session that lacks one of the optional attributes is left as it is."""
    for name in PROOF_FLAGS:
        if hasattr(sess, name):
            setattr(sess, name, True)
    if hasattr(sess, "save_integration"):
        sess.save_integration = save
    if hasattr(sess, "integration_text") and saved_text:
        sess.integration_text = saved_text


def _own_verdict(summary):
    """The harness's own verdict on its last build, copied from that session's summary.
    ``strength`` is recorded only when the summary carries it."""
    ver = summary.get("verification") or {}
    qual = summary.get("qualification") or ver.get("qualification") or {}
    out = {"verdict": qual.get("verdict"), "failed": list(qual.get("failed") or []),
           "integration": ver.get("integration"), "goal": ver.get("goal")}
    if "strength" in qual:
        out["strength"] = qual["strength"]
    return out


def run_cell(spec, arm, generate, workdir, mode="demo", limits=None, repeat=0, lessons=None):
    """Run SPEC's prompts in order under ARM. ``lessons`` is the arm's shared store (or None,
    in which case a store private to this cell is made when the arm uses lessons)."""
    limits = dict(DEFAULT_LIMITS, **(limits or {}))
    cell = Path(workdir) / ("%s-%s-r%d" % (spec["id"], arm["id"], repeat))
    if cell.exists():
        shutil.rmtree(cell)
    (cell / "logs").mkdir(parents=True)
    store = None
    if arm["lessons"]:
        store = lessons if lessons is not None else orc.LessonStore(cell / "lessons.json")
    shared = cell / "tools.json"
    prompts, last_path = [], None
    integ = {"text": ""}              # integration tests saved by this cell's sessions

    def keep_integration(text):
        integ["text"] = text
    last_summary, last_state = {}, None
    for i, prompt in enumerate(spec["prompts"], 1):
        path = shared if arm["memory"] else cell / ("prompt-%d" % i) / "tools.json"
        sess = ag.Session(prompt, generate, registry=ag.ToolRegistry(path), mode=mode,
                          log_path=cell / "logs" / ("p%d.jsonl" % i),
                          lessons=store)
        sess.visual = False
        sess.use_kit = arm["kit"]
        sess.use_advice = arm["advice"]
        sess.max_usd = limits["max_usd"]
        sess.max_calls = limits["max_calls"]
        sess.max_seconds = limits["max_seconds"]
        _configure_proof(sess, keep_integration, integ["text"])
        t0 = time.perf_counter()
        sess.run()
        wall = round(time.perf_counter() - t0, 3)
        summary = next((e for e in reversed(sess.events) if e.get("kind") == "summary"), {})
        prompts.append({"state": sess.state, "model_calls": sess.model_calls,
                        "cost_usd": round(sess.cost_usd, 6),
                        "input_tokens": sess.input_tokens, "output_tokens": sess.output_tokens,
                        "seconds": wall, "built": len(summary.get("built") or []),
                        "harness": summary.get("harness")})
        last_path = path
        last_summary, last_state = summary, sess.state
    final = shared if arm["memory"] else last_path
    registry = ag.ToolRegistry(final).for_mode(mode)
    tools = registry.load()
    summ, req_errors = _evaluate(spec, registry, cell)
    prov_mod = _module("provenance")
    prov = prov_mod.audit(tools) if prov_mod is not None and hasattr(prov_mod, "audit") else None
    fn_agent, fn_harness, source = _function_counts(tools, prov)
    totals = {"model_calls": sum(p["model_calls"] for p in prompts),
              "cost_usd": round(sum(p["cost_usd"] for p in prompts), 6),
              "tokens": sum(p["input_tokens"] + p["output_tokens"] for p in prompts),
              "seconds": round(sum(p["seconds"] for p in prompts), 3)}
    record = {"spec": spec["id"], "arm": arm["id"], "repeat": repeat, "mode": mode,
              "prompts": prompts,
              "completed": all(p["state"] == "done" for p in prompts),
              "final_state": last_state, "own_verdict": _own_verdict(last_summary),
              "requirements": summ, "requirement_errors": req_errors,
              "provenance": prov, "functions_agent": fn_agent,
              "functions_harness": fn_harness, "functions_source": source,
              "totals": totals}
    if not arm["memory"]:
        record["note"] = ("Arm A starts every prompt from an empty registry, so the "
                          "requirements are checked against the registry of the last prompt only.")
    return record


# -- the experiment --------------------------------------------------------

def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _store(path, result):
    """Write RESULT with its agreement measures recomputed from the cells."""
    result["agreement"] = agreement(result)
    _write_json(path, result)


def _key(spec, arm, repeat):
    return "%s|%s|%d" % (spec, arm, repeat)


def _eta(seconds_per_cell, remaining):
    if seconds_per_cell is None or remaining <= 0:
        return "0s" if remaining <= 0 else "unknown"
    s = int(seconds_per_cell * remaining)
    return "%dm%02ds" % (s // 60, s % 60)


def run_experiment(specs, arms, generate, outdir, mode="demo", max_usd=None, repeats=1,
                   progress=print, limits=None):
    """Run every cell not yet in OUTDIR/results.json. Stops before a cell whose worst case
    would take the spend past MAX_USD. Returns the result dict."""
    outdir = Path(outdir)
    limits = dict(DEFAULT_LIMITS, **(limits or {}))
    results_path = outdir / "results.json"
    ids = ([s["id"] for s in specs], [a["id"] for a in arms])
    if results_path.exists():
        result = json.loads(results_path.read_text(encoding="utf-8"))
        if [result.get("specs"), result.get("arms")] != list(ids):
            raise ValueError("%s was written for other specs or arms; choose another --out"
                             % results_path)
        result["repeats"] = repeats
    else:
        result = {"schema": 1, "mode": mode, "max_usd": max_usd, "repeats": repeats,
                  "specs": ids[0], "arms": ids[1], "limits": limits,
                  "estimate": estimate(specs, arms, repeats), "cells": [],
                  "spent_usd": 0.0, "stopped": None, "complete": False}
    done = {_key(c["spec"], c["arm"], c["repeat"]): c for c in result["cells"]}
    spent = sum(c["totals"]["cost_usd"] for c in result["cells"])
    total = len(specs) * len(arms) * repeats
    stores, ran, started, k = {}, 0, time.time(), 0
    for repeat in range(repeats):
        for spec in specs:
            for arm in arms:
                k += 1
                key = _key(spec["id"], arm["id"], repeat)
                if key in done:
                    continue
                worst = len(spec["prompts"]) * limits["max_usd"]
                if max_usd is not None and spent + worst > max_usd:
                    result["stopped"] = ("spend cap: $%.4f spent, the next cell may cost up to "
                                         "$%.2f, cap $%.2f" % (spent, worst, max_usd))
                    _store(results_path, result)
                    progress("STOP before cell %d/%d: %s" % (k, total, result["stopped"]))
                    return result
                if arm["lessons"] and arm["id"] not in stores:
                    stores[arm["id"]] = orc.LessonStore(outdir / "lessons" / (arm["id"] + ".json"))
                lessons = stores.get(arm["id"])
                rec = run_cell(spec, arm, generate, outdir / "work", mode=mode, limits=limits,
                               repeat=repeat, lessons=lessons)
                ran += 1
                result["cells"].append(rec)
                spent += rec["totals"]["cost_usd"]
                result["spent_usd"] = round(spent, 6)
                result["complete"] = len(result["cells"]) == total
                _store(results_path, result)
                per_cell = (time.time() - started) / ran
                states = "/".join(p["state"] for p in rec["prompts"])
                progress("[%d/%d] %s arm %s r%d: %s, %d model calls, $%.4f this cell, "
                         "$%.4f spent, ETA %s" % (
                             len(result["cells"]), total, spec["id"], arm["id"], repeat, states,
                             rec["totals"]["model_calls"], rec["totals"]["cost_usd"], spent,
                             _eta(per_cell, total - len(result["cells"]))))
    result["complete"] = len(result["cells"]) == total
    _store(results_path, result)
    return result


# -- table -----------------------------------------------------------------

def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _arm_summary(result, arm_id):
    cells = [c for c in result["cells"] if c["arm"] == arm_id]
    reqs = [c["requirements"] for c in cells if c.get("requirements")]
    met = sum(r.get("met", 0) for r in reqs)
    total = sum(r.get("total", 0) for r in reqs)
    n = len(cells)
    return {"cells": n, "completed": sum(1 for c in cells if c["completed"]),
            "met": met if reqs else None, "total": total if reqs else None,
            "calls": _mean([c["totals"]["model_calls"] for c in cells]),
            "tokens": _mean([c["totals"]["tokens"] for c in cells]),
            "cost": _mean([c["totals"]["cost_usd"] for c in cells]),
            "seconds": _mean([c["totals"]["seconds"] for c in cells]),
            "agent": _mean([c["functions_agent"] for c in cells]),
            "harness": _mean([c["functions_harness"] for c in cells]),
            "has_cells": n > 0}


def _frac(s):
    return "%d/%d" % (s["met"], s["total"]) if s["met"] is not None else "n/a"


CONTRASTS = [("Memory", "B", "A"), ("Lessons", "C", "B"),
             ("Kit", "K", "B"), ("Advice", "D", "K")]

# -- agreement between the harness's verdict and the hidden requirements ----
# A cell is "checked" when its hidden requirements were run (the requirements summary
# exists and has at least one check). For a checked cell, "all met" means met == total.
#   completion      checked cells with all hidden requirements met
#   claimed done    cells (all of them) whose last session ended in state "done"
#   false done      checked cells that ended "done" and are NOT all met (circularity)
#   missed done     checked cells that did NOT end "done" and ARE all met
#   false/missed done, proven   the two rates above, restricted to cells whose own
#                               verdict (qualification.verdict) was "proven"
# Each rate is numerator/denominator with a Wilson 95% interval. A zero denominator
# is reported as "n/a", never as 0.

Z95 = 1.959963984540054
AGREEMENT_HEAD = "Does the harness's own verdict agree with the hidden requirements?"


def wilson(k, n, z=Z95):
    """Wilson score interval (low, high) for k successes in n trials, or None when n is 0."""
    if n <= 0:
        return None
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5) / denom
    low = 0.0 if k == 0 else max(0.0, centre - half)     # exact at the ends, despite rounding
    high = 1.0 if k == n else min(1.0, centre + half)
    return (low, high)


def _rate(k, n):
    if n <= 0:
        return {"numerator": k, "denominator": n, "rate": "n/a", "wilson95": "n/a"}
    lo, hi = wilson(k, n)
    return {"numerator": k, "denominator": n, "rate": round(k / n, 6),
            "wilson95": [round(lo, 6), round(hi, 6)]}


def _all_met(summ):
    """True or False when the hidden requirements were checked, None when they were not."""
    if not summ or not summ.get("total"):
        return None
    return summ.get("met") == summ.get("total")


def _final_state(cell):
    prompts = cell.get("prompts") or []
    return prompts[-1].get("state") if prompts else None


def _verdict(cell):
    return (cell.get("own_verdict") or {}).get("verdict")


def _measures(cells):
    """Agreement measures for a list of cell records. Definitions, all over these cells:

    checked: a cell whose hidden requirements were run (a requirements summary with at least
        one check). The hidden rates below use only checked cells.
    completion: checked cells with every hidden requirement met (met == total).
    claimed_done: cells, checked or not, whose last session ended in state "done".
    false_done: checked cells that ended "done" and are NOT all met (the circularity measure).
    missed_done: checked cells that did NOT end "done" and ARE all met.
    false_done_proven, missed_done_proven: the two rates above restricted to cells whose own
        verdict (qualification.verdict) was "proven".

    Every rate is {"numerator", "denominator", "rate", "wilson95"}: rate = numerator /
    denominator, wilson95 the Wilson 95% score interval. A zero denominator gives "n/a".
    """
    known = [(c, _all_met(c.get("requirements"))) for c in cells]
    known = [(c, m) for c, m in known if m is not None]
    done = [(c, m) for c, m in known if _final_state(c) == "done"]
    not_done = [(c, m) for c, m in known if _final_state(c) != "done"]
    done_p = [(c, m) for c, m in done if _verdict(c) == "proven"]
    not_done_p = [(c, m) for c, m in not_done if _verdict(c) == "proven"]
    return {
        "cells": len(cells),
        "checked": len(known),
        "completion": _rate(sum(1 for _, m in known if m), len(known)),
        "claimed_done": _rate(sum(1 for c in cells if _final_state(c) == "done"), len(cells)),
        "false_done": _rate(sum(1 for _, m in done if not m), len(done)),
        "missed_done": _rate(sum(1 for _, m in not_done if m), len(not_done)),
        "false_done_proven": _rate(sum(1 for _, m in done_p if not m), len(done_p)),
        "missed_done_proven": _rate(sum(1 for _, m in not_done_p if m), len(not_done_p)),
    }


def agreement(result):
    """{"by_arm": {arm id: measures}, "all": measures} over the cells of RESULT. The measures
    and their exact definitions are described in _measures."""
    cells = result.get("cells", [])
    arms = result.get("arms") or sorted({c["arm"] for c in cells})
    return {"by_arm": {a: _measures([c for c in cells if c["arm"] == a]) for a in arms},
            "all": _measures(cells)}


def _fmt_rate(r):
    if r["denominator"] == 0:
        return "n/a (no cells)"
    lo, hi = r["wilson95"]
    return "%d/%d = %.2f [%.2f, %.2f]" % (r["numerator"], r["denominator"], r["rate"], lo, hi)


def _agreement_lines(result):
    """The second block of the table: the agreement measures per arm and over all arms."""
    block = agreement(result)
    names = [("completion", "completion (all hidden requirements met)"),
             ("claimed_done", "claimed done (last session ended done)"),
             ("false_done", "false done (done, not all met)"),
             ("missed_done", "missed done (not done, all met)"),
             ("false_done_proven", "false done, verdict proven"),
             ("missed_done_proven", "missed done, verdict proven")]
    lines = ["", AGREEMENT_HEAD,
             "Each rate is numerator/denominator = rate [Wilson 95% interval]; n/a means a "
             "denominator of 0. Completion, false done and missed done leave out cells whose "
             "hidden requirements were not checked."]
    if result.get("mode") != "live":
        lines.append("Scripted generator (mode %s): these numbers describe the scripted model "
                     "and say nothing about the live one." % result.get("mode"))
    for arm_id, m in list(block["by_arm"].items()) + [("all", block["all"])]:
        lines.append("arm %s: %d cells, %d with hidden requirements checked"
                     % (arm_id, m["cells"], m["checked"]))
        for key, label in names:
            lines.append("  %-44s %s" % (label, _fmt_rate(m[key])))
    return lines


def table(result):
    """Per-arm table plus the four contrasts as sentences, computed from the cells."""
    labels = {a["id"]: a["label"] for a in ARMS}
    summ = {a: _arm_summary(result, a) for a in result["arms"]}
    head = "%-4s %-44s %5s %9s %9s %7s %8s %8s %8s %6s %6s" % (
        "arm", "label", "cells", "completed", "met", "calls", "tokens", "USD", "seconds",
        "agent", "harn")
    rows = [head, "-" * len(head)]
    for a in result["arms"]:
        s = summ[a]
        rows.append("%-4s %-44s %5d %9s %9s %7.1f %8.0f %8.4f %8.1f %6.1f %6.1f" % (
            a, labels.get(a, ""), s["cells"],
            "%d/%d" % (s["completed"], s["cells"]) if s["cells"] else "n/a",
            _frac(s), s["calls"], s["tokens"], s["cost"], s["seconds"], s["agent"], s["harness"]))
    rows.append("")
    rows.append("Per cell means (one spec, one arm, one repeat). met = requirement checks met "
                "out of all checks; agent = functions the model wrote or composed; harn = "
                "functions the harness supplied (kit).")
    rows.append("")
    for name, x, y in CONTRASTS:
        if x not in summ or y not in summ:
            rows.append("%s (%s vs %s): not run, one of the arms is not in this result."
                        % (name, x, y))
            continue
        sx, sy = summ[x], summ[y]
        rows.append("%s (%s vs %s): completed %s vs %s, requirements met %s vs %s, mean model "
                    "calls %.1f vs %.1f, mean cost $%.2f vs $%.2f per cell."
                    % (name, x, y,
                       "%d/%d" % (sx["completed"], sx["cells"]),
                       "%d/%d" % (sy["completed"], sy["cells"]),
                       _frac(sx), _frac(sy), sx["calls"], sy["calls"], sx["cost"], sy["cost"]))
    rows += _agreement_lines(result)
    if result.get("repeats", 1) == 1:
        rows.append("")
        rows.append("Caution: each cell ran once. Differences between single runs are not "
                    "evidence of an effect; repeat with --repeats 3 or more before reading "
                    "any contrast.")
    return "\n".join(rows)


# -- command line ----------------------------------------------------------

def _select(items, csv, what):
    if not csv:
        return list(items)
    by_id = {x["id"]: x for x in items}
    want = [w.strip() for w in csv.split(",") if w.strip()]
    unknown = [w for w in want if w not in by_id]
    if unknown:
        raise ValueError("unknown %s id(s): %s" % (what, ", ".join(unknown)))
    return [by_id[w] for w in want]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--estimate", action="store_true", help="print the plan and costs, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="run every cell with the scripted generator (no model, no cost)")
    ap.add_argument("--live", action="store_true", help="run on the live paid model")
    ap.add_argument("--max-usd", type=float, default=None,
                    help="spend cap in USD; required with --live")
    ap.add_argument("--specs", default=None, help="comma-separated spec ids (default all)")
    ap.add_argument("--arms", default=None, help="comma-separated arm ids (default all)")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", default=None, help="output directory")
    args = ap.parse_args(argv)
    if sum([args.estimate, args.dry_run, args.live]) > 1:
        print("choose one of --estimate, --dry-run, --live", file=sys.stderr)
        return 2
    if args.live and (args.max_usd is None or args.max_usd <= 0):
        print("refusing to run live: a live run needs --live and --max-usd X, where X is a "
              "positive spend cap in USD", file=sys.stderr)
        return 2
    if args.repeats < 1:
        print("--repeats must be at least 1", file=sys.stderr)
        return 2
    try:
        specs = _select(SPECS, args.specs, "spec")
        arms = _select(ARMS, args.arms, "arm")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.estimate:
        print(_plan_text(specs, arms, args.repeats))
        return 0
    if not (args.dry_run or args.live):
        print("nothing to do: use --estimate, --dry-run, or --live --max-usd X", file=sys.stderr)
        return 2
    print(_plan_text(specs, arms, args.repeats))
    if args.dry_run:
        outdir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="autonomy-dry-"))
        generate, mode, cap = scripted_generate, "demo", None
        print("DRY RUN: scripted generator, no model call, output in %s" % outdir, flush=True)
    else:
        status = ag.live_status()
        if not status["available"]:
            print("live mode unavailable: %s" % status["reason"], file=sys.stderr)
            return 2
        outdir = Path(args.out) if args.out else DEFAULT_OUT
        generate, mode, cap = ag.live_generate, "live", args.max_usd
        print("LIVE RUN: spend cap $%.2f, output in %s" % (cap, outdir), flush=True)
    result = run_experiment(specs, arms, generate, outdir, mode=mode, max_usd=cap,
                            repeats=args.repeats, progress=lambda line: print(line, flush=True))
    print()
    print(table(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
