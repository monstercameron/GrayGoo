"""Repeat the live guided run and record shipped evidence (PAID, capped).

Runs the 7 guided prompts plus the 10 held-out prompts ``--runs`` times on
the live model, each run in an isolated tool registry. Every prompt is also
answered by a no-memory arm (an empty throwaway registry), so savings and
accuracy are measured on both sides, including on the held-out prompts.

Output: ``dashboard/evidence/live-repeat-runs.json`` (rewritten after every
run, so a stopped job still leaves usable partial evidence) and a tailable
progress log at ``artifacts/agent/evidence/progress.log``.

    uv run python evidence_run.py --runs 3 --cap 0.14

Cost guard: stops starting new prompts once the measured spend reaches
``--cap`` USD and marks the output ``partial``.
"""

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import agent_session as ag

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dashboard" / "evidence" / "live-repeat-runs.json"
WORK = ROOT / "artifacts" / "agent" / "evidence"

GUIDED = [
    ("write a function that squares a number, then square 12", "144"),
    ("sum of squares of the list (3 4 5)", "50"),
    ("write a function that cubes a number, then cube 4", "64"),
    ("mean of squares of the list (2 4 6 8)", "30"),
    ("sum of squares of the list (3 4 5), again", "50"),
    ("mean of squares of the list (2 4 6 8), again", "30"),
    ("sum of squares of the list (3 4 5)", "50"),
]


def log(handle, text):
    line = "%s %s" % (time.strftime("%H:%M:%S"), text)
    print(line, flush=True)
    handle.write(line + "\n")
    handle.flush()


def one(prompt, expected, oracle, registry, logdir, arm="main", pair=None):
    sess = ag.Session(prompt, ag.live_generate, registry=registry, mode="live",
                      arm=arm, pair=pair, expected=expected, oracle=oracle)
    sess._log_path = logdir / ("%s.jsonl" % sess.id)
    sess.run()
    return sess


TEMPERATURE = 0.0
HELD = 10


def write(runs, partial, spent, label_runs):
    sampled = TEMPERATURE > 0
    temp_note = (
        "Temperature %.1f, so runs genuinely differ: the ranges are real "
        "run-to-run sampling variance." % TEMPERATURE if sampled else
        "Temperature 0, so runs are near-identical: repetition shows stability, "
        "not sampling variance.")
    snap = {
        "label": "Repeated live runs (%d×%s, real tokens)" % (
            label_runs, ", T=%.1f" % TEMPERATURE if sampled else ""),
        "note": ("Cerebras Qwen 3.8 27B, %s: %d independent runs of the 7 guided "
                 "prompts plus %d held-out prompts (answers from independent "
                 "Python). Every prompt, including the held-out ones, is also "
                 "answered with an empty registry. %s Measured spend $%.3f."
                 % (time.strftime("%Y-%m-%d"), label_runs, HELD, temp_note, spent)),
        "mode": "live", "estimated": False, "partial": partial,
        "temperature": TEMPERATURE,
        "rows": runs[0]["rows"] if runs else [],
        "runs": runs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, indent=1), encoding="utf-8")
    tmp.replace(OUT)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--cap", type=float, default=0.14, help="stop at this USD spend")
    ap.add_argument("--temperature", type=float, default=0.0,
                    help="sampling temperature for ordinary calls (0 = near-identical runs; "
                         "use 0.7 to measure real run-to-run variance)")
    ap.add_argument("--held", type=int, default=10, help="held-out prompts per run (max 20)")
    ap.add_argument("--out", default=None, help="output JSON name inside dashboard/evidence/")
    args = ap.parse_args(argv)
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    status = ag.live_status()
    if not status["available"]:
        print("live mode unavailable:", status["reason"])
        return 2
    progress = open(WORK / "progress.log", "w", encoding="utf-8")
    spent, runs, stopped = 0.0, [], False
    ag.BASE_TEMPERATURE = args.temperature
    held = ag.heldout_tasks(args.held)
    global OUT, TEMPERATURE, HELD
    TEMPERATURE, HELD = args.temperature, args.held
    if args.out:
        OUT = ROOT / "dashboard" / "evidence" / args.out
    for r in range(1, args.runs + 1):
        logdir = WORK / ("run%d" % r)
        logdir.mkdir()
        registry = ag.ToolRegistry(logdir / "tools.json")
        plan = [(p, e, None) for p, e in GUIDED] + \
               [(t["prompt"], t["expected"], t["oracle"]) for t in held]
        for i, (prompt, expected, oracle) in enumerate(plan, 1):
            if spent >= args.cap:
                stopped = True
                log(progress, "COST CAP reached ($%.4f): stopping" % spent)
                break
            main_s = one(prompt, expected, oracle, registry, logdir)
            scratch = tempfile.mkdtemp(prefix="gg-ev-")
            try:
                twin = one(prompt, expected, oracle,
                           ag.ToolRegistry(Path(scratch) / "t.json"), logdir,
                           arm="nomem", pair=main_s.id)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            spent += main_s.cost_usd + twin.cost_usd
            log(progress, "run %d/%d prompt %d/%d %s | main %s %d tok | no-memory %s %d tok | spent $%.4f" % (
                r, args.runs, i, len(plan), "HELD" if oracle else "GUID",
                main_s.state, main_s.input_tokens + main_s.output_tokens,
                twin.state, twin.input_tokens + twin.output_tokens, spent))
        paths = sorted(logdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        rows = [x for x in (ag.row_from_log(p) for p in paths) if x]
        runs.append({"run": r, "rows": rows})
        write(runs, stopped or r < args.runs, spent, len(runs))
        log(progress, "run %d saved (%d rows)" % (r, len(rows)))
        if stopped:
            break
    write(runs, stopped, spent, len(runs))
    log(progress, "DONE runs=%d partial=%s spent=$%.4f" % (len(runs), stopped, spent))
    progress.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
