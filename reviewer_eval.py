"""Measure how often the goal reviewer is wrong on labelled cases.

The goal reviewer (agent_session.Session._ask_review) reads what the user asked, what
was built and the answer shown, and says whether anything the user asked for is
missing. This tool sends it hand-labelled cases from reviewer_cases.json and scores the
verdicts: how often it accepts an incomplete build (false acceptance) and how often it
rejects a good one (false rejection), each with a Wilson 95% interval.

    uv run python reviewer_eval.py --dry-run
    uv run python reviewer_eval.py --live --max-usd 2 --repeats 3

--dry-run uses a scripted reviewer: no network, no cost, output in a temporary
directory. --live calls the paid model through agent_session.live_generate and needs
BOTH --live and --max-usd; it prints the call count and an upper cost estimate before
the first call. Results are written after every case to <out>/results.json, so an
interrupted run resumes where it stopped.
"""

import argparse
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_CASES = ROOT / "reviewer_cases.json"
DEFAULT_LIVE_OUT = ROOT / "artifacts" / "agent" / "reviewer"

# qwen-3.8-27b prices in USD per million tokens, as in cerebras_client.py. They are
# copied rather than imported: importing cerebras_client loads .env at import time.
COST_INPUT_USD_PER_MTOK = 0.99
COST_OUTPUT_USD_PER_MTOK = 1.49
REPLY_CAP_TOKENS = 4500          # agent_session.REPLY_TOKENS: most a plain call may return
CHARS_PER_TOKEN = 3              # conservative input estimate (agent_session estimates 4)
Z95 = 1.959964                   # two-sided 95% normal quantile for the Wilson interval
LABELS = ("good", "incomplete")
VERDICTS = ("accept", "reject", "unreadable")
DRY_RUN_MORE_THAN_TESTS = 2      # the scripted reviewer accepts a build with MORE tests than this
NO_TRAP = "(no trap)"


# -- cases -------------------------------------------------------------------

def _fail(where, what):
    raise ValueError("%s: %s" % (where, what))


def _check_case(case, where):
    if not isinstance(case, dict):
        _fail(where, "a case must be a JSON object")
    cid = case.get("id")
    where = "%s (case %r)" % (where, cid)
    for key in ("id", "label", "goals", "tools", "answer"):
        if key not in case:
            _fail(where, "missing key %r" % key)
    if not isinstance(cid, str) or not cid.strip():
        _fail(where, "id must be a non-empty string")
    if case["label"] not in LABELS:
        _fail(where, "label must be 'good' or 'incomplete', not %r" % (case["label"],))
    goals = case["goals"]
    if not isinstance(goals, list) or not goals or not all(
            isinstance(g, str) and g.strip() for g in goals):
        _fail(where, "goals must be a non-empty list of non-empty strings")
    tools = case["tools"]
    if not isinstance(tools, list) or not tools:
        _fail(where, "tools must be a non-empty list")
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            _fail(where, "every tool needs a string name")
        if not isinstance(tool.get("params", []), list) or not all(
                isinstance(p, str) for p in tool.get("params", [])):
            _fail(where, "tool %s: params must be a list of strings" % tool["name"])
        if not isinstance(tool.get("description"), str) or not tool["description"].strip():
            _fail(where, "tool %s: description must be a non-empty string" % tool["name"])
        tests = tool.get("tests")
        if not isinstance(tests, list) or not tests:
            _fail(where, "tool %s: tests must be a non-empty list" % tool["name"])
        for test in tests:
            if not isinstance(test, dict) or not isinstance(test.get("call"), str) \
                    or not isinstance(test.get("expect"), str):
                _fail(where, "tool %s: each test needs string call and expect" % tool["name"])
    answer = case["answer"]
    if not isinstance(answer, dict) or not isinstance(answer.get("call"), str) \
            or not isinstance(answer.get("value"), str):
        _fail(where, "answer needs string call and value")
    if case["label"] == "incomplete" and not (isinstance(case.get("missing_truth"), str)
                                              and case["missing_truth"].strip()):
        _fail(where, "an incomplete case needs a missing_truth sentence")
    if "trap" in case and not isinstance(case["trap"], str):
        _fail(where, "trap must be a string when given")


def load_cases(path):
    """The labelled cases in PATH, validated. Raises ValueError with the case and key at fault."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        _fail(str(path), "expected a non-empty JSON list of cases")
    seen = set()
    for i, case in enumerate(data):
        _check_case(case, "%s, item %d" % (path, i))
        if case["id"] in seen:
            _fail(str(path), "duplicate case id %r" % case["id"])
        seen.add(case["id"])
    return data


# -- the question --------------------------------------------------------------

def review_system():
    """The reviewer's system prompt, read from agent_session at run time."""
    import agent_session as ag
    return ag.GOAL_REVIEW_SYSTEM


def build_message(case):
    """The user message the reviewer gets for CASE.

    Mirrors the text Session._ask_review assembles: "WHAT THE USER ASKED (latest
    last):", then "WHAT WAS BUILT:" with one "TOOL name (params): description" line
    per tool and up to three "   tested: call => expect" lines under it, then "THE
    ANSWER SHOWN TO THE USER: call => value". The same length limits apply.
    """
    goals = [g for g in dict.fromkeys(case["goals"]) if g][-4:]
    lines = []
    for tool in case["tools"][-24:]:
        params = " ".join(tool.get("params") or [])
        lines.append("TOOL %s (%s): %s" % (tool["name"], params,
                                           " ".join(tool["description"].split())[:160]))
        for test in (tool.get("tests") or [])[:3]:
            lines.append("   tested: %s => %s" % (" ".join(test["call"].split())[:220],
                                                  " ".join(test["expect"].split())[:120]))
    answer = "%s => %s" % (case["answer"]["call"], case["answer"]["value"])
    return ("WHAT THE USER ASKED (latest last):\n%s\nWHAT WAS BUILT:\n%s\n"
            "THE ANSWER SHOWN TO THE USER: %s" % (
                "\n".join("- " + " ".join(g.split())[:500] for g in goals),
                "\n".join(lines)[:6000], answer[:500]))


def worst_call_usd(system, user):
    """Upper bound on what one reviewer call can cost: input at a conservative token
    estimate, plus the most a plain reply may return."""
    tokens_in = math.ceil((len(system) + len(user)) / CHARS_PER_TOKEN)
    return (tokens_in * COST_INPUT_USD_PER_MTOK
            + REPLY_CAP_TOKENS * COST_OUTPUT_USD_PER_MTOK) / 1_000_000


# -- reading the reply -----------------------------------------------------------

def _parse(reply):
    """The reviewer object in REPLY, read as Session._parsed reads a reply, or None.

    REPLY is the generator's result dict (its "text" is read), the reply text, or an
    already parsed object. Each goes through extract_json and normalize_plan.
    """
    import agent_session as ag
    if isinstance(reply, dict) and "met" not in reply and "missing" not in reply:
        reply = reply.get("text")
    try:
        obj = dict(reply) if isinstance(reply, dict) else ag.extract_json(reply)
        plan = ag.normalize_plan(obj)
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(plan, dict):
        return None
    plan.pop("auto_fixes", None)
    return plan


def _read(reply):
    """(verdict, parsed object or None), following Session._ask_review."""
    parsed = _parse(reply)
    if parsed is None:
        return "unreadable", None
    missing = parsed.get("missing")
    if not isinstance(missing, list):
        return "unreadable", parsed
    if parsed.get("met") is True:
        return "accept", parsed
    found = [x for x in missing if str(x).strip()]
    return ("reject" if found else "accept"), parsed


def judge(reply):
    """'accept', 'reject' or 'unreadable' for a reviewer reply.

    met true is accept. Otherwise a non-empty missing list is reject, and an empty one
    is accept. Anything else, including a reply that does not parse, is unreadable.
    """
    return _read(reply)[0]


# -- running -----------------------------------------------------------------------

def _read_results(path):
    path = Path(path)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("results") or [])


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _envelope(results, mode, spent, stopped, repeats, max_usd, total):
    return {"schema": 1, "mode": mode, "repeats": repeats, "max_usd": max_usd,
            "spent_usd": round(spent, 6), "stopped": stopped,
            "complete": len(results) == total, "results": results}


def _ask(case, repeat, system, user, generate):
    """One reviewer call for CASE. A generator error is recorded as unreadable, with no cost."""
    started = time.perf_counter()
    try:
        reply = generate(system, user)
    except Exception as exc:  # noqa: BLE001 - one failed call must not end the run
        return {"id": case["id"], "label": case["label"], "trap": case.get("trap"),
                "repeat": repeat, "verdict": "unreadable", "why": "", "missing": [],
                "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "raw": "", "error": str(exc)[:300]}
    verdict, parsed = _read(reply)
    if isinstance(reply, dict):
        raw = reply.get("text") or ""
        cost = reply.get("cost_usd") or 0.0
        in_tok, out_tok = reply.get("input_tokens") or 0, reply.get("output_tokens") or 0
        latency = reply.get("latency_ms") or round((time.perf_counter() - started) * 1000, 1)
    else:
        raw, cost, in_tok, out_tok = str(reply or ""), 0.0, 0, 0
        latency = round((time.perf_counter() - started) * 1000, 1)
    parsed = parsed or {}
    missing = parsed.get("missing") if isinstance(parsed.get("missing"), list) else []
    return {"id": case["id"], "label": case["label"], "trap": case.get("trap"),
            "repeat": repeat, "verdict": verdict,
            "why": " ".join(str(parsed.get("why") or "").split())[:300],
            "missing": [" ".join(str(x).split())[:300] for x in missing][:4],
            "cost_usd": round(float(cost), 6), "input_tokens": in_tok,
            "output_tokens": out_tok, "latency_ms": latency, "raw": raw, "error": None}


def run(cases, generate, repeats=1, max_usd=None, out=None, mode="dry-run", progress=None):
    """Ask the reviewer about every case REPEATS times and return the results envelope.

    GENERATE is called as generate(system, user). Before each call, its worst-case cost
    is added to what has been spent; if that would pass MAX_USD the run stops cleanly
    with ``stopped`` set and no further call is made. OUT is a JSON file: records already
    in it are kept and skipped, and the file is rewritten after every case.
    """
    system = review_system()
    results = _read_results(out) if out else []
    done = {(r["id"], r["repeat"]) for r in results}
    spent = sum(r.get("cost_usd") or 0.0 for r in results)
    total = len(cases) * repeats
    stopped = None
    for repeat in range(repeats):
        for case in cases:
            if (case["id"], repeat) in done:
                continue
            user = build_message(case)
            worst = worst_call_usd(system, user)
            if max_usd is not None and spent + worst > max_usd:
                stopped = ("spend cap $%.4f: $%.4f spent, the next call may cost up to $%.4f"
                           % (max_usd, spent, worst))
                break
            rec = _ask(case, repeat, system, user, generate)
            results.append(rec)
            done.add((case["id"], repeat))
            spent += rec["cost_usd"]
            if out:
                _write(out, _envelope(results, mode, spent, None, repeats, max_usd, total))
            if progress:
                progress("[%d/%d] %s r%d: %s, $%.4f this call, $%.4f spent"
                         % (len(results), total, case["id"], repeat, rec["verdict"],
                            rec["cost_usd"], spent))
        if stopped:
            break
    data = _envelope(results, mode, spent, stopped, repeats, max_usd, total)
    if out:
        _write(out, data)
    return data


# -- scoring ---------------------------------------------------------------------

def wilson(k, n, z=Z95):
    """Wilson score interval (low, high) for k successes in n trials, or None when n is 0."""
    if n <= 0:
        return None
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _rate(count, of):
    return {"count": count, "of": of,
            "rate": (count / of) if of else None,
            "interval": wilson(count, of)}


def score(results):
    """Confusion counts, both error rates with Wilson intervals, the per-trap breakdown,
    agreement across repeats and the misjudged records. ``results`` is a list of
    records or the envelope that run() returns."""
    if isinstance(results, dict):
        results = results.get("results", [])
    counts = {label: {v: 0 for v in VERDICTS} for label in LABELS}
    traps, misjudged, judged_by_id = {}, [], {}
    for r in results:
        counts[r["label"]][r["verdict"]] += 1
        trap = r.get("trap") or NO_TRAP
        row = traps.setdefault(trap, {"judged": 0, "misjudged": 0})
        if r["verdict"] == "unreadable":
            continue
        row["judged"] += 1
        judged_by_id.setdefault(r["id"], []).append(r["verdict"])
        wrong = (r["label"] == "incomplete" and r["verdict"] == "accept") or \
                (r["label"] == "good" and r["verdict"] == "reject")
        if wrong:
            row["misjudged"] += 1
            misjudged.append({"id": r["id"], "label": r["label"], "verdict": r["verdict"],
                              "repeat": r["repeat"], "why": r.get("why") or ""})
    good, inc = counts["good"], counts["incomplete"]
    good_judged = good["accept"] + good["reject"]
    inc_judged = inc["accept"] + inc["reject"]
    repeated = {k: v for k, v in judged_by_id.items() if len(v) >= 2}
    consistent = sorted(k for k, v in repeated.items() if len(set(v)) == 1)
    flipping = sorted(k for k in repeated if k not in consistent)
    return {
        "records": len(results),
        "cases": len({r["id"] for r in results}),
        "unreadable": good["unreadable"] + inc["unreadable"],
        "unreadable_ids": sorted({r["id"] for r in results if r["verdict"] == "unreadable"}),
        "confusion": {"good": {"accept": good["accept"], "reject": good["reject"]},
                      "incomplete": {"accept": inc["accept"], "reject": inc["reject"]}},
        "false_acceptance": _rate(inc["accept"], inc_judged),
        "false_rejection": _rate(good["reject"], good_judged),
        "traps": traps,
        "agreement": {"repeated": len(repeated), "consistent": len(consistent),
                      "rate": (len(consistent) / len(repeated)) if repeated else None,
                      "flipping": flipping},
        "misjudged": misjudged,
    }


def _pct(x):
    return "n/a" if x is None else "%.1f%%" % (100 * x)


def _rate_line(name, block):
    if not block["of"]:
        return "%s: no case judged" % name
    lo, hi = block["interval"]
    return "%s: %d of %d = %s (95%% Wilson interval %s to %s)" % (
        name, block["count"], block["of"], _pct(block["rate"]), _pct(lo), _pct(hi))


def table(report):
    """Plain-text report of score(results): the rates with their intervals, the confusion
    counts, the traps, agreement, and each misjudged case with the reviewer's reason."""
    s = report if "confusion" in report else score(report)
    c = s["confusion"]
    lines = ["GOAL REVIEWER EVALUATION",
             "records %d, cases %d, unreadable %d" % (s["records"], s["cases"], s["unreadable"]),
             "", "%-12s %8s %8s" % ("", "accepts", "rejects"),
             "%-12s %8d %8d" % ("good", c["good"]["accept"], c["good"]["reject"]),
             "%-12s %8d %8d" % ("incomplete", c["incomplete"]["accept"], c["incomplete"]["reject"]),
             "",
             _rate_line("False acceptance (an incomplete build was passed)", s["false_acceptance"]),
             _rate_line("False rejection (a good build was failed)", s["false_rejection"])]
    ag = s["agreement"]
    if ag["repeated"]:
        lines.append("Agreement across repeats: %d of %d repeated cases gave one verdict every "
                     "time (%s)" % (ag["consistent"], ag["repeated"], _pct(ag["rate"])))
        if ag["flipping"]:
            lines.append("Cases that flipped: " + ", ".join(ag["flipping"]))
    else:
        lines.append("Agreement across repeats: no case was asked more than once")
    if s["unreadable_ids"]:
        lines.append("Unreadable replies for: " + ", ".join(s["unreadable_ids"]))
    lines += ["", "Per trap (judged, misjudged):"]
    for trap in sorted(s["traps"]):
        row = s["traps"][trap]
        lines.append("  %-34s %4d %4d" % (trap, row["judged"], row["misjudged"]))
    lines += ["", "Misjudged cases (id, label, verdict, repeat: the reviewer's why):"]
    if not s["misjudged"]:
        lines.append("  none")
    for m in s["misjudged"]:
        lines.append("  %s (%s, %s, repeat %d): %s" % (
            m["id"], m["label"], m["verdict"], m["repeat"], m["why"] or "(no reason given)"))
    return "\n".join(lines)


# -- dry run ---------------------------------------------------------------------------

def scripted_generate(system, user):
    """Offline stand-in reviewer for --dry-run and the tests: no network, no cost.

    Deliberately imperfect: it accepts a build only when its tools carry more than
    DRY_RUN_MORE_THAN_TESTS tested calls, and rejects every other build whatever it says.
    """
    tests = user.count("   tested: ")
    if tests > DRY_RUN_MORE_THAN_TESTS:
        obj = {"action": "review", "met": True, "missing": [],
               "why": "the tested calls cover what was asked"}
    else:
        obj = {"action": "review", "met": False,
               "missing": ["the requested behaviour is not shown by the tests"],
               "why": "too few tested calls to show the requested behaviour"}
    text = json.dumps(obj)
    return {"text": text, "input_tokens": (len(system) + len(user)) // 4,
            "output_tokens": len(text) // 4, "estimated": True, "cost_usd": 0.0,
            "latency_ms": 0.0, "model": "dry-run-script", "request_id": None,
            "finish_reason": "stop"}


# -- command line -----------------------------------------------------------------

def estimate(cases, repeats, system):
    """Calls and an upper cost estimate for a run over CASES."""
    calls = len(cases) * repeats
    usd = sum(worst_call_usd(system, build_message(c)) for c in cases) * repeats
    return {"calls": calls, "upper_usd": round(usd, 4)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Measure the goal reviewer's error rates on labelled cases.")
    ap.add_argument("--dry-run", action="store_true",
                    help="use the scripted reviewer (no model, no cost, temporary output)")
    ap.add_argument("--live", action="store_true", help="call the paid model; needs --max-usd")
    ap.add_argument("--max-usd", type=float, default=None,
                    help="spend cap in USD; required with --live")
    ap.add_argument("--cases", default=str(DEFAULT_CASES), help="case file (JSON)")
    ap.add_argument("--repeats", type=int, default=1, help="asks per case (default 1)")
    ap.add_argument("--out", default=None,
                    help="output directory for results.json (dry runs default to a temp directory, "
                         "live runs to artifacts/agent/reviewer)")
    args = ap.parse_args(argv)
    if args.dry_run == args.live:
        print("choose exactly one of --dry-run or --live --max-usd X", file=sys.stderr)
        return 2
    if args.live and (args.max_usd is None or args.max_usd <= 0):
        print("refusing to run live: a live run needs --live and --max-usd X, where X is a "
              "positive spend cap in USD", file=sys.stderr)
        return 2
    if args.repeats < 1:
        print("--repeats must be at least 1", file=sys.stderr)
        return 2
    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError) as exc:
        print("cannot load cases: %s" % exc, file=sys.stderr)
        return 2
    system = review_system()
    if args.dry_run:
        outdir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="reviewer-dry-"))
        generate, mode, cap = scripted_generate, "dry-run", None
        print("DRY RUN: scripted reviewer, no model call; output in %s" % outdir, flush=True)
    else:
        plan = estimate(cases, args.repeats, system)
        print("LIVE RUN plan: %d reviewer calls (%d case(s) x %d repeat(s)); upper cost estimate "
              "$%.4f; spend cap $%.2f" % (plan["calls"], len(cases), args.repeats,
                                          plan["upper_usd"], args.max_usd), flush=True)
        import agent_session as ag
        status = ag.live_status()
        if not status["available"]:
            print("live mode unavailable: %s" % status["reason"], file=sys.stderr)
            return 2
        outdir = Path(args.out) if args.out else DEFAULT_LIVE_OUT
        generate, mode, cap = ag.live_generate, "live", args.max_usd
        print("LIVE RUN: spend cap $%.2f; output in %s" % (cap, outdir), flush=True)
    data = run(cases, generate, repeats=args.repeats, max_usd=cap,
               out=Path(outdir) / "results.json", mode=mode,
               progress=lambda line: print(line, flush=True))
    print()
    print(table(score(data)))
    if data["stopped"]:
        print("\nSTOPPED: %s" % data["stopped"])
    print("\nMODE THAT RAN: %s" % (
        "dry-run (scripted reviewer, no model call, no network)" if mode == "dry-run"
        else "LIVE (paid model through agent_session.live_generate, spend cap $%.2f, "
             "spent $%.4f)" % (cap, data["spent_usd"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
