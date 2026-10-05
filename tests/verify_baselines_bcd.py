"""Independent verification of Baselines B/C/D from raw artifacts.

Reads artifacts/baselines-bcd/{b,c,d}/A-*.json directly (stdlib only;
NO imports from benchmarks/) and recomputes every headline claim in
documents/baselines-bcd.md: per-baseline pass counts, split
breakdowns, fail sets, and Phase-1 exposure output-token identity
against Baseline A stripped. Prints match/mismatch per claim.

Usage: uv run python tests/verify_baselines_bcd.py
"""

import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BCD = os.path.join(ROOT, "artifacts", "baselines-bcd")
A_STRIPPED = os.path.join(ROOT, "artifacts", "baseline-a", "stripped")

# Headline claims transcribed from documents/baselines-bcd.md.
DOC = {
    "b": {"passed": 30, "total": 35,
          "fail": {"A-EXP-08", "A-EXP-16", "A-TRN-02", "A-TRN-06",
                   "A-ADV-02"},
          "splits": {"exposure": (16, 18), "transfer": (6, 8),
                     "adversarial": (4, 5), "equivalent-transform": (4, 4)}},
    "c": {"passed": 29, "total": 35,
          "fail": {"A-ADV-02", "A-EXP-08", "A-EXP-16", "A-TRN-02",
                   "A-TRN-06", "A-TRN-08"},
          "splits": {"exposure": (16, 18), "transfer": (5, 8),
                     "adversarial": (4, 5), "equivalent-transform": (4, 4)}},
    "d": {"passed": 30, "total": 35,
          "fail": {"A-EXP-08", "A-EXP-16", "A-TRN-02", "A-TRN-06",
                   "A-ADV-02"},
          "splits": {"exposure": (16, 18), "transfer": (6, 8),
                     "adversarial": (4, 5), "equivalent-transform": (4, 4)}},
}

failures = []


def check(name, expected, actual):
    ok = expected == actual
    print("%-7s %-42s expected=%r actual=%r"
          % ("MATCH" if ok else "MISMATCH", name, expected, actual))
    if not ok:
        failures.append(name)


def task_rows(path):
    """Return [(task_id, split, passed, output_tokens)] from a file."""
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    rows = []
    for task in doc["summary"]["tasks"]:
        out = sum(u.get("output_tokens", 0)
                  for u in task.get("usage", []) or [])
        rows.append((task["id"], task.get("split"), bool(task["passed"]),
                     out))
    return rows


def load_dir(directory):
    rows = {}
    for path in sorted(glob.glob(os.path.join(directory, "A-*.json"))):
        for task_id, split, passed, out in task_rows(path):
            rows[task_id] = (split, passed, out)
    return rows


def main():
    a_rows = load_dir(A_STRIPPED)
    for base in ("b", "c", "d"):
        rows = load_dir(os.path.join(BCD, base))
        claim = DOC[base]
        passed = sorted(t for t, (_, p, _) in rows.items() if p)
        failed = sorted(t for t, (_, p, _) in rows.items() if not p)
        check("%s tasks" % base, (claim["passed"], claim["total"]),
              (len(passed), len(rows)))
        check("%s fail set" % base, sorted(claim["fail"]), failed)
        for split, (exp_p, exp_t) in claim["splits"].items():
            got = [(t, p) for t, (s, p, _) in rows.items() if s == split]
            check("%s split %s" % (base, split), (exp_p, exp_t),
                  (sum(1 for _, p in got if p), len(got)))
        with open(os.path.join(BCD, "%s-summary.json" % base),
                  encoding="utf-8") as handle:
            summary = json.load(handle)
        check("%s summary tasks_passed" % base, len(passed),
              summary["tasks_passed"])
        # Phase-1 exposure outputs must be bit-identical to A (memory
        # cannot affect Phase 1): compare per-task output tokens.
        mism = [t for t, (s, _, out) in rows.items()
                if s == "exposure" and a_rows[t][2] != out]
        check("%s exposure output-tokens identical to A" % base, [], mism)
    print("---")
    if failures:
        print("%d MISMATCHES: %s" % (len(failures), failures))
        return 1
    print("all baseline B/C/D claims match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
