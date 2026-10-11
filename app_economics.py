"""Does building comparable applications get cheaper as the harness gains experience?

    uv run python app_economics.py --dry-run                 # whole pipeline, offline
    uv run python app_economics.py --estimate                # plan and cost only
    uv run python app_economics.py --analyse DIR             # re-analyse saved results
    uv run python app_economics.py --live --max-usd 30       # the paid run (owner only)

Design. Five command-line apps of the same size and shape (notes, contacts,
inventory, expenses, reading) are each built at every position of a sequence.
A cyclic Latin square of orders puts every app once at every position (5
orders; --orders 10 adds the reversed square). Two conditions per order:

  warm  one experience store (a LessonStore) is carried through the order, so
        each build sees the advice that earlier builds of the order produced;
        the store starts empty at the first position of each order;
  cold  every build starts with a fresh, empty store.

The arm is D of autonomy_experiment (kit, advice, lessons, memory on). That is
the product's default: Session defaults use_kit and use_advice to True, and
the live server passes its shared LessonStore to every session.

What carries over, read from the code. Within one build, the registry of saved
functions carries from prompt to prompt. Across builds, run_cell gives each
cell its own registry, so only the LessonStore carries over. A LessonStore
changes only the text of the user message: at most two hand-written hints,
chosen from a fixed table (oracle.HINTS) by how often a slip recurred. Kit
helpers and repair rules are fixed code. So the warm condition isolates the
lessons, and nothing else that the live server shares between builds of one
project (saved tools, earlier goals) is in this experiment.

One app build is one cell: its three prompts run in order in one run_cell. Success
for a build means every hidden requirement line of its spec is met.

Analysis. Cost is the cell's cost_usd. Using only orders that have both
conditions at every position, it fits an ordinary least squares slope of cost
on position for each condition, and the warm-minus-cold difference at each
position and overall. Intervals are 95% percentile bootstrap intervals,
resampling orders with replacement under a fixed seed.

Verdict rule (applied in this order):
  "cost rises" if the warm slope interval lies entirely above zero;
  "cost falls with experience" only if ALL of these hold:
      the warm slope interval lies entirely below zero,
      the warm-minus-cold difference at the last position lies entirely below zero,
      warm success is not lower than cold success by more than 10 points;
  "no evidence that cost falls" otherwise.
Cost per successful build is reported beside every figure, because a cheap
failed build is not a saving.
"""
import argparse
import json
import math
import random
import sys
import tempfile
import time
from pathlib import Path

import agent_session as ag
import autonomy_experiment as ae
import oracle as orc

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "artifacts" / "agent" / "app-economics"
ARM_ID = "D"                       # the product's default configuration (see module docstring)
CONDITIONS = ("warm", "cold")
REPS = 2000                        # bootstrap resamples
SEED = 20261010                    # fixed seed: the same results always give the same intervals
SUCCESS_TOLERANCE = 0.10           # warm success may trail cold by at most this share
FALLS = "cost falls with experience"
RISES = "cost rises"
NO_EVIDENCE = "no evidence that cost falls"
GHOST = "Zed"                      # a name that is never added, for the remove error check

# Same size and shape, different subject: each app stores, lists, removes one, and counts.
THEMES = [
    {"id": "notes", "title": "Notes list", "noun": "notes", "one": "note",
     "label": "notes", "names": ("Buy milk", "Call mum")},
    {"id": "contacts", "title": "Contact book", "noun": "contacts", "one": "contact",
     "label": "contacts", "names": ("Ann Lee", "Ben Cho")},
    {"id": "inventory", "title": "Inventory count", "noun": "stock items", "one": "stock item",
     "label": "items", "names": ("Bolts", "Nails")},
    {"id": "expenses", "title": "Expense log", "noun": "expenses", "one": "expense",
     "label": "expenses", "names": ("Rent", "Coffee")},
    {"id": "reading", "title": "Reading list", "noun": "books", "one": "book",
     "label": "books", "names": ("Dune", "Emma")},
]


def _spec(theme):
    """One app spec in autonomy_experiment's format: three prompts, five requirement units."""
    a, b = theme["names"]
    label, one, noun = theme["label"], theme["one"], theme["noun"]
    prompts = [
        "Build a command-line app that keeps a list of %s. Each command is one line. "
        "add NAME stores a %s called NAME. list prints every name, one per line, in the order "
        "added. count prints the line %s: followed by how many there are, for example after "
        "add %s and add %s, count prints %s: 2. The list is saved between runs."
        % (noun, one, label, a, b, label),
        "Also make remove NAME delete the %s called NAME, so that list no longer shows it. "
        "Keep everything else as it is." % one,
        "Also reject bad input. remove of an unknown name prints error: not found and changes "
        "nothing. A command that is not one of these prints a line starting with usage: "
        "followed by the commands. Keep everything else as it is.",
    ]
    requirements = "\n".join([
        "scenario: add and list",
        '  run "add %s" prints "%s"' % (a, a),
        '  run "add %s" prints "%s"' % (b, b),
        '  run "list" prints "%s"' % a,
        '  run "list" prints "%s"' % b,
        "scenario: remove",
        '  run "add %s" prints "%s"' % (a, a),
        '  run "add %s" prints "%s"' % (b, b),
        '  run "remove %s" then run "list" does not print "%s"' % (a, a),
        '  run "list" prints "%s"' % b,
        "scenario: count",
        '  run "add %s" prints "%s"' % (a, a),
        '  run "add %s" prints "%s"' % (b, b),
        '  run "count" prints "%s: 2"' % label,
        'run "remove %s" prints "error: not found"' % GHOST,
        'run "frobnicate" prints "usage:"',
    ])
    return {"id": theme["id"], "title": theme["title"], "kind": "cli",
            "prompts": prompts, "requirements": requirements}


SPECS = [_spec(t) for t in THEMES]
SPEC_IDS = [s["id"] for s in SPECS]


# -- orders and plan ---------------------------------------------------------

def latin_square(ids):
    """Cyclic Latin square: row k starts with ids[k]; each id appears once in each column."""
    n = len(ids)
    return [[ids[(k + p) % n] for p in range(n)] for k in range(n)]


def orders(ids=SPEC_IDS, count=None):
    """Order rows (lists of spec ids). COUNT must be a multiple of len(ids); each block of
    len(ids) rows is a Latin square, forward first, then reversed, and so on."""
    n = len(ids)
    count = n if count is None else count
    if count < n or count % n:
        raise ValueError("orders must be a positive multiple of %d, got %d" % (n, count))
    rows = []
    for block in range(count // n):
        if block % 2 == 0:
            rows += latin_square(ids)
        else:
            rows += [[ids[(k - p) % n] for p in range(n)] for k in range(n)]
    return rows


def plan(specs, order_rows):
    """Cells: one per (order, position, condition). Raises if a row is not a permutation."""
    ids = [s["id"] for s in specs]
    cells = []
    for o, row in enumerate(order_rows):
        if sorted(row) != sorted(ids):
            raise ValueError("order %d is not a permutation of the specs" % o)
        for p, sid in enumerate(row, 1):
            for cond in CONDITIONS:
                cells.append({"order": o, "position": p, "spec": sid, "condition": cond})
    return cells


def estimate(specs, cells, per_build_typical=None, per_build_worst=None):
    """Counts and dollar figures for a plan. An app build is one cell; a cell runs each of
    its spec's prompts as one session, so the per-session figures from autonomy_experiment
    are multiplied by the number of prompt sessions."""
    typical_each = ae.TYPICAL_BUILD_USD if per_build_typical is None else per_build_typical
    worst_each = ae.WORST_BUILD_USD if per_build_worst is None else per_build_worst
    prompts = {s["id"]: len(s["prompts"]) for s in specs}
    sessions = sum(prompts[c["spec"]] for c in cells)
    return {"cells": len(cells), "app_builds": len(cells), "prompt_sessions": sessions,
            "typical_usd": round(sessions * typical_each, 2),
            "worst_case_usd": round(sessions * worst_each, 2),
            "per_session_typical_usd": typical_each, "per_session_worst_usd": worst_each}


# -- statistics --------------------------------------------------------------

def ols_slope(xs, ys):
    """Least-squares slope of ys on xs, or None when it is not defined."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx, 12)


def _quantile(sorted_vals, q):
    """Linear-interpolated quantile of an already sorted, non-empty list."""
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _interval(samples):
    """95% percentile interval of bootstrap samples, or (None, None) when there are too few."""
    vals = sorted(v for v in samples if v is not None)
    if len(vals) < 2:
        return None, None
    return _quantile(vals, 0.025), _quantile(vals, 0.975)


def bootstrap_slope(rows_by_order, seed=SEED, reps=REPS):
    """rows_by_order: one list of (position, cost) pairs per complete order.

    Returns {"slope", "low", "high"}. Orders are resampled with replacement, so an order
    drawn twice contributes its rows twice."""
    rng = random.Random(seed)
    k = len(rows_by_order)
    point = ols_slope([p for rows in rows_by_order for p, _ in rows],
                      [y for rows in rows_by_order for _, y in rows]) if k else None
    samples = []
    for _ in range(reps if k >= 2 else 0):
        pick = [rows_by_order[rng.randrange(k)] for _ in range(k)]
        samples.append(ols_slope([p for rows in pick for p, _ in rows],
                                 [y for rows in pick for _, y in rows]))
    low, high = _interval(samples)
    return {"slope": point, "low": low, "high": high}


def _position_means(by_order, positions):
    """Per position, the mean over the given orders of their values at that position."""
    out = []
    for p in positions:
        vals = [row[p] for row in by_order]
        out.append(sum(vals) / len(vals) if vals else None)
    return out


def bootstrap_difference(warm_by_order, cold_by_order, positions, seed=SEED, reps=REPS):
    """Warm minus cold, per position and overall. Each argument is a list (one entry per
    complete order) of {position: cost} dicts; the two lists are paired by order.

    Returns {"by_position": [{"position", "point", "low", "high"}], "overall": {...}}."""
    k = len(warm_by_order)

    def diffs(indices):
        w = _position_means([warm_by_order[i] for i in indices], positions)
        c = _position_means([cold_by_order[i] for i in indices], positions)
        per = [None if a is None or b is None else round(a - b, 12) for a, b in zip(w, c)]
        known = [d for d in per if d is not None]
        overall = sum(known) / len(known) if known else None
        return per, overall

    point_per, point_all = diffs(list(range(k))) if k else ([None] * len(positions), None)
    rng = random.Random(seed)
    per_samples = [[] for _ in positions]
    all_samples = []
    for _ in range(reps if k >= 2 else 0):
        per, overall = diffs([rng.randrange(k) for _ in range(k)])
        for i, d in enumerate(per):
            per_samples[i].append(d)
        all_samples.append(overall)
    by_position = []
    for i, p in enumerate(positions):
        low, high = _interval(per_samples[i])
        by_position.append({"position": p, "point": point_per[i], "low": low, "high": high})
    low, high = _interval(all_samples)
    return {"by_position": by_position,
            "overall": {"point": point_all, "low": low, "high": high}}


# -- reading results ---------------------------------------------------------

def _metrics(cell):
    """The numbers one cell contributes. success is the hidden requirements all met."""
    rec = cell["record"]
    totals = rec.get("totals") or {}
    req = rec.get("requirements") or {}
    total = req.get("total") or 0
    return {"cost": float(totals.get("cost_usd") or 0.0),
            "calls": float(totals.get("model_calls") or 0),
            "tokens": float(totals.get("tokens") or 0),
            "seconds": float(totals.get("seconds") or 0.0),
            "success": bool(total) and req.get("met") == total}


def _share(part, whole):
    return part / whole if whole else None


def _cell_table(cells):
    """{(order, position, condition): metrics} for the cells given."""
    return {(c["order"], c["position"], c["condition"]): _metrics(c) for c in cells}


def _summary(rows):
    """Descriptive figures over a list of metrics dicts (one per cell)."""
    n = len(rows)
    if not n:
        return {"cells": 0, "successes": 0, "success_share": None, "mean_calls": None,
                "mean_tokens": None, "mean_cost": None, "mean_seconds": None,
                "cost_per_success": None, "total_cost": 0.0}
    successes = sum(1 for r in rows if r["success"])
    total_cost = sum(r["cost"] for r in rows)
    return {"cells": n, "successes": successes,
            "success_share": successes / n,
            "mean_calls": sum(r["calls"] for r in rows) / n,
            "mean_tokens": sum(r["tokens"] for r in rows) / n,
            "mean_cost": total_cost / n,
            "mean_seconds": sum(r["seconds"] for r in rows) / n,
            "cost_per_success": (total_cost / successes) if successes else None,
            "total_cost": total_cost}


def analyse(results, seed=SEED, reps=REPS):
    """Per condition and position: descriptive figures; slopes and differences with bootstrap
    intervals over the orders that are complete (both conditions at every position); the
    verdict and the rule that produced it. A plain dict, rendered by table()."""
    cells = results.get("cells") or []
    positions = sorted({c["position"] for c in cells})
    table = _cell_table(cells)
    all_orders = sorted({c["order"] for c in cells})
    complete = [o for o in all_orders
                if all((o, p, c) in table for p in positions for c in CONDITIONS)]

    per_position = {}
    for cond in CONDITIONS:
        per_position[cond] = []
        for p in positions:
            rows = [table[k] for k in table if k[1] == p and k[2] == cond]
            per_position[cond].append(dict(_summary(rows), position=p))
    overall = {cond: _summary([m for k, m in table.items() if k[2] == cond])
               for cond in CONDITIONS}

    slopes, rows_by = {}, {}
    for cond in CONDITIONS:
        rows_by[cond] = [[(p, table[(o, p, cond)]["cost"]) for p in positions] for o in complete]
        slopes[cond] = bootstrap_slope(rows_by[cond], seed, reps)
    diff = bootstrap_difference(
        [{p: table[(o, p, "warm")]["cost"] for p in positions} for o in complete],
        [{p: table[(o, p, "cold")]["cost"] for p in positions} for o in complete],
        positions, seed, reps)

    gap = None
    if overall["warm"]["success_share"] is not None and overall["cold"]["success_share"] is not None:
        gap = overall["warm"]["success_share"] - overall["cold"]["success_share"]
    analysis = {"positions": positions, "orders": {"total": len(all_orders),
                                                   "complete": len(complete),
                                                   "complete_ids": complete},
                "per_position": per_position, "overall": overall,
                "slopes": slopes, "difference": diff,
                "success_gap": gap, "seed": seed, "reps": reps}
    analysis["verdict"], analysis["rule"] = verdict(analysis)
    return analysis


def _entirely(interval, below):
    """True when the interval lies entirely below zero (below=True) or above it."""
    low, high = interval.get("low"), interval.get("high")
    if low is None or high is None:
        return False
    return high < 0 if below else low > 0


def verdict(analysis):
    """(verdict, rule_text) for an analysis dict, following the rule in the module docstring."""
    warm = analysis["slopes"]["warm"]
    last = analysis["difference"]["by_position"][-1] if analysis["difference"]["by_position"] else {}
    gap = analysis["success_gap"]
    if analysis["orders"]["complete"] < 2:
        return NO_EVIDENCE, ("fewer than two complete orders, so no interval exists; "
                             "no conclusion is drawn")
    detail = ("warm slope %s, interval [%s, %s]; warm minus cold at position %s %s, interval "
              "[%s, %s]; warm success minus cold success %s"
              % (_fmt(warm["slope"]), _fmt(warm["low"]), _fmt(warm["high"]),
                 last.get("position"), _fmt(last.get("point")), _fmt(last.get("low")),
                 _fmt(last.get("high")), _fmt(gap, pct=True)))
    if _entirely(warm, below=False):
        return RISES, "warm slope interval entirely above zero; " + detail
    falls_slope = _entirely(warm, below=True)
    falls_last = _entirely(last, below=True)
    success_ok = gap is None or gap >= -SUCCESS_TOLERANCE
    if falls_slope and falls_last and success_ok:
        return FALLS, ("warm slope interval entirely below zero, warm minus cold below zero at "
                       "the last position, and warm success not lower than cold by more than "
                       "%d points; %s" % (round(SUCCESS_TOLERANCE * 100), detail))
    failed = []
    if not falls_slope:
        failed.append("the warm slope interval is not entirely below zero")
    if not falls_last:
        failed.append("the warm-minus-cold difference at the last position is not entirely below zero")
    if not success_ok:
        failed.append("warm success is lower than cold success by more than %d points"
                      % round(SUCCESS_TOLERANCE * 100))
    return NO_EVIDENCE, "; ".join(failed) + "; " + detail


def _fmt(x, pct=False):
    if x is None:
        return "n/a"
    return ("%+.1f%%" % (100 * x)) if pct else ("%.4f" % x)


# -- running -----------------------------------------------------------------

def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    tmp.replace(path)


def _cell_key(cell):
    return (cell["order"], cell["position"], cell["condition"])


def _clock(seconds):
    s = int(seconds)
    return "%dm%02ds" % (s // 60, s % 60)


def run(specs, order_rows, generate, outdir, mode="demo", max_usd=None, progress=print,
        limits=None):
    """Run every cell not yet in OUTDIR/results.json, in plan order. Stops cleanly before a
    cell whose worst case would take the spend past MAX_USD. Every finished cell is written
    to results.json at once, and every progress line is also appended to progress.log.

    The warm store of an order is a LessonStore under OUTDIR/lessons; it is deleted when
    the order's first position starts, so each order begins with nothing learned."""
    outdir = Path(outdir)
    limits = dict(ae.DEFAULT_LIMITS, **(limits or {}))
    results_path = outdir / "results.json"
    log_path = outdir / "progress.log"
    ids = [s["id"] for s in specs]
    cells = plan(specs, order_rows)
    if results_path.exists():
        result = json.loads(results_path.read_text(encoding="utf-8"))
        if result.get("specs") != ids or result.get("orders") != order_rows:
            raise ValueError("%s was written for other specs or orders; choose another --out"
                             % results_path)
    else:
        result = {"schema": 1, "experiment": "app-economics", "arm": ARM_ID, "mode": mode,
                  "max_usd": max_usd, "limits": limits, "specs": ids, "orders": order_rows,
                  "estimate": estimate(specs, cells), "cells": [], "spent_usd": 0.0,
                  "stopped": None, "complete": False}
    done = {_cell_key(c) for c in result["cells"]}
    spent = sum(c["record"]["totals"]["cost_usd"] for c in result["cells"])
    arm = next(a for a in ae.ARMS if a["id"] == ARM_ID)
    by_id = {s["id"]: s for s in specs}
    total, ran, started = len(cells), 0, time.time()

    def say(line):
        progress(line)
        outdir.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    for cell in cells:
        key = _cell_key(cell)
        if key in done:
            continue
        spec = by_id[cell["spec"]]
        worst = len(spec["prompts"]) * limits["max_usd"]
        if max_usd is not None and spent + worst > max_usd:
            result["stopped"] = ("spend cap: $%.4f spent, the next cell may cost up to $%.2f, "
                                 "cap $%.2f" % (spent, worst, max_usd))
            _write_json(results_path, result)
            say("STOP before cell %d/%d: %s" % (len(result["cells"]) + 1, total, result["stopped"]))
            return result
        lessons = None
        if cell["condition"] == "warm":
            store_path = outdir / "lessons" / ("order-%02d.json" % cell["order"])
            if cell["position"] == 1 and store_path.exists():
                store_path.unlink()
            lessons = orc.LessonStore(store_path)
        workdir = outdir / "work" / ("o%02d-p%d-%s" % key)
        rec = ae.run_cell(spec, arm, generate, workdir, mode=mode, limits=limits, repeat=0,
                          lessons=lessons)
        ran += 1
        result["cells"].append({"order": cell["order"], "position": cell["position"],
                                "spec": cell["spec"], "condition": cell["condition"],
                                "record": rec})
        spent += rec["totals"]["cost_usd"]
        result["spent_usd"] = round(spent, 6)
        result["complete"] = len(result["cells"]) == total
        _write_json(results_path, result)
        elapsed = time.time() - started
        remaining = total - len(result["cells"])
        eta = _clock(elapsed / ran * remaining) if remaining else "0m00s"
        say("[%d/%d] order %d position %d %s %s: %s, %d model calls, $%.4f this build, "
            "$%.4f spent, elapsed %s, ETA %s" % (
                len(result["cells"]), total, cell["order"], cell["position"], cell["condition"],
                cell["spec"], "/".join(p["state"] for p in rec["prompts"]),
                rec["totals"]["model_calls"], rec["totals"]["cost_usd"], spent,
                _clock(elapsed), eta))
    result["complete"] = len(result["cells"]) == total
    _write_json(results_path, result)
    return result


# -- report ------------------------------------------------------------------

RULE_TEXT = (
    "Rule. Cost is the cost of one app build (cell). Only orders with both conditions at "
    "every position are used for the slopes and differences. Intervals are 95%% percentile "
    "bootstrap intervals over orders. 'cost rises' if the warm slope interval is entirely "
    "above zero. 'cost falls with experience' only if the warm slope interval is entirely "
    "below zero, the warm-minus-cold difference at the last position is entirely below "
    "zero, and warm success is not lower than cold success by more than %d points. "
    "Otherwise 'no evidence that cost falls'." % round(SUCCESS_TOLERANCE * 100))


def _row(cond, pos, s):
    def num(x, fmt):
        return fmt % x if x is not None else "n/a"
    return "%-6s %-4s %3d %8s %7s %8s %9s %12s %8s" % (
        cond, pos, s["cells"],
        num(None if s["success_share"] is None else 100 * s["success_share"], "%.0f%%"),
        num(s["mean_calls"], "%.1f"), num(s["mean_tokens"], "%.0f"),
        num(s["mean_cost"], "$%.4f"),
        num(s["cost_per_success"], "$%.4f") if s["cost_per_success"] is not None else "none ok",
        num(s["mean_seconds"], "%.1f"))


def table(analysis):
    """A plain-text report: the figures, the intervals, the verdict and the rule behind it."""
    o = analysis["orders"]
    head = "%-6s %-4s %3s %8s %7s %8s %9s %12s %8s" % (
        "cond", "pos", "n", "success", "calls", "tokens", "USD/build", "USD/success", "seconds")
    lines = ["App economics: %d order(s), %d complete, positions %s, arm %s, seed %d, "
             "%d bootstrap resamples." % (o["total"], o["complete"],
                                          ",".join(str(p) for p in analysis["positions"]),
                                          ARM_ID, analysis["seed"], analysis["reps"]),
             "", head, "-" * len(head)]
    for cond in CONDITIONS:
        for s in analysis["per_position"][cond]:
            lines.append(_row(cond, s["position"], s))
        lines.append(_row(cond, "all", analysis["overall"][cond]))
        lines.append("")
    lines.append("Slope of cost against position, USD per position, 95% bootstrap interval:")
    for cond in CONDITIONS:
        sl = analysis["slopes"][cond]
        lines.append("  %-5s %s  [%s, %s]" % (cond, _fmt(sl["slope"]), _fmt(sl["low"]),
                                             _fmt(sl["high"])))
    lines.append("Warm minus cold, USD per build, 95% bootstrap interval:")
    for d in analysis["difference"]["by_position"]:
        lines.append("  position %d  %s  [%s, %s]" % (d["position"], _fmt(d["point"]),
                                                     _fmt(d["low"]), _fmt(d["high"])))
    ov = analysis["difference"]["overall"]
    lines.append("  overall     %s  [%s, %s]" % (_fmt(ov["point"]), _fmt(ov["low"]),
                                                _fmt(ov["high"])))
    gap = analysis["success_gap"]
    def share(x):
        return "n/a" if x is None else "%.0f%%" % (100 * x)
    lines.append("Success: warm %s, cold %s, warm minus cold %s." % (
        share(analysis["overall"]["warm"]["success_share"]),
        share(analysis["overall"]["cold"]["success_share"]), _fmt(gap, pct=True)))
    lines += ["", "VERDICT: %s" % analysis["verdict"], RULE_TEXT,
              "Applied: %s." % analysis["rule"]]
    if o["complete"] < 5:
        lines.append("Caution: only %d complete order(s); the intervals are wide or absent. "
                     "The study needs all 5 orders." % o["complete"])
    return "\n".join(lines)


# -- command line ------------------------------------------------------------

def _plan_text(order_count, est):
    lines = ["Plan: %d specs, %d orders, %d cells (app builds), %d prompt sessions."
             % (len(SPECS), order_count, est["cells"], est["prompt_sessions"])]
    lines += ["  spec %-10s %s (%d prompts)" % (s["id"], s["title"], len(s["prompts"]))
              for s in SPECS]
    lines.append("  arm %s (%s) for every build; conditions warm and cold."
                 % (ARM_ID, next(a["label"] for a in ae.ARMS if a["id"] == ARM_ID)))
    lines.append("Cost: typical $%.2f ($%.2f per prompt session), worst case $%.2f ($%.2f per "
                 "prompt session, the live per-build limit)."
                 % (est["typical_usd"], est["per_session_typical_usd"], est["worst_case_usd"],
                    est["per_session_worst_usd"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--estimate", action="store_true", help="print the plan and costs, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="run every cell with the scripted generator (no model, no cost)")
    ap.add_argument("--live", action="store_true", help="run on the live paid model")
    ap.add_argument("--max-usd", type=float, default=None,
                    help="spend cap in USD; required with --live")
    ap.add_argument("--orders", type=int, default=len(SPEC_IDS),
                    help="number of orders: a multiple of %d (default %d)"
                    % (len(SPEC_IDS), len(SPEC_IDS)))
    ap.add_argument("--out", default=None, help="output directory")
    ap.add_argument("--analyse", default=None, metavar="DIR",
                    help="re-analyse DIR/results.json and print the report; runs nothing")
    args = ap.parse_args(argv)
    if sum([args.estimate, args.dry_run, args.live]) > 1:
        print("choose one of --estimate, --dry-run, --live", file=sys.stderr)
        return 2
    if args.live and (args.max_usd is None or args.max_usd <= 0):
        print("refusing to run live: a live run needs --live and --max-usd X, where X is a "
              "positive spend cap in USD", file=sys.stderr)
        return 2
    if args.analyse:
        path = Path(args.analyse) / "results.json"
        if not path.exists():
            print("no results at %s" % path, file=sys.stderr)
            return 2
        print(table(analyse(json.loads(path.read_text(encoding="utf-8")))))
        return 0
    try:
        rows = orders(SPEC_IDS, args.orders)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    cells = plan(SPECS, rows)
    est = estimate(SPECS, cells)
    print(_plan_text(len(rows), est))
    if args.estimate:
        return 0
    if not (args.dry_run or args.live):
        print("nothing to do: use --estimate, --dry-run, --analyse DIR, or --live --max-usd X",
              file=sys.stderr)
        return 2
    if args.dry_run:
        outdir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="app-econ-dry-"))
        generate, mode, cap = ae.scripted_generate, "demo", None
        print("DRY RUN: scripted generator, no model call, output in %s" % outdir, flush=True)
    else:
        status = ag.live_status()
        if not status["available"]:
            print("live mode unavailable: %s" % status["reason"], file=sys.stderr)
            return 2
        outdir = Path(args.out) if args.out else DEFAULT_OUT
        generate, mode, cap = ag.live_generate, "live", args.max_usd
        print("LIVE RUN: spend cap $%.2f, output in %s" % (cap, outdir), flush=True)
    result = run(SPECS, rows, generate, outdir, mode=mode, max_usd=cap,
                 progress=lambda line: print(line, flush=True))
    print()
    print(table(analyse(result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
