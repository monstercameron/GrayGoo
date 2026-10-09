"""Review the agent's logs to find what to refine next.

Reads the session logs (every prompt sent to the model, every reply, every
Lisp evaluation and verdict), the typed-REPL call log and the mounted apps'
request logs, and reports where tokens, time and repairs go.

    uv run python logreview.py                    # everything since the last reset
    uv run python logreview.py --mode live --hours 24
    uv run python logreview.py --project blog-a69f --json

All functions here are pure over parsed log rows; only ``read_jsonl`` and
``main`` touch the disk.
"""
import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import agent_session as ag
import oracle as orc


def read_jsonl(path):
    """Parsed rows of a JSONL file; unreadable lines are skipped."""
    rows = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def session_meta(events):
    """``{prompt, mode, project, arm, t, outcome}`` of one session's events."""
    goal = next((e for e in events if e.get("kind") == "goal"), {})
    summary = next((e for e in reversed(events) if e.get("kind") == "summary"), {})
    done = next((e for e in reversed(events) if e.get("kind") == "done"), {})
    return {"prompt": goal.get("prompt", ""), "mode": goal.get("mode"),
            "project": goal.get("project") or "scratch", "arm": goal.get("arm", "main"),
            "t": goal.get("t", 0),
            "outcome": summary.get("outcome") or done.get("state") or "unfinished"}


def median(values):
    values = sorted(v for v in values if v is not None)
    return values[len(values) // 2] if values else None


def model_calls(events):
    """One row per model call: label, tokens, cost, latency, sampling."""
    out, pending = [], None
    for e in events:
        if e.get("kind") == "model_call":
            pending = e
        elif e.get("kind") == "model_reply" and pending is not None:
            out.append({"label": pending.get("label", "?").split(" (retry")[0],
                        "retry": "(retry" in pending.get("label", ""),
                        "prompt_chars": len(pending.get("prompt") or ""),
                        "input_tokens": e.get("input_tokens") or 0,
                        "output_tokens": e.get("output_tokens") or 0,
                        "cost_usd": e.get("cost_usd") or 0.0,
                        "latency_ms": e.get("latency_ms"),
                        "temperature": pending.get("temperature")})
            pending = None
    return out


def step_costs(events):
    """Repairs and failed verdicts per tool name, in build order."""
    out, current = defaultdict(lambda: {"repairs": 0, "failed": 0, "saved": False}), "(direct)"
    for e in events:
        k = e.get("kind")
        if k == "step":
            current = e.get("name") or "?"
        elif k == "repair":
            out[current]["repairs"] += 1
        elif k == "verdict" and not e.get("ok"):
            out[current]["failed"] += 1
        elif k == "promoted":
            out[e.get("name") or current]["saved"] = True
    return dict(out)


def review(sessions, calls=(), requests=()):
    """The report for a list of ``(session_id, events)``."""
    outcomes, by_label = Counter(), defaultdict(list)
    classes, hints, fixes, signatures = Counter(), Counter(), Counter(), Counter()
    examples, steps, logged = {}, defaultdict(lambda: Counter()), Counter()
    wasted = retries = bad = waits = evals = eval_fail = 0
    eval_ms = []
    for sid, events in sessions:
        meta = session_meta(events)
        outcomes[meta["outcome"]] += 1
        for call in model_calls(events):
            by_label[call["label"]].append(call)
        for name, c in step_costs(events).items():
            steps[name]["repairs"] += c["repairs"]
            steps[name]["failed"] += c["failed"]
            steps[name]["sessions"] += 1
            steps[name]["saved"] += 1 if c["saved"] else 0
        for e in events:
            k = e.get("kind")
            logged[k] += 1
            if k == "verdict" and not e.get("ok"):
                classes[e.get("class") or "UNCLASSIFIED"] += 1
                detail = e.get("detail") or e.get("reason") or ""
                sig = orc.error_signature(detail)
                signatures[sig] += 1
                examples.setdefault(sig, {"session": sid, "detail": detail[:260]})
                for key, _ in orc.lisp_hints(detail):
                    hints[key] += 1
            elif k == "auto_fix":
                fixes.update(e.get("fixes") or [])
            elif k == "json_retry":
                retries += 1
            elif k == "bad_reply":
                bad += 1
            elif k == "model_wait":
                waits += 1
            elif k == "repl":
                evals += 1
                eval_fail += 0 if e.get("ok") else 1
                eval_ms.append(e.get("elapsed_ms"))
            elif k == "summary":
                wasted += (e.get("efficiency") or {}).get("wasted_tokens") or 0
    labels = []
    for label, rows in by_label.items():
        labels.append({
            "label": label, "calls": len(rows), "retries": sum(1 for r in rows if r["retry"]),
            "input_tokens": sum(r["input_tokens"] for r in rows),
            "output_tokens": sum(r["output_tokens"] for r in rows),
            "cost_usd": round(sum(r["cost_usd"] for r in rows), 5),
            "median_prompt_chars": median(r["prompt_chars"] for r in rows),
            "median_latency_ms": median(r["latency_ms"] for r in rows)})
    labels.sort(key=lambda r: -(r["input_tokens"] + r["output_tokens"]))
    total_tokens = sum(r["input_tokens"] + r["output_tokens"] for r in labels)
    request_errors = Counter(r["error"][:160] for r in requests if r.get("error"))
    return {
        "sessions": sum(outcomes.values()), "outcomes": dict(outcomes),
        "model_calls": sum(r["calls"] for r in labels), "tokens": total_tokens,
        "cost_usd": round(sum(r["cost_usd"] for r in labels), 5),
        "wasted_tokens": wasted,
        "by_call_type": labels,
        "failure_classes": dict(classes.most_common()),
        "lisp_slips": dict(hints.most_common()),
        "automatic_fixes": dict(fixes.most_common()),
        "invalid_json_retries": retries, "unreadable_replies": bad, "api_waits": waits,
        "lisp_evals": {"count": evals, "failed": eval_fail, "median_ms": median(eval_ms)},
        "costliest_tools": [dict(name=n, **dict(c)) for n, c in
                            sorted(steps.items(), key=lambda kv: -kv[1]["repairs"])[:8]
                            if c["repairs"]],
        "top_errors": [{"signature": s, "count": n, **examples[s]}
                       for s, n in signatures.most_common(8)],
        "typed_calls": {"count": len(calls),
                        "failed": sum(1 for c in calls if not c.get("ok"))},
        "mounted_requests": {"count": len(requests),
                             "failed": sum(1 for r in requests if r.get("error")),
                             "median_ms": median(r.get("ms") for r in requests),
                             "errors": dict(request_errors.most_common(5))},
        "logged_event_kinds": dict(logged.most_common()),
    }


def coverage(report):
    """What the logs do and do not capture, so gaps are visible, not assumed."""
    kinds = report["logged_event_kinds"]
    need = {"model_call": "prompts sent to the model",
            "model_reply": "model replies with tokens, cost and latency",
            "system_prompt": "system prompt text (logged once per session)",
            "repl": "Lisp evaluations with code, value and error",
            "verdict": "test verdicts with failure class",
            "auto_fix": "automatic repairs applied to a reply",
            "summary": "per-session effort summary"}
    return {text: kinds.get(kind, 0) for kind, text in need.items()}


def render(report):
    """Plain-text report."""
    lines = ["%d sessions %s; %d model calls, %d tokens, $%.4f; %d tokens wasted on failed attempts"
             % (report["sessions"], report["outcomes"], report["model_calls"],
                report["tokens"], report["cost_usd"], report["wasted_tokens"]), "",
             "Tokens by call type:"]
    for r in report["by_call_type"]:
        lines.append("  %-16s %3d calls  %6d in  %6d out  $%.4f  median prompt %s chars, %s ms"
                     % (r["label"], r["calls"], r["input_tokens"], r["output_tokens"],
                        r["cost_usd"], r["median_prompt_chars"], r["median_latency_ms"]))
    for title, key in (("Failure classes", "failure_classes"), ("Lisp slips", "lisp_slips"),
                       ("Automatic fixes applied", "automatic_fixes")):
        lines += ["", title + ": " + (", ".join("%s %d" % kv for kv in report[key].items())
                                      or "none")]
    lines += ["", "Invalid-JSON retries %d, unreadable replies %d, API waits %d"
              % (report["invalid_json_retries"], report["unreadable_replies"], report["api_waits"]),
              "Lisp evaluations %(count)d (%(failed)d failed), median %(median_ms)s ms"
              % report["lisp_evals"], "", "Tools that cost the most repairs:"]
    lines += ["  %-20s %d repairs, %d failed verdicts over %d session(s)"
              % (t["name"], t["repairs"], t["failed"], t["sessions"])
              for t in report["costliest_tools"]] or ["  none"]
    lines += ["", "Most frequent errors:"]
    lines += ["  %dx %s\n     e.g. %s" % (e["count"], e["signature"], e["detail"])
              for e in report["top_errors"]] or ["  none"]
    lines += ["", "Typed REPL calls %(count)d (%(failed)d failed)" % report["typed_calls"],
              "Mounted-app requests %(count)d (%(failed)d failed), median %(median_ms)s ms"
              % report["mounted_requests"], "", "Log coverage:"]
    lines += ["  %-52s %d" % (text, n) for text, n in coverage(report).items()]
    return "\n".join(lines)


def render_learning(rep):
    """Plain-text view of ``LessonStore.report()``."""
    lines = ["Lessons (slips the model is warned about):"]
    for row in rep["lessons"]:
        rate = "n/a" if row["recurrence_rate"] is None else "%d%%" % (100 * row["recurrence_rate"])
        lines.append("  %-20s seen %3d  shown in %2d run(s), recurred anyway in %2d (%s)%s"
                     % (row["lesson"], row["count"], row["shown_in_runs"],
                        row["recurred_after_shown"], rate,
                        "  <- warning is not working" if row["lesson"] in rep["ineffective"] else ""))
    lines += ["", "Repeated errors with NO lesson yet (write a hint or a harness fix):"]
    lines += ["  %dx %s\n     e.g. %s" % (e["count"], e["signature"], e["example"][:200])
              for e in rep["unhandled_errors"]] or ["  none"]
    lines += ["", "Model slips the harness fixed silently: "
              + (", ".join("%s %d" % kv for kv in rep["harness_fixes"].items()) or "none"),
              "Harness events: "
              + (", ".join("%s %d" % kv for kv in rep["harness_events"].items()) or "none")]
    return "\n".join(lines)


def load_sessions(agent_dir, mode=None, project=None, since=0, main_only=True):
    """``[(session_id, events)]`` matching the filters, oldest first."""
    out = []
    for path in sorted((Path(agent_dir) / "sessions").glob("*.jsonl"),
                       key=lambda p: p.stat().st_mtime):
        events = read_jsonl(path)
        meta = session_meta(events)
        if meta["t"] < since or (mode and meta["mode"] != mode) or \
                (project and meta["project"] != project) or \
                (main_only and meta["arm"] != "main"):
            continue
        out.append((path.stem, events))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=("demo", "live"), default=None)
    ap.add_argument("--project", default=None)
    ap.add_argument("--hours", type=float, default=None, help="only the last N hours")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--learning", action="store_true",
                    help="show what the learning layer has recorded, then exit")
    args = ap.parse_args(argv)
    if args.learning:
        rep = orc.LessonStore(ag.AGENT_DIR / "lessons.json").report()
        print(json.dumps(rep, indent=2) if args.json else render_learning(rep))
        return 0
    since = time.time() - args.hours * 3600 if args.hours else 0
    sessions = load_sessions(ag.AGENT_DIR, args.mode, args.project, since)
    calls = [c for c in read_jsonl(ag.AGENT_DIR / "calls.jsonl")
             if c.get("t", 0) >= since and (not args.project or c.get("project") == args.project)]
    requests = []
    for path in (ag.AGENT_DIR / "apps").glob("*/requests.jsonl"):
        if not args.project or path.parent.name == args.project:
            requests += [r for r in read_jsonl(path) if r.get("t", 0) >= since]
    report = review(sessions, calls, requests)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
