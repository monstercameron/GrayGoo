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


def _stored_keys(patch_mem):
    keys = set()
    for patch in patch_mem.store.list_patches():
        candidate = patch.get("candidate", {}) or {}
        if not isinstance(candidate, dict):
            continue
        keys.add((patch.get("task_id"), candidate.get("check")))
    return keys


def run_round(round_no, tasks, patch_mem, make_inner, out_dir):
    """Run one cumulative round; return the round metrics dict."""
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
                    patch = (patch_mem.store.get_patch(patch_id)
                             or {"patch_id": patch_id})
                    patch_mem.record_reuse(patch, task,
                                           helped=passed, hit=True)
            elif passed:
                if (task["id"], j) not in _stored_keys(patch_mem):
                    output = inner.outputs.get((task["id"], j), "")
                    patch = patch_mem.add_success(
                        task, j, check["input"], output)
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


def run_stream(tasks, out_root, make_inner, rounds=DEFAULT_ROUNDS):
    """Run expanding cumulative rounds; write artifacts; return payload."""
    out_root = Path(out_root)
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    patch_mem = patch_mem_mod.PatchMemory(str(out_root / "memory"))
    round_rows = []
    for i, size in enumerate(rounds, 1):
        subset = tasks[:max(0, min(size, len(tasks)))]
        row = run_round(i, subset, patch_mem, make_inner, out_root)
        round_rows.append(row)
        with open(out_root / ("round-%d.json" % i), "w",
                  encoding="utf-8") as handle:
            json.dump(row, handle, indent=2)
        print("round %d: %d/%d passed, %d hits, lib=%d, "
              "calls/task=%.3f, tokens/task=%.1f" % (
                  i, row["passed"], row["tasks"],
                  row["fast_path_hits"], row["library_size"],
                  row["calls_per_task"], row["tokens_per_task"]),
              flush=True)
    payload = {"experiment": "repeat-stream",
               "rounds": [r for r in rounds],
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
        str(r) for r in DEFAULT_ROUNDS))
    args = parser.parse_args(argv)

    tasks = sorted(
        (t for t in runner.load_tasks(Path(args.tasks))
         if t.get("split") == "exposure"),
        key=lambda t: t["id"])
    rounds = tuple(int(r) for r in args.rounds.split(",") if r.strip())

    def make_inner():
        if args.adapter == "stub":
            if not args.recorded:
                raise RuntimeError(
                    "--recorded FILE is required with --adapter stub")
            return runner.StubAdapter(Path(args.recorded))
        return runner.CerebrasAdapter(max_tokens=args.max_tokens)

    run_stream(tasks, args.artifacts, make_inner, rounds=rounds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
