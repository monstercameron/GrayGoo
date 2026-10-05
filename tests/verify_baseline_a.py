"""Independent verification of Baseline A numbers from raw artifacts.

Reads artifacts/baseline-a/raw/A-*.json and stripped/A-*.json directly
(stdlib only; NO imports from benchmarks/) and recomputes every number
claimed in documents/baseline-a.md. Prints a match/mismatch verdict per
claim and writes it to artifacts/demo-e2e/verification.json.

Usage: uv run python tests/verify_baseline_a.py
"""

import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "artifacts", "baseline-a", "raw")
STRIPPED_DIR = os.path.join(ROOT, "artifacts", "baseline-a", "stripped")
OUT_PATH = os.path.join(ROOT, "artifacts", "demo-e2e", "verification.json")

# Every claim below is transcribed from documents/baseline-a.md.
DOC = {
    "raw": {
        "tasks": (22, 35), "rate_pct": 62.9,
        "exposure": (13, 18, 72.2), "transfer": (2, 8, 25.0),
        "adversarial": (4, 5, 80.0), "equivalent-transform": (3, 4, 75.0),
        "fail_ids": ["A-ADV-02", "A-EQV-01", "A-EXP-05", "A-EXP-06",
                     "A-EXP-08", "A-EXP-12", "A-EXP-16", "A-TRN-02",
                     "A-TRN-03", "A-TRN-04", "A-TRN-06", "A-TRN-07",
                     "A-TRN-08"],
        "calls": 72, "in_tok": 7188, "out_tok": 2078, "cost": 0.010209,
        "mean_calls": 2.057, "mean_in": 205.4, "mean_out": 59.4,
        "mean_total": 264.7, "mean_lat": 1223.3, "mean_cost": 0.000292,
        "finish": {"stop": 71, "length": 1},
    },
    "stripped": {
        "tasks": (29, 35), "rate_pct": 82.9,
        "exposure": (16, 18, 88.9), "transfer": (5, 8, 62.5),
        "adversarial": (4, 5, 80.0), "equivalent-transform": (4, 4, 100.0),
        "fail_ids": ["A-ADV-02", "A-EXP-08", "A-EXP-16", "A-TRN-02",
                     "A-TRN-06", "A-TRN-08"],
        "calls": 72, "in_tok": 7188, "out_tok": 2078, "cost": 0.010209,
        "mean_calls": 2.057, "mean_in": 205.4, "mean_out": 59.4,
        "mean_total": 264.7, "mean_lat": 1080.9, "mean_cost": 0.000292,
        "finish": {"stop": 71, "length": 1},
    },
    "flipped": ["A-EQV-01", "A-EXP-05", "A-EXP-06", "A-EXP-12", "A-TRN-03",
                "A-TRN-04", "A-TRN-07"],
    "checks_total": 72, "checks_json": 46, "checks_exact": 26,
    "fenced_total": 15, "fenced_json": 15, "fenced_exact": 0,
    "paired_out_tok_tasks": 35,
    "lane_calls": 144, "lane_in": 14376, "lane_out": 4156,
    "lane_lat_s": 80.6, "lane_cost": 0.020418,
    "lat_min": 172.0, "lat_max": 3281.0,
    "unique_request_ids": 144,
}


def load_condition(directory):
    """Load per-task records from one condition dir. Returns {task_id: task}."""
    tasks = {}
    paths = sorted(glob.glob(os.path.join(directory, "A-*.json")))
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        for task in blob["summary"]["tasks"]:
            tasks[task["id"]] = task
    return tasks


def main():
    for _label, _directory in (("raw", RAW_DIR), ("stripped", STRIPPED_DIR)):
        if not os.path.isdir(_directory):
            print("SKIP: baseline-a artifacts absent (%s); nothing to verify"
                  % _directory)
            return 0
    results = []  # (claim, expected, actual, ok)

    def check(claim, expected, actual, tol=0.0):
        if isinstance(expected, float) or isinstance(actual, float) or tol:
            ok = abs(float(expected) - float(actual)) <= tol
        else:
            ok = expected == actual
        results.append({"claim": claim, "expected": expected,
                        "actual": actual, "match": bool(ok)})

    raw = load_condition(RAW_DIR)
    stripped = load_condition(STRIPPED_DIR)
    check("raw file count", 35, len(raw))
    check("stripped file count", 35, len(stripped))
    check("same task ids both conditions", sorted(raw), sorted(stripped))

    all_request_ids = []
    for name, tasks, doc in (("raw", raw, DOC["raw"]),
                             ("stripped", stripped, DOC["stripped"])):
        ids = sorted(tasks)
        passed = [i for i in ids if tasks[i]["passed"]]
        failed = [i for i in ids if not tasks[i]["passed"]]
        check("%s tasks passed/total" % name, list(doc["tasks"]),
              [len(passed), len(ids)])
        check("%s success rate %%" % name, doc["rate_pct"],
              round(100.0 * len(passed) / len(ids), 1), tol=0.051)
        for split in ("exposure", "transfer", "adversarial",
                      "equivalent-transform"):
            s_ids = [i for i in ids if tasks[i]["split"] == split]
            s_pass = [i for i in s_ids if tasks[i]["passed"]]
            exp_p, exp_t, exp_r = doc[split]
            check("%s %s passed/total" % (name, split), [exp_p, exp_t],
                  [len(s_pass), len(s_ids)])
            check("%s %s rate %%" % (name, split), exp_r,
                  round(100.0 * len(s_pass) / len(s_ids), 1)
                  if s_ids else 0.0, tol=0.051)
        check("%s fail ids" % name, sorted(doc["fail_ids"]), sorted(failed))

        calls = sum(t["totals"]["calls"] for t in tasks.values())
        in_tok = sum(t["totals"]["input_tokens"] for t in tasks.values())
        out_tok = sum(t["totals"]["output_tokens"] for t in tasks.values())
        lat = sum(t["totals"]["latency_ms"] for t in tasks.values())
        cost = sum(t["totals"]["cost_usd"] for t in tasks.values())
        check("%s total calls" % name, doc["calls"], calls)
        check("%s total input tokens" % name, doc["in_tok"], in_tok)
        check("%s total output tokens" % name, doc["out_tok"], out_tok)
        check("%s total cost" % name, doc["cost"], round(cost, 6), tol=1e-6)
        n = len(ids)
        check("%s mean calls/task" % name, doc["mean_calls"],
              round(calls / n, 3), tol=0.00051)
        check("%s mean in/task" % name, doc["mean_in"],
              round(in_tok / n, 1), tol=0.051)
        check("%s mean out/task" % name, doc["mean_out"],
              round(out_tok / n, 1), tol=0.051)
        check("%s mean total/task" % name, doc["mean_total"],
              round((in_tok + out_tok) / n, 1), tol=0.051)
        check("%s mean latency/task" % name, doc["mean_lat"],
              round(lat / n, 1), tol=0.051)
        check("%s mean cost/task" % name, doc["mean_cost"],
              round(cost / n, 6), tol=1e-6)

        finish = {}
        for t in tasks.values():
            for u in t["usage"]:
                finish[u["finish_reason"]] = finish.get(u["finish_reason"], 0) + 1
                all_request_ids.append(u["request_id"])
        check("%s finish reasons" % name, doc["finish"], finish)

    # Cross-condition claims.
    flipped = sorted(i for i in raw if not raw[i]["passed"]
                     and stripped[i]["passed"])
    check("flipped raw-FAIL->stripped-PASS", sorted(DOC["flipped"]), flipped)
    regressed = sorted(i for i in raw if raw[i]["passed"]
                       and not stripped[i]["passed"])
    check("stripped regressions (doc: none)", [], regressed)

    checks_total = checks_json = checks_exact = 0
    fenced = fenced_json = fenced_exact = 0
    for t in stripped.values():
        for c in t["checks"]:
            checks_total += 1
            if c["compare"] == "json":
                checks_json += 1
            elif c["compare"] == "exact":
                checks_exact += 1
            if c["stripped"]:
                fenced += 1
                if c["compare"] == "json":
                    fenced_json += 1
                elif c["compare"] == "exact":
                    fenced_exact += 1
    check("checks total", DOC["checks_total"], checks_total)
    check("checks json", DOC["checks_json"], checks_json)
    check("checks exact", DOC["checks_exact"], checks_exact)
    check("fenced checks", DOC["fenced_total"], fenced)
    check("fenced json", DOC["fenced_json"], fenced_json)
    check("fenced exact", DOC["fenced_exact"], fenced_exact)
    raw_fenced = sum(1 for t in raw.values() for c in t["checks"]
                     if c["stripped"])
    check("raw fenced flags (doc: 0)", 0, raw_fenced)

    paired = sum(1 for i in raw
                 if raw[i]["totals"]["output_tokens"]
                 == stripped[i]["totals"]["output_tokens"])
    check("paired per-task output tokens", DOC["paired_out_tok_tasks"], paired)

    lane_calls = sum(t["totals"]["calls"] for t in list(raw.values())
                     + list(stripped.values()))
    lane_in = sum(t["totals"]["input_tokens"] for t in list(raw.values())
                  + list(stripped.values()))
    lane_out = sum(t["totals"]["output_tokens"] for t in list(raw.values())
                   + list(stripped.values()))
    lane_lat = sum(t["totals"]["latency_ms"] for t in list(raw.values())
                   + list(stripped.values()))
    lane_cost = sum(t["totals"]["cost_usd"] for t in list(raw.values())
                    + list(stripped.values()))
    check("lane calls", DOC["lane_calls"], lane_calls)
    check("lane input tokens", DOC["lane_in"], lane_in)
    check("lane output tokens", DOC["lane_out"], lane_out)
    check("lane latency s", DOC["lane_lat_s"], round(lane_lat / 1000.0, 1),
          tol=0.051)
    check("lane cost", DOC["lane_cost"], round(lane_cost, 6), tol=1e-6)

    lats = [u["latency_ms"] for t in list(raw.values()) + list(stripped.values())
            for u in t["usage"]]
    check("per-call latency min", DOC["lat_min"], min(lats))
    check("per-call latency max", DOC["lat_max"], max(lats))
    check("unique request ids across lane", DOC["unique_request_ids"],
          len(set(all_request_ids)))
    check("request id rows total", 144, len(all_request_ids))

    # A-TRN-08 truncation detail: check 0 length-capped in both conditions.
    for name, tasks in (("raw", raw), ("stripped", stripped)):
        reasons = [u["finish_reason"] for u in tasks["A-TRN-08"]["usage"]]
        check("%s A-TRN-08 check0 length" % name, "length", reasons[0])

    mismatches = [r for r in results if not r["match"]]
    for r in results:
        print("%s %-42s expected=%r actual=%r"
              % ("MATCH   " if r["match"] else "MISMATCH", r["claim"],
                 r["expected"], r["actual"]))
    print("---")
    print("%d/%d claims match" % (len(results) - len(mismatches),
                                  len(results)))

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump({"claims": results,
                   "matched": len(results) - len(mismatches),
                   "total": len(results),
                   "verdict": "MATCH" if not mismatches else "MISMATCH"},
                  fh, indent=2, sort_keys=True)
    print("wrote %s" % OUT_PATH)
    return 0 if not mismatches else 1


if __name__ == "__main__":
    sys.exit(main())
