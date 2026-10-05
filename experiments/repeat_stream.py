"""Repeated-input stream: reuse learning over expanding rounds.

Runs Family-A exposure tasks in expanding cumulative rounds (default
6 -> 12 -> 18 tasks) through the input-gated patch fast-path
(`matched_rerun.FastPathAdapter`). Round 1 solves from scratch and
stores verified check exemplars; later rounds repeat earlier tasks,
so the fast-path SHOULD fire on the repeated inputs with zero calls.

This is the experiment the gate doc calls for: held-out transfer is
novel by design (fast-path fires 0/32 there), so reuse-over-time
(#4), growth shape (#6), and calls/task (#2) can only move on a
stream with repeated inputs.

Storage policy (documented, load-bearing for #6): check-level passes
stored with dedup by (task_id, check) — one exemplar per check, so
re-solving a repeated check adds nothing. A patch earns its seed
confidence (helped=1) by passing its own check at store time; every
later fast-path hit records its outcome (helped/hurt), so a wrong
recorded output self-revokes after one hurt. Assisted (miss) checks
solve from scratch (empty injection block): misses cost exactly
Baseline-A-like calls.

Live usage (<= ~60 calls):
    uv run python experiments/repeat_stream.py --adapter cerebras

Offline flow check (no live calls):
    uv run python experiments/repeat_stream.py --adapter stub \\
        --recorded benchmarks/family-a/recorded/stub_all_pass.json

Outputs (all under artifacts/):
    artifacts/repeat-stream/round-N.json   per-round metrics + task files
    artifacts/repeat-stream/summary.json   round table + trends
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "benchmarks"))
sys.path.insert(0, str(_ROOT))

import runner  # noqa: E402
import patch_memory_adapter as patch_mem_mod  # noqa: E402
import consolidate as consolidate_mod  # noqa: E402
from experiments.matched_rerun import FastPathAdapter  # noqa: E402

DEFAULT_ROUNDS = (6, 12, 18)


class RecordingAdapter(runner.ModelAdapter):
    """Delegate solves to inner, recording outputs per (task, check)."""

    name = "recording"

    def __init__(self, inner):
        self.inner = inner
        self.name = getattr(inner, "name", "?")
        self.max_tokens = getattr(inner, "max_tokens", None)
        self.outputs = {}

    @property
    def history(self):
        return getattr(self.inner, "history", None)

    def solve(self, task, check_index, check_input):
        output = self.inner.solve(task, check_index, check_input)
        self.outputs[(task.get("id", "?"), check_index)] = output
        return output


def _empty_block(task):
    return "", {"injected_chars": 0, "injected_est_tokens": 0,
                "retrieval": "stream-from-scratch"}


def run_round(round_no, tasks, patch_mem, make_inner, out_dir,
              stored_rounds=None, hit_patches=None):
    """Run one round; return the round metrics dict.

    ``stored_rounds`` (patch_id -> round stored) and ``hit_patches``
    (patch_ids hit at least once) are updated in place for
    retire-unused decisions across rounds.
    """
    history = patch_mem.reuse_history()
    inner = RecordingAdapter(make_inner())
    adapter = FastPathAdapter(inner, patch_mem, history, _empty_block,
                              "stream")
    summary = runner.run(tasks, adapter, strip_fences=True)
    by_id = {t["id"]: t for t in summary["tasks"]}

    stored = 0
    hits = 0
    hit_helped = 0
    for task in tasks:
        task_summary = by_id[task["id"]]
        checks = task_summary.get("checks", [])
        for j, check in enumerate(task["checks"]):
            info = adapter.checks.get((task["id"], j), {})
            passed = (j < len(checks)
                      and bool(checks[j].get("passed")))
            if info.get("fast_path"):
                hits += 1
                hit_helped += 1 if passed else 0
                patch_id = info.get("patch_id")
                if patch_id:
                    if hit_patches is not None:
                        hit_patches.add(patch_id)
                    patch = (patch_mem.store.get_patch(patch_id)
                             or {"patch_id": patch_id})
                    patch_mem.record_reuse(patch, task,
                                           helped=passed, hit=True)
            elif passed:
                if not patch_mem.has_check(task["id"], j):
                    output = inner.outputs.get((task["id"], j), "")
                    patch = patch_mem.add_success(
                        task, j, check["input"], output)
                    if stored_rounds is not None:
                        stored_rounds[patch.get("patch_id")] = round_no
                    patch_mem.record_reuse(
                        patch, task, helped=True, seeded=True)
                    stored += 1
    usage = summary.get("usage", {})
    n_tasks = len(tasks)
    return {
        "round": round_no,
        "tasks": n_tasks,
        "passed": sum(1 for t in summary["tasks"] if t["passed"]),
        "fail_ids": sorted(t["id"] for t in summary["tasks"]
                           if not t["passed"]),
        "fast_path_hits": hits,
        "assisted_checks": adapter.assisted_calls,
        "hit_helped": hit_helped,
        "stored": stored,
        "library_size": patch_mem.capability_count(),
        "calls": usage.get("calls", 0),
        "calls_per_task": (usage.get("calls", 0) / n_tasks
                           if n_tasks else 0.0),
        "tokens_per_task": (usage.get("total_tokens", 0) / n_tasks
                            if n_tasks else 0.0),
        "cost_usd": usage.get("cost_usd", 0.0),
    }


def _round_subset(tasks, spec):
    """Task slice for a round spec: int N = prefix 1..N, "A-B" = slice."""
    if isinstance(spec, int):
        return tasks[:max(0, min(spec, len(tasks)))]
    start, _, end = str(spec).partition("-")
    lo = max(1, int(start)) - 1
    hi = min(int(end), len(tasks))
    return tasks[lo:max(lo, hi)]


def run_stream(tasks, out_root, make_inner, rounds=DEFAULT_ROUNDS,
               retire_unused_after=0):
    """Run expanding rounds; write artifacts; return payload.

    ``rounds`` items are prefix sizes (int) or "A-B" 1-based slices.
    When ``retire_unused_after`` is K>0, after round K every patch
    stored before round K with zero hits so far is retired via
    ``consolidate.retire`` (growth control under churn); the count is
    recorded on that round's row as ``"retired"``.
    """
    out_root = Path(out_root)
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    patch_mem = patch_mem_mod.PatchMemory(str(out_root / "memory"))
    stored_rounds = {}
    hit_patches = set()
    round_rows = []
    for i, spec in enumerate(rounds, 1):
        subset = _round_subset(tasks, spec)
        row = run_round(i, subset, patch_mem, make_inner, out_root,
                        stored_rounds=stored_rounds,
                        hit_patches=hit_patches)
        retired = 0
        if retire_unused_after and i == retire_unused_after:
            stale = [pid for pid, stored_in in stored_rounds.items()
                     if stored_in < i and pid not in hit_patches]
            if stale:
                report = consolidate_mod.retire(
                    patch_mem.store, stale)
                retired = report["count"]
            row["library_size"] = patch_mem.capability_count()
        row["retired"] = retired
        round_rows.append(row)
        with open(out_root / ("round-%d.json" % i), "w",
                  encoding="utf-8") as handle:
            json.dump(row, handle, indent=2)
        print("round %d: %d/%d passed, %d hits, lib=%d, retired=%d, "
              "calls/task=%.3f, tokens/task=%.1f" % (
                  i, row["passed"], row["tasks"],
                  row["fast_path_hits"], row["library_size"], retired,
                  row["calls_per_task"], row["tokens_per_task"]),
              flush=True)
    payload = {"experiment": "repeat-stream",
               "rounds": [r for r in rounds],
               "retire_unused_after": retire_unused_after,
               "round_rows": round_rows}
    with open(out_root / "summary.json", "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="Repeated-input stream")
    parser.add_argument("--tasks", default=str(
        _ROOT / "benchmarks" / "family-a"))
    parser.add_argument("--adapter", choices=("stub", "cerebras"),
                        default="stub")
    parser.add_argument("--recorded", default=None)
    parser.add_argument("--artifacts", default=str(
        _ROOT / "artifacts" / "repeat-stream"))
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--rounds", default=",".join(
        str(r) for r in DEFAULT_ROUNDS),
        help="comma list of prefix sizes (N) or 1-based slices (A-B)")
    parser.add_argument("--retire-unused-after", type=int, default=0,
                        help="retire never-hit patches after round K")
    args = parser.parse_args(argv)

    tasks = sorted(
        (t for t in runner.load_tasks(Path(args.tasks))
         if t.get("split") == "exposure"),
        key=lambda t: t["id"])

    def _parse(spec):
        spec = spec.strip()
        return int(spec) if "-" not in spec else spec

    rounds = tuple(_parse(r) for r in args.rounds.split(",")
                   if r.strip())

    def make_inner():
        if args.adapter == "stub":
            if not args.recorded:
                raise RuntimeError(
                    "--recorded FILE is required with --adapter stub")
            return runner.StubAdapter(Path(args.recorded))
        return runner.CerebrasAdapter(max_tokens=args.max_tokens)

    run_stream(tasks, args.artifacts, make_inner, rounds=rounds,
               retire_unused_after=args.retire_unused_after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
