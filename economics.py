"""Lifecycle economics of the agent's projects, read from its session logs.

    uv run python economics.py                       # all live sessions
    uv run python economics.py --project pcrm-a82b   # one project
    uv run python economics.py --mode demo --json

What the numbers mean.

Exact, read straight from the logs:
  cost and tokens   every model_call is paired with the next model_reply from
                    the same lane; the reply's cost_usd and token counts are
                    summed. A call with no reply (a crash) is not counted.
  model calls       the number of such pairs.
  seconds           the summary's seconds, else the time from the first to the
                    last event of the session.
  outcome           the summary's outcome; a log without a summary uses its
                    done event (done means success). Anything else is "unknown".
  categories        each call label grouped as planning, first drafts, repairs,
                    screenshot checks or other (see LABEL_CATEGORY).

Estimated, because they depend on attribution or a counterfactual:
  creation and rework   a function's creation cost is the cost of the calls that
                        built it up to its first save anywhere in the logs; any
                        later call for it (rebuilds, repairs) is rework. When the
                        log records lanes, a call belongs to the function of its
                        lane. Otherwise it belongs to the function whose promoted
                        event follows it, counting only the calls after the
                        previous step or promoted event. Planning and other calls
                        in no function's window are in the totals only.
  reuse savings         estimate: zero-build sessions (flow cache or reuse) times
                        the average cost of build and plan sessions, minus what
                        the zero-build sessions actually cost. It assumes a
                        reused function would have cost an average build.
  break-even            estimate: the learning cost (all build and plan sessions)
                        against those savings, and how many more zero-build
                        sessions would repay the rest at the current average
                        saving per reuse.

Nothing else is modelled: no prices are recomputed and no durations are guessed.
"""
import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SESSIONS_DIR = ROOT / "artifacts" / "agent" / "sessions"
OUTCOMES = ("success", "failed", "cancelled", "error", "unknown")
CATEGORIES = ("planning", "first drafts", "repairs", "screenshot checks", "other")
LABEL_CATEGORY = {
    "plan": "planning", "extend-plan": "planning", "split": "planning", "visual-fix": "planning",
    "step": "first drafts", "sub-step": "first drafts", "final": "first drafts",
    "quick-reuse": "first drafts",
    "repair": "repairs", "rewrite": "repairs", "test-calls": "repairs",
    "property-tests": "repairs", "repair-call": "repairs",
    "visual-review": "screenshot checks",
}
FLOW_OF_ACTION = {"build": "build", "plan": "plan", "cache": "cache", "use": "reuse"}


def category(label):
    """Category of a call label. Any label containing "retry" counts as a repair.

    A numbered step ("step 1", "step 2") is a first draft.
    """
    if "retry" in label:
        return "repairs"
    base = label.split(" (")[0].strip()
    if base in LABEL_CATEGORY:
        return LABEL_CATEGORY[base]
    if re.fullmatch(r"step \d+", base):
        return "first drafts"
    return "other"


def _money(x):
    return round(float(x), 6)


def _share(part, whole):
    return round(part / whole, 3) if whole else 0.0


def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _num(x):
    return float(x) if _is_number(x) else 0.0


def _lane(event):
    value = event.get("lane")
    return value if isinstance(value, str) and value else None


def _first(events, kind):
    return next((e for e in events if e.get("kind") == kind), {})


def _last(events, kind):
    return next((e for e in reversed(events) if e.get("kind") == kind), {})


def read_events(path):
    """Event dicts of one JSONL log; unreadable or non-object lines are skipped."""
    events = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        pass
    return events


def _meta(events):
    goal = _first(events, "goal")
    summary = _last(events, "summary")
    done = _last(events, "done")
    action = _first(events, "decision").get("action")
    times = [e["t"] for e in events if _is_number(e.get("t"))]
    outcome = summary.get("outcome") or done.get("state")
    if outcome == "done":
        outcome = "success"
    flow = summary.get("flow") or (FLOW_OF_ACTION.get(action) if isinstance(action, str) else None)
    seconds = summary.get("seconds")
    if not _is_number(seconds):
        seconds = times[-1] - times[0] if times else 0.0
    start = goal.get("t") if _is_number(goal.get("t")) else (times[0] if times else 0.0)
    prompt = goal.get("prompt") if isinstance(goal.get("prompt"), str) else ""
    return {"project": goal.get("project") or "scratch", "mode": goal.get("mode"),
            "arm": goal.get("arm", "main"), "prompt": prompt, "t": float(start),
            "outcome": outcome if outcome in OUTCOMES else "unknown",
            "flow": flow if isinstance(flow, str) else None, "seconds": float(seconds)}


def _pair(events):
    """(calls, saves, marks): replies paired with their calls per lane, and the saves.

    ``marks`` are the positions of step and promoted events, which bound the
    no-lane attribution windows.
    """
    pending, calls, saves, marks = defaultdict(deque), [], [], []
    for pos, e in enumerate(events):
        kind = e.get("kind")
        if kind == "model_call":
            pending[_lane(e)].append((pos, e))
        elif kind == "model_reply":
            lane = _lane(e)
            if not pending.get(lane):
                heads = [(queue[0][0], key) for key, queue in pending.items() if queue]
                if not heads:
                    continue
                lane = min(heads)[1]
            _, call = pending[lane].popleft()
            label = call.get("label") if isinstance(call.get("label"), str) else "?"
            calls.append({"pos": pos, "lane": _lane(call), "label": label,
                          "cat": category(label), "retry": "retry" in label,
                          "cost": _num(e.get("cost_usd")),
                          "input_tokens": int(_num(e.get("input_tokens"))),
                          "output_tokens": int(_num(e.get("output_tokens")))})
        elif kind in ("step", "promoted"):
            marks.append(pos)
            if kind == "promoted":
                name = e.get("name") if isinstance(e.get("name"), str) and e.get("name") else (_lane(e) or "?")
                saves.append({"pos": pos, "name": name, "key": _lane(e) or name,
                              "lane_logged": _lane(e) is not None})
    return calls, saves, marks


def _session(stem, events, meta):
    """One session: its meta, its calls, and a timeline of calls and saves."""
    calls, saves, marks = _pair(events)
    if any(s["lane_logged"] for s in saves):
        attributed = [(c["pos"], c["lane"], c) for c in calls if c["lane"] is not None]
    else:
        attributed = []
        for s in saves:
            lo = max((m for m in marks if m < s["pos"]), default=-1)
            attributed += [(c["pos"], s["key"], c) for c in calls if lo < c["pos"] < s["pos"]]
    timeline = [(pos, "call", key, c) for pos, key, c in attributed]
    timeline += [(s["pos"], "save", s["key"], s["name"]) for s in saves]
    timeline.sort(key=lambda item: item[0])
    return dict(meta, id=stem, calls=calls, cost=sum(c["cost"] for c in calls), timeline=timeline)


def _ledger(sessions):
    """Per-function creation and rework, walking the sessions oldest first.

    Returns ({name: function row}, total number of saves).
    """
    saved, name_of = set(), {}
    creation, rework, repairs, builds = Counter(), Counter(), Counter(), Counter()
    for s in sessions:
        for _, kind, key, payload in s["timeline"]:
            if kind == "save":
                saved.add(key)
                name_of[key] = payload
                builds[key] += 1
            else:
                (rework if key in saved else creation)[key] += payload["cost"]
                if payload["cat"] == "repairs":
                    repairs[key] += 1
    functions = {}
    for key, n in builds.items():
        name = name_of[key]
        row = functions.setdefault(name, {"name": name, "builds": 0, "creation": 0.0,
                                          "rework": 0.0, "repair_calls": 0})
        row["builds"] += n
        row["creation"] += creation[key]
        row["rework"] += rework[key]
        row["repair_calls"] += repairs[key]
    return functions, sum(builds.values())


def project_economics(project=None, mode="live", sessions_dir=None):
    """Lifecycle economics of one project (or of all, when PROJECT is None): a plain dict.

    Only sessions of arm "main" count. MODE filters on the goal's mode ("live",
    "demo", or None for both). SESSIONS_DIR defaults to artifacts/agent/sessions.
    """
    directory = Path(sessions_dir) if sessions_dir is not None else SESSIONS_DIR
    try:
        paths = sorted(directory.glob("*.jsonl"))
    except OSError:
        paths = []
    sessions = []
    for path in paths:
        events = read_events(path)
        meta = _meta(events)
        if meta["arm"] != "main" or (mode and meta["mode"] != mode) or \
                (project and meta["project"] != project):
            continue
        sessions.append(_session(path.stem, events, meta))
    sessions.sort(key=lambda s: (s["t"], s["id"]))
    return _report(sessions, project, mode)


def _report(sessions, project, mode):
    functions, saves = _ledger(sessions)
    calls = [c for s in sessions for c in s["calls"]]
    total_cost = sum(s["cost"] for s in sessions)

    by_kind = {name: {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "model_calls": 0}
               for name in CATEGORIES}
    for c in calls:
        row = by_kind[c["cat"]]
        row["cost_usd"] += c["cost"]
        row["input_tokens"] += c["input_tokens"]
        row["output_tokens"] += c["output_tokens"]
        row["model_calls"] += 1
    for row in by_kind.values():
        row["tokens"] = row["input_tokens"] + row["output_tokens"]
        row["cost_usd"] = _money(row["cost_usd"])

    function_rows = sorted(functions.values(),
                           key=lambda f: (-(f["creation"] + f["rework"]), f["name"]))
    rework_total = sum(f["rework"] for f in functions.values())
    rebuilt = sorted(f["name"] for f in functions.values() if f["builds"] > 1)

    outcomes = {name: 0 for name in OUTCOMES}
    for s in sessions:
        outcomes[s["outcome"]] += 1

    repair_cost = sum(c["cost"] for c in calls if c["cat"] == "repairs")
    reuse_sessions = [s for s in sessions if s["flow"] in ("cache", "reuse")]
    build_sessions = [s for s in sessions if s["flow"] in ("build", "plan")]
    their_cost = sum(s["cost"] for s in reuse_sessions)
    learning = sum(s["cost"] for s in build_sessions)
    avg_build = learning / len(build_sessions) if build_sessions else 0.0
    savings = max(0.0, len(reuse_sessions) * avg_build - their_cost)
    reached = learning > 0 and savings >= learning
    sessions_to_go = None
    if reached:
        sessions_to_go = 0
    elif reuse_sessions:
        per_reuse = avg_build - their_cost / len(reuse_sessions)
        if per_reuse > 0:
            sessions_to_go = math.ceil((learning - savings) / per_reuse)

    return {
        "scope": {"project": project or "all", "mode": mode or "all"},
        "sessions": len(sessions),
        "outcomes": outcomes,
        "total": {"cost_usd": _money(total_cost),
                  "input_tokens": sum(c["input_tokens"] for c in calls),
                  "output_tokens": sum(c["output_tokens"] for c in calls),
                  "model_calls": len(calls),
                  "seconds": round(sum(s["seconds"] for s in sessions), 1)},
        "by_kind": by_kind,
        "functions": [{"name": f["name"], "builds": f["builds"],
                       "creation_cost_usd": _money(f["creation"]),
                       "rework_cost_usd": _money(f["rework"]),
                       "total_cost_usd": _money(f["creation"] + f["rework"]),
                       "repair_calls": f["repair_calls"]} for f in function_rows],
        "churn": {"functions_saved": len(functions), "saves": saves, "rebuilt": rebuilt,
                  "rework_share": _share(rework_total, total_cost)},
        "waste": {"failed_session_cost_usd": _money(sum(s["cost"] for s in sessions
                                                        if s["outcome"] != "success")),
                  "repair_cost_usd": _money(repair_cost),
                  "unreadable_reply_calls": sum(1 for c in calls if c["retry"])},
        "reuse": {"zero_build_sessions": len(reuse_sessions),
                  "their_cost_usd": _money(their_cost),
                  "avg_build_session_cost_usd": _money(avg_build),
                  "estimated_savings_usd": _money(savings),
                  "note": "Estimate: each zero-build session is assumed to have cost as much "
                          "as the average build or plan session, so the saving is that average "
                          "times the count, minus what those sessions actually cost."},
        "break_even": {"learning_cost_usd": _money(learning),
                       "savings_so_far_usd": _money(savings),
                       "reached": reached,
                       "sessions_to_break_even": sessions_to_go},
        "trend": [{"id": s["id"], "cost_usd": _money(s["cost"]), "model_calls": len(s["calls"]),
                   "seconds": round(s["seconds"], 1), "outcome": s["outcome"],
                   "prompt": s["prompt"][:60]} for s in sessions[-10:]],
    }


def render(report):
    """The report as readable text lines (a list of str), most important first."""
    scope = report["scope"]
    who = "All projects" if scope["project"] == "all" else "Project " + scope["project"]
    modes = "all modes" if scope["mode"] == "all" else scope["mode"] + " mode"
    if not report["sessions"]:
        return ["%s (%s): no sessions." % (who, modes)]
    total, outcomes = report["total"], report["outcomes"]
    spent = total["cost_usd"]
    lines = ["%s (%s): $%.4f spent over %d session(s), %d model calls, %d tokens, %.1f minutes."
             % (who, modes, spent, report["sessions"], total["model_calls"],
                total["input_tokens"] + total["output_tokens"], total["seconds"] / 60),
             "It bought %d saved function(s) in %d successful session(s). Outcomes: %s."
             % (report["churn"]["functions_saved"], outcomes["success"],
                ", ".join("%s %d" % (k, outcomes[k]) for k in OUTCOMES)),
             "Where the spend went: " + ", ".join(
                 "%s %d%%" % (name, round(100 * _share(row["cost_usd"], spent)))
                 for name, row in report["by_kind"].items()) + "."]
    lines += ["", "Most expensive functions (total cost, of which rework):"]
    lines += ["  %-24s $%.4f total, $%.4f rework, saved %d time(s), %d repair call(s)"
              % (f["name"], f["total_cost_usd"], f["rework_cost_usd"], f["builds"],
                 f["repair_calls"]) for f in report["functions"][:5]] or ["  none"]
    churn = report["churn"]
    top = max(report["functions"], key=lambda f: f["builds"], default=None)
    pct = round(100 * churn["rework_share"])
    if top and top["builds"] > 1:
        lines += ["", "%d%% of the spend rebuilt functions that already existed: %s was saved %d times."
                  % (pct, top["name"], top["builds"])]
    else:
        lines += ["", "%d%% of the spend rebuilt functions that already existed; no function was saved twice."
                  % pct]
    waste = report["waste"]
    lines += ["Waste: $%.4f went to sessions that did not succeed, $%.4f to repair calls, and "
              "%d model call(s) needed a retry after an unreadable reply."
              % (waste["failed_session_cost_usd"], waste["repair_cost_usd"],
                 waste["unreadable_reply_calls"])]
    reuse = report["reuse"]
    lines += ["Reuse (estimate): %d zero-build session(s) cost $%.4f; at the $%.4f average "
              "build-session cost, that is an estimated $%.4f saved."
              % (reuse["zero_build_sessions"], reuse["their_cost_usd"],
                 reuse["avg_build_session_cost_usd"], reuse["estimated_savings_usd"])]
    be = report["break_even"]
    head = "Break-even (estimate): learning cost $%.4f, estimated savings $%.4f so far" \
        % (be["learning_cost_usd"], be["savings_so_far_usd"])
    if be["reached"]:
        lines.append(head + "; break-even is reached.")
    elif be["sessions_to_break_even"] is None:
        lines.append(head + "; no average saving per reuse yet, so no break-even estimate.")
    else:
        lines.append(head + "; about %d more zero-build session(s) needed."
                     % be["sessions_to_break_even"])
    if report["trend"]:
        lines += ["", "Cost of the last %d session(s), oldest first: %s."
                  % (len(report["trend"]),
                     ", ".join("$%.4f" % r["cost_usd"] for r in report["trend"]))]
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", default=None, help="only this project's sessions")
    ap.add_argument("--mode", choices=("live", "demo"), default="live")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    report = project_economics(args.project, args.mode)
    print(json.dumps(report, indent=2) if args.json else "\n".join(render(report)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
