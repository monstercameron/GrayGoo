"""What the harness specialisations save, measured from the session logs.

    uv run python specialization.py                  # all live builds
    uv run python specialization.py pcrm-a82b        # one project
    uv run python specialization.py --mode demo --json

A build is one session log of arm "main" in the chosen mode (the "nomem" twin
is skipped), optionally narrowed to one project. Spend, model calls and
seconds are read the way economics.py reads them. report() returns a plain
dict whose keys the dashboard relies on.

Measured from the logs: spend, calls, seconds, the first verdict of each
function and the repair share of spend in each period, the warm check
timings, and the prices per token (least squares over the logged reply costs).

Estimated: every saving in "mechanisms". Each entry's "how" sentence says how
its figure was derived. A saving is a counterfactual, so the total is a rough
ceiling rather than an audit.
"""
import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path

import economics as E

SESSIONS_DIR = E.SESSIONS_DIR
MECHANISM_LABELS = (
    ("auto-fix", "Replies repaired without a model call"),
    ("compaction", "Prompt text left out by context compaction"),
    ("edits", "Repairs sent as small edits instead of whole functions"),
    ("warm-repl", "Lisp checks answered by the warm process"),
    ("kit", "Helper functions supplied instead of built"),
    ("reuse", "Prompts answered by functions that already existed"),
    ("free-checks", "Faults found by checks that cost no model call"),
)
LABELS = dict(MECHANISM_LABELS)
DEFAULT_PRICE_IN = 0.99 / 1_000_000    # USD per input token, if the fit fails
DEFAULT_PRICE_OUT = 1.49 / 1_000_000   # USD per output token, if the fit fails
CHARS_PER_TOKEN = 4
COLD_FALLBACK_MS = 122.0               # used when no check is logged as cold
MAX_PERIODS = 4
MIN_PERIOD_BUILDS = 3
TOP_FIXES = 10
CHECK_KINDS = ("acceptance", "interface_check", "style_check")


# ---------------------------------------------------------------- reading

def _non_empty_list(value):
    return isinstance(value, list) and len(value) > 0


def _pair_calls(events):
    """Model replies paired with their calls, lane by lane, as economics.py pairs them."""
    pending, calls = defaultdict(deque), []
    for pos, e in enumerate(events):
        kind = e.get("kind")
        if kind == "model_call":
            pending[E._lane(e)].append((pos, e))
        elif kind == "model_reply":
            lane = E._lane(e)
            if not pending.get(lane):
                heads = [(queue[0][0], key) for key, queue in pending.items() if queue]
                if not heads:
                    continue
                lane = min(heads)[1]
            _, call = pending[lane].popleft()
            label = call.get("label") if isinstance(call.get("label"), str) else "?"
            context = call.get("context")
            calls.append({"label": label, "cat": E.category(label),
                          "cost": E._num(e.get("cost_usd")),
                          "input_tokens": int(E._num(e.get("input_tokens"))),
                          "output_tokens": int(E._num(e.get("output_tokens"))),
                          "latency_ms": E._num(e.get("latency_ms")),
                          "context": context if isinstance(context, dict) else None})
    return calls


def _first_verdicts(events):
    """One pass (True) or fail (False) per function attempt chain in a session.

    A verdict with a lane belongs to that lane. An unlaned verdict belongs to
    the span between two step or promoted events. Only the first verdict of
    each function counts.
    """
    seen, outcomes, span = set(), [], 0
    for e in events:
        kind = e.get("kind")
        if kind in ("step", "promoted"):
            span += 1
        elif kind == "verdict":
            lane = E._lane(e)
            key = ("lane", lane) if lane else ("span", span)
            if key not in seen:
                seen.add(key)
                outcomes.append(e.get("ok") is True)
    return outcomes


def _session(stem, events, meta):
    """One build: its meta, its paired calls and the raw facts the mechanisms count."""
    times = [e["t"] for e in events if E._is_number(e.get("t"))]
    calls = _pair_calls(events)
    row = {"id": stem, "t": meta["t"], "end": max(times) if times else meta["t"],
           "project": meta["project"], "flow": meta["flow"], "seconds": meta["seconds"],
           "calls": calls, "cost": sum(c["cost"] for c in calls),
           "first": _first_verdicts(events), "functions": 0, "compaction": [],
           "fixes": [], "edit_events": 0, "edit_chars": 0, "repl": [], "kit": set(),
           "checks": 0, "kinds": set(), "summary": False}
    for e in events:
        kind = e.get("kind")
        row["kinds"].add(kind)
        if kind == "summary":
            row["summary"] = True
        elif kind == "model_call":
            context = e.get("context")
            if isinstance(context, dict) and E._is_number(context.get("saved")) \
                    and context["saved"] > 0:
                row["compaction"].append(context["saved"])
        elif kind == "auto_fix":
            ids = e.get("fixes")
            if _non_empty_list(ids):
                row["fixes"].append(sorted({i for i in ids if isinstance(i, str)}))
        elif kind == "edit":
            row["edit_events"] += 1
            if E._is_number(e.get("saved_chars")):
                row["edit_chars"] += e["saved_chars"]
        elif kind == "repl":
            row["repl"].append((e.get("warm"), e.get("elapsed_ms")))
        elif kind == "kit_seeded":
            tools = e.get("tools")
            if isinstance(tools, list):
                row["kit"].update(t for t in tools if isinstance(t, str) and t)
        elif kind == "promoted":
            row["functions"] += 1
        elif kind == "smoke" and e.get("ok") is False:
            row["checks"] += 1
        elif kind == "acceptance" and E._num(e.get("failed")) > 0:
            row["checks"] += 1
        elif kind == "interface_check" and _non_empty_list(e.get("problems")):
            row["checks"] += 1
        elif kind == "style_check" and _non_empty_list(e.get("undefined")):
            row["checks"] += 1
    return row


def _load_sessions(project, mode, sessions_dir):
    """Builds of arm "main", in the given mode (None for both), oldest first.

    A log with no goal event is not a build (an empty or unreadable file).
    """
    directory = Path(sessions_dir) if sessions_dir is not None else SESSIONS_DIR
    try:
        paths = sorted(directory.glob("*.jsonl"))
    except OSError:
        paths = []
    sessions = []
    for path in paths:
        events = E.read_events(path)
        meta = E._meta(events)
        if not any(e.get("kind") == "goal" for e in events):
            continue
        if meta["arm"] != "main" or (mode and meta["mode"] != mode) or \
                (project and meta["project"] != project):
            continue
        sessions.append(_session(path.stem, events, meta))
    sessions.sort(key=lambda s: (s["t"], s["id"]))
    return sessions


# ---------------------------------------------------------------- measured figures

def _median(values):
    values = list(values)
    return statistics.median(values) if values else None


def _per(part, whole, digits):
    """part / whole rounded to DIGITS, or None when the whole is zero."""
    return round(part / whole, digits) if whole else None


def _scaled(count, value):
    """count x value: zero when nothing was counted, None when the value is unknown."""
    if not count:
        return 0.0
    return None if value is None else count * value


def _cell(value, spec, empty="n/a"):
    return empty if value is None else spec % value


def _fit_prices(calls):
    """(USD per input token, USD per output token, measured) by least squares.

    The fit is cost = a x input tokens + b x output tokens over the replies
    with a positive cost. When it cannot be made, the defaults are returned
    and measured is False.
    """
    rows = [(c["input_tokens"], c["output_tokens"], c["cost"]) for c in calls if c["cost"] > 0]
    sii = sum(i * i for i, _, _ in rows)
    soo = sum(o * o for _, o, _ in rows)
    sio = sum(i * o for i, o, _ in rows)
    sic = sum(i * c for i, _, c in rows)
    soc = sum(o * c for _, o, c in rows)
    det = sii * soo - sio * sio
    if len(rows) >= 2 and det > 0:
        a = (sic * soo - soc * sio) / det
        b = (soc * sii - sic * sio) / det
        if a > 0 and b > 0:
            return a, b, True
    return DEFAULT_PRICE_IN, DEFAULT_PRICE_OUT, False


def _entry(mid, events, saved_calls, saved_usd, saved_seconds, basis, how, **extra):
    """One mechanism row, rounded the way the report promises."""
    row = {"id": mid, "label": LABELS[mid], "events": events, "saved_calls": saved_calls,
           "saved_usd": None if saved_usd is None else round(saved_usd, 4),
           "saved_seconds": None if saved_seconds is None else round(saved_seconds, 1),
           "basis": basis, "how": how}
    row.update(extra)
    return row


COSMETIC_FIXES = frozenset(["added-docstring"])   # tidying that never stood between a reply and its tests
APP_BUILD_FUNCTIONS = 3                            # a build that saved this many functions is "app-sized"


def _real_fixes(session):
    """The auto-fix events of one build that changed more than a docstring."""
    return [ids for ids in session["fixes"] if set(ids) - COSMETIC_FIXES]


def _auto_fix(sessions, calls):
    every = [ids for s in sessions for ids in s["fixes"]]
    fixes = [ids for s in sessions for ids in _real_fixes(s)]
    count = len(fixes)
    repairs = [c for c in calls if c["cat"] == "repairs"]
    cost = _median(c["cost"] for c in repairs)
    latency = _median(c["latency_ms"] for c in repairs if c["latency_ms"] > 0)
    tally = Counter(fid for ids in fixes for fid in ids)
    return _entry(
        "auto-fix", count, count, _scaled(count, cost),
        _scaled(count, None if latency is None else latency / 1000), "estimate",
        "Each reply repaired without a call would otherwise have gone back to the model "
        "once, priced at the median cost (%s USD) and latency (%s ms) of the repair calls "
        "in these logs. %d further replies only had a docstring added and are not counted."
        % (_cell(cost, "%.6f"), _cell(latency, "%.1f"), len(every) - count),
        by_fix=dict(tally.most_common(TOP_FIXES)), cosmetic=len(every) - count)


def _compaction(sessions, calls, price_in, measured):
    saved = [v for s in sessions for v in s["compaction"]]
    chars = int(round(sum(saved)))
    pairs = [(c["context"]["chars"], c["input_tokens"]) for c in calls
             if c["context"] is not None and E._is_number(c["context"].get("chars"))]
    context_chars = sum(ch for ch, _ in pairs)
    input_tokens = sum(tok for _, tok in pairs)
    ratio = input_tokens / context_chars if context_chars else None
    tokens = None if ratio is None else chars * ratio
    usd = 0.0 if not saved else (None if tokens is None else tokens * price_in)
    source = "fitted to these logs" if measured else "the default, the fit could not be made"
    return _entry(
        "compaction", len(saved), None, usd, None, "estimate",
        "Estimate: the %d characters left out, at %s input tokens per character over the "
        "calls that logged both figures, priced at %s USD per million input tokens (%s)."
        % (chars, _cell(ratio, "%.4f"), "%.4f" % (price_in * 1_000_000), source),
        saved_chars=chars)


def _edits(sessions, calls, price_out, measured):
    events = sum(s["edit_events"] for s in sessions)
    chars = sum(s["edit_chars"] for s in sessions)
    tokens = chars / CHARS_PER_TOKEN
    timed = [c for c in calls if c["latency_ms"] > 0]
    out_tokens = sum(c["output_tokens"] for c in timed)
    out_seconds = sum(c["latency_ms"] for c in timed) / 1000
    speed = out_tokens / out_seconds if out_seconds else None
    seconds = 0.0 if not events else (tokens / speed if speed else None)
    source = "fitted to these logs" if measured else "the default"
    return _entry(
        "edits", events, None, tokens * price_out, seconds, "estimate",
        "Estimate: the small edits saved %d characters, which is %.0f output tokens at %d "
        "characters per token, priced at %s USD per million output tokens (%s) and timed at "
        "the mean output speed of %s tokens per second."
        % (chars, tokens, CHARS_PER_TOKEN, "%.4f" % (price_out * 1_000_000), source,
           _cell(speed, "%.1f")))


def _warm_repl(sessions):
    checks = [item for s in sessions for item in s["repl"]]
    warm_events = sum(1 for flag, _ in checks if flag is True)
    warm_ms = [ms for flag, ms in checks if flag is True and E._is_number(ms)]
    cold_ms = [ms for flag, ms in checks if flag is False and E._is_number(ms)]
    warm = _median(warm_ms)
    cold_logged = _median(cold_ms)
    cold = cold_logged if cold_logged is not None else COLD_FALLBACK_MS
    if not warm_events:
        seconds = 0.0
    elif warm is None:
        seconds = None
    else:
        seconds = warm_events * max(0.0, cold - warm) / 1000
    measured = cold_logged is not None and warm is not None
    source = ("the median of the checks logged as cold" if cold_logged is not None
              else "the fallback figure, because no check is logged as cold")
    return _entry(
        "warm-repl", warm_events, None, None, seconds,
        "measured" if measured else "estimate",
        "Each warm check saves the gap between a cold check at %s ms (%s) and the warm "
        "median of %s ms."
        % (_cell(cold, "%.1f"), source, _cell(warm, "%.1f")))


def _kit(sessions):
    seeded = defaultdict(set)
    for s in sessions:
        seeded[s["project"]] |= s["kit"]
    events = sum(len(tools) for tools in seeded.values())
    built = [s for s in sessions if s["functions"] > 0]
    per_calls = _median(len(s["calls"]) / s["functions"] for s in built)
    per_usd = _median(s["cost"] / s["functions"] for s in built)
    calls_saved = 0 if not events else (None if per_calls is None else int(events * per_calls))
    return _entry(
        "kit", events, calls_saved, _scaled(events, per_usd), None, "estimate",
        "Estimate: each distinct helper seeded for a project would otherwise have been built, "
        "at the median %s model calls and %s USD per saved function over the builds that saved "
        "one." % (_cell(per_calls, "%.2f"), _cell(per_usd, "%.6f")))


def _reuse(sessions):
    reused = [s for s in sessions if s["flow"] in ("cache", "reuse")]
    typical = _median(s["cost"] for s in sessions if s["flow"] in ("build", "plan"))
    their_cost = sum(s["cost"] for s in reused)
    events = len(reused)
    if not events:
        usd = 0.0
    elif typical is None:
        usd = None
    else:
        usd = max(0.0, events * typical - their_cost)
    return _entry(
        "reuse", events, None, usd, None, "estimate",
        "Estimate: each reused prompt would otherwise have cost the median build or plan "
        "session (%s USD), minus what the reused sessions actually cost (%.6f USD)."
        % (_cell(typical, "%.6f"), their_cost))


def _free_checks(sessions):
    events = sum(s["checks"] for s in sessions)
    return _entry(
        "free-checks", events, None, None, None, "count",
        "Count only: smoke checks that failed, acceptance runs with failures, interface checks "
        "with problems and style checks with undefined names; none of them cost a model call.")


# ---------------------------------------------------------------- periods

def _groups(n):
    """Index spans (start, end) splitting N builds into equal groups of at least three."""
    count = min(MAX_PERIODS, n // MIN_PERIOD_BUILDS)
    spans, start = [], 0
    for i in range(count):
        size = n // count + (1 if i < n % count else 0)
        spans.append((start, start + size))
        start += size
    return spans


def _period(label, group):
    functions = sum(s["functions"] for s in group)
    calls = sum(len(s["calls"]) for s in group)
    cost = sum(s["cost"] for s in group)
    seconds = sum(s["seconds"] for s in group)
    repair_cost = sum(c["cost"] for s in group for c in s["calls"] if c["cat"] == "repairs")
    outcomes = [o for s in group for o in s["first"]]
    return {"label": label, "builds": len(group), "functions_saved": functions,
            "calls_per_function": _per(calls, functions, 3),
            "usd_per_function": _per(cost, functions, 4),
            "seconds_per_function": _per(seconds, functions, 1),
            "first_try_rate": _per(sum(outcomes), len(outcomes), 3),
            "repair_share": _per(repair_cost, cost, 3)}


def _periods(sessions):
    return [_period("builds %d-%d" % (lo + 1, hi), sessions[lo:hi])
            for lo, hi in _groups(len(sessions))]


def _day(epoch):
    return time.strftime("%b %d", time.localtime(epoch)).replace(" 0", " ")


def _app_periods(sessions):
    """Periods over app-sized builds only, so early one-function builds do not set the baseline.

    Early builds were single small functions and later ones whole web apps; per-function
    figures over all builds mostly show that change. Among app-sized builds the unit is
    comparable, and paid repairs can be read next to the repairs the harness made for free.
    """
    apps = [s for s in sessions if s["functions"] >= APP_BUILD_FUNCTIONS]
    out = []
    for lo, hi in _groups(len(apps)):
        group = apps[lo:hi]
        first, last = _day(group[0]["t"]), _day(group[-1]["t"])
        row = _period(first if first == last else "%s to %s" % (first, last), group)
        functions = row["functions_saved"]
        row["repairs_per_function"] = _per(
            sum(1 for s in group for c in s["calls"] if c["cat"] == "repairs"), functions, 2)
        row["free_repairs_per_function"] = _per(
            sum(len(_real_fixes(s)) for s in group), functions, 2)
        out.append(row)
    return out


# ---------------------------------------------------------------- report

def _notes(sessions, mechanisms, prices, project, mode):
    price_in, price_out, measured = prices
    total = len(sessions)
    scope_mode = "all modes" if mode is None else mode + " mode"
    scope_project = "" if project is None else ", project " + project
    notes = ["Builds are sessions of arm main in %s%s; nomem twins are not counted."
             % (scope_mode, scope_project)]
    if total == 0:
        notes.append("No builds match this scope, so every figure is zero or empty.")
    elif total < MIN_PERIOD_BUILDS:
        notes.append("Fewer than three builds, so there are no periods.")
    notes.append("Later builds answer different prompts from earlier ones, so the periods "
                 "compare different work as well as different harness versions.")
    notes.append("Savings overlap (a helper and a reused function can replace the same work), "
                 "so the saved total is a rough ceiling, not a sum of exact figures.")
    if measured:
        notes.append("Prices were fitted from the logs: %.4f USD per million input tokens and "
                     "%.4f per million output tokens."
                     % (price_in * 1_000_000, price_out * 1_000_000))
    else:
        notes.append("Prices are the defaults (0.99 and 1.49 USD per million tokens), because "
                     "the fit could not be made from these logs.")
    with_summary = sum(1 for s in sessions if s["summary"])
    if with_summary < total:
        notes.append("summary.flow is logged on %d of %d builds; the rest take their flow from "
                     "the first decision, as economics.py does." % (with_summary, total))
    warm_checks = sum(1 for s in sessions for flag, _ in s["repl"] if flag is True)
    warm_builds = sum(1 for s in sessions if any(flag is True for flag, _ in s["repl"]))
    if warm_checks:
        notes.append("The warm flag is logged on %d check(s) in %d build(s). A check without "
                     "the flag is never counted as cold." % (warm_checks, warm_builds))
    missing = [kind for kind in CHECK_KINDS if not any(kind in s["kinds"] for s in sessions)]
    if total and missing:
        notes.append("No %s events appear in these builds, so those checks add nothing to the "
                     "free-checks figure." % ", ".join(missing))
    auto = next(m for m in mechanisms if m["id"] == "auto-fix")
    if auto["events"]:
        notes.append("A reply the harness repaired might also have passed unrepaired, so the "
                     "auto-fix saving is an upper bound; replay_evidence.py measures the same "
                     "thing directly by running each logged reply with and without the repairs.")
    notes.append("The periods over app-sized builds (%d or more functions saved) are the "
                 "like-for-like comparison; even there the prompts differ, so read them as a "
                 "description, not as proof of a trend." % APP_BUILD_FUNCTIONS)
    if any(s["compaction"] for s in sessions):
        notes.append("The tokens-per-character ratio is measured over whole prompts, so it is a "
                     "rough conversion for the text that was left out.")
    if any(s["kit"] for s in sessions):
        notes.append("The kit figure assumes each seeded helper would otherwise have cost the "
                     "median calls of one saved function; it is an assumption, not a measurement.")
    if any(s["functions"] for s in sessions):
        notes.append("Functions saved counts promoted events, so a function saved twice counts "
                     "twice.")
    return notes


def _measured_auto_fix(entry, replay):
    """The auto-fix entry with the replay's measurement in place of the estimate.

    The estimate assumes every repaired reply would have failed without the
    repair. The replay ran each logged reply both ways, so its count of replies
    that pass only with the repairs is the figure to show when it exists.
    """
    rescued, cases = replay.get("rescued"), replay.get("cases")
    if not isinstance(rescued, int) or not isinstance(cases, int) or rescued < 0:
        return entry
    per_call = (entry["saved_seconds"] / entry["saved_calls"]
                if entry["saved_calls"] and entry["saved_seconds"] is not None else None)
    measured = dict(entry)
    measured.update(
        saved_calls=rescued,
        saved_usd=round(float(replay.get("rescued_usd") or 0.0), 4),
        saved_seconds=None if per_call is None else round(rescued * per_call, 1),
        basis="measured", estimate_calls=entry["saved_calls"],
        how="Measured by running %d logged replies with and without the repairs: %d pass "
            "only with them (replay_evidence.py). Dollars and seconds use the median repair "
            "call. The logs show %d replies that got a repair, which would be the estimate."
            % (cases, rescued, entry["events"]))
    return measured


def report(project=None, mode="live", sessions_dir=None, replay=None):
    """The specialisation report for one mode (None for both) and project (None for all).

    The dict has the keys mode, project, builds, functions_saved, spent_usd,
    model_calls, seconds, window, periods, app_periods, mechanisms, saved and notes.
    REPLAY is the result of ``replay_evidence`` over the same logs, when there is one:
    it replaces the auto-fix estimate with what was measured.
    """
    sessions = _load_sessions(project, mode, sessions_dir)
    calls = [c for s in sessions for c in s["calls"]]
    price_in, price_out, measured = _fit_prices(calls)
    mechanisms = [
        _auto_fix(sessions, calls),
        _compaction(sessions, calls, price_in, measured),
        _edits(sessions, calls, price_out, measured),
        _warm_repl(sessions),
        _kit(sessions),
        _reuse(sessions),
        _free_checks(sessions),
    ]
    if isinstance(replay, dict) and project is None:
        mechanisms[0] = _measured_auto_fix(mechanisms[0], replay)
    saved_calls = sum(m["saved_calls"] or 0 for m in mechanisms)
    saved_usd = sum(m["saved_usd"] or 0.0 for m in mechanisms)
    saved_seconds = sum(m["saved_seconds"] or 0.0 for m in mechanisms)
    return {
        "mode": mode,
        "project": project,
        "builds": len(sessions),
        "functions_saved": sum(s["functions"] for s in sessions),
        "spent_usd": round(sum(s["cost"] for s in sessions), 4),
        "model_calls": len(calls),
        "seconds": round(sum(s["seconds"] for s in sessions), 1),
        "window": {"first": float(min(s["t"] for s in sessions)) if sessions else None,
                   "last": float(max(s["end"] for s in sessions)) if sessions else None},
        "periods": _periods(sessions),
        "app_periods": _app_periods(sessions),
        "mechanisms": mechanisms,
        "saved": {"calls": int(saved_calls), "usd": round(saved_usd, 4),
                  "seconds": round(saved_seconds, 1)},
        "notes": _notes(sessions, mechanisms, (price_in, price_out, measured), project, mode),
    }


# ---------------------------------------------------------------- text output

def _when(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def _table(header, rows):
    """Aligned plain-text table lines: the header, a rule, then the rows."""
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]

    def line(cells):
        return "  ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()

    return [line(header), "  ".join("-" * w for w in widths)] + [line(r) for r in rows]


def render(report):
    """The report as plain text (ASCII), one string with newlines."""
    who = "all projects" if report["project"] is None else "project " + report["project"]
    which = "all modes" if report["mode"] is None else report["mode"] + " mode"
    lines = ["Specialization report: %s, %s, arm main." % (who, which)]
    window = report["window"]
    if window["first"] is not None:
        lines.append("Window: %s to %s." % (_when(window["first"]), _when(window["last"])))
    lines.append("Builds %d, functions saved %d, spent $%.4f, %d model call(s), %.1f minutes."
                 % (report["builds"], report["functions_saved"], report["spent_usd"],
                    report["model_calls"], report["seconds"] / 60))

    lines.append("")
    if report["periods"]:
        header = ["Period", "Builds", "Saved", "Calls/fn", "USD/fn", "Sec/fn", "First try",
                  "Repair share"]
        rows = [[p["label"], p["builds"], p["functions_saved"],
                 _cell(p["calls_per_function"], "%.3f", "-"),
                 _cell(p["usd_per_function"], "%.4f", "-"),
                 _cell(p["seconds_per_function"], "%.1f", "-"),
                 _cell(p["first_try_rate"], "%.3f", "-"),
                 _cell(p["repair_share"], "%.3f", "-")] for p in report["periods"]]
        lines.append("Periods, oldest first:")
        lines += _table(header, rows)
    else:
        lines.append("No periods (fewer than three builds).")

    lines += ["", "Mechanisms:"]
    for m in report["mechanisms"]:
        lines.append("  %s [%s]" % (m["label"], m["basis"]))
        lines.append("      events %d; calls saved %s; cost saved %s; time saved %s"
                     % (m["events"], _cell(m["saved_calls"], "%d"),
                        _cell(m["saved_usd"], "$%.4f"), _cell(m["saved_seconds"], "%.1f s")))
        if "by_fix" in m:
            top = ", ".join("%s %d" % item for item in m["by_fix"].items()) or "none"
            lines.append("      top fixes: " + top)
        lines.append("      " + m["how"])

    saved = report["saved"]
    lines += ["", "Saved in total (sum of the mechanisms, which can overlap): %d calls, $%.4f, "
                  "%.1f s." % (saved["calls"], saved["usd"], saved["seconds"]), "", "Notes:"]
    lines += ["  - " + note for note in report["notes"]]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("project", nargs="?", default=None, help="only this project's builds")
    ap.add_argument("--mode", choices=("live", "demo"), default="live")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    data = report(args.project, args.mode)
    print(json.dumps(data, indent=2) if args.json else render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
