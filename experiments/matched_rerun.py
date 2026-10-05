"""Token-matched rerun: fast-path reuse vs the two FAILed criteria.

Runs the 8 Family A transfer tasks (16 checks) under 3 conditions —
A no-memory, C patch-fast-path, D text-concise + patch-fast-path —
measuring success, tokens/task, calls/task, and fast-path hit rate.
Attacks learning-assessment criteria #2 (calls/task decreases) and #3
(tokens/task decreases), both FAIL in baselines A–D.

Baselines standard conditions (documents/baseline-a.md): stripped
scoring, temperature 0.0, reasoning off, max_tokens 128, one call per
check max (zero on fast-path hits), no retries, no repair.

Memory (all offline, zero model calls to build):
    C  Phase-1 patch snapshot copied from the baselines-bcd C store
       (exposure patches only; posthoc Phase-2 patches excluded) with
       its reuse.jsonl history filtered to the kept patches. Per check:
       high-confidence hit -> reuse recorded output, ZERO model calls;
       miss -> assisted call with the standard top-2 patch block.
    D  Same patch fast-path (snapshot from the D store) + concise text
       memory (frozen Phase-1 view, top-1 entry, 120-char budget). On a
       fast-path miss the assisted call injects ONLY the concise text
       block (patches contribute via fast-path only).

Live usage (<= 48 calls: 16 per condition max, fewer on fast-path hits):
    uv run python experiments/matched_rerun.py --adapter cerebras

Offline flow check (no live calls):
    uv run python experiments/matched_rerun.py --adapter stub \
        --recorded benchmarks/family-a/recorded/stub_all_pass.json

Outputs (all under artifacts/):
    artifacts/matched-rerun/<A,C,D>/<TASK-ID>.json  per-task results
    artifacts/matched-rerun/summary.json            condition table + deltas
    artifacts/matched-rerun/memory/...              frozen snapshots used
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "benchmarks"))
sys.path.insert(0, str(_ROOT))

import runner  # noqa: E402
import patch_memory_adapter as patch_mem_mod  # noqa: E402
import text_memory as text_mem_mod  # noqa: E402

CONDITIONS = ("A", "C", "D")
CONDITION_LABELS = {
    "A": "no memory",
    "C": "patch fast-path (reuse-without-calls)",
    "D": "text-concise + patch fast-path",
}

PILOT_TASK_IDS = tuple("A-TRN-%02d" % n for n in range(1, 9))

# Assisted-call injection budgets (tiny contexts only).
PATCH_TOP_K = 2
PATCH_MAX_CHARS = 320
TEXT_TOP_K = text_mem_mod.CONCISE_TOP_K
TEXT_MAX_CHARS = text_mem_mod.CONCISE_BUDGET_CHARS

# Live-call budget guard (brief allows at most 80 total).
MAX_CALLS = 80

# Baseline A stripped reference bars (documents/learning-assessment.md).
A_BAR_CALLS_PER_TASK = 2.057
A_BAR_TOKENS_PER_TASK = 264.7

# Snapshot source (read-only): frozen Phase-1 patch view from baselines-bcd.
# BOTH fast-path conditions share this one source (copied to separate dirs
# so each condition's reuse accounting stays independent): the C and D
# stores hold content-identical Phase-1 patches under different patch ids
# (id hashes include creation time), so separate lineages would fire on
# different subsets by timestamp accident and confound the C-vs-D read.
PATCH_SRC = (
    _ROOT / "artifacts" / "baselines-bcd" / "c" / "memory" / "patches-c"
)
TEXT_P1_SRC = (
    _ROOT / "artifacts" / "baselines-bcd" / "b" / "memory" / "text_memory_p1.json"
)


def snapshot_phase1_patch_store(src_dir, dst_dir):
    """Copy a Phase-1-only patch snapshot (offline, read src / write dst).

    Keeps patch files whose task_id is an exposure task (A-EXP-*);
    posthoc Phase-2 patches are excluded so the fast-path memory sees
    exactly the Phase 1 library. reuse.jsonl history is copied filtered
    to the kept patch ids (the frozen confidence signal for
    PatchMemory.find_fast_path). Returns (patch_count, history_rows).
    """
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    patches_dst = dst_dir / "patches"
    patches_dst.mkdir(parents=True, exist_ok=True)
    kept = set()
    for path in sorted((src_dir / "patches").glob("*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if not str(data.get("task_id", "")).startswith("A-EXP-"):
            continue
        shutil.copy2(path, patches_dst / path.name)
        kept.add(data.get("patch_id"))
    rows = 0
    reuse_src = src_dir / "reuse.jsonl"
    reuse_dst = dst_dir / "reuse.jsonl"
    with open(reuse_dst, "w", encoding="utf-8") as out:
        if reuse_src.exists():
            with open(reuse_src, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("patch_id") not in kept:
                        continue
                    out.write(json.dumps(row, sort_keys=True) + "\n")
                    rows += 1
    return len(kept), rows


class FastPathAdapter(runner.ModelAdapter):
    """Wrap an inner adapter with patch fast-path + assisted injection.

    Per check: ``patch_mem.find_fast_path(task)`` hit -> return the
    recorded output with ZERO inner solves; miss -> inject
    ``build_block(task)`` (once per task) and solve via inner. Usage
    history delegates to the inner adapter, so runner.run attributes
    calls/tokens ONLY to assisted checks (fast-path hits append no
    usage entries). ``checks`` records per-check fast-path accounting.
    """

    def __init__(self, inner, patch_mem, history, build_block, name_suffix):
        self.inner = inner
        self.patch_mem = patch_mem
        self._fp_history = history
        self._build_block = build_block
        self.name = "%s+%s" % (getattr(inner, "name", "?"), name_suffix)
        self.max_tokens = getattr(inner, "max_tokens", None)
        self.blocks = {}
        self.retrieval_by_task = {}
        self.checks = {}
        self.fast_path_hits = 0
        self.assisted_calls = 0

    @property
    def history(self):
        return getattr(self.inner, "history", None)

    def solve(self, task, check_index, check_input):
        task_id = task.get("id", "?")
        patch = self.patch_mem.find_fast_path(
            task, history=self._fp_history, check_input=check_input)
        if patch is not None:
            output = self.patch_mem.fast_path_output(patch)
            candidate = patch.get("candidate", {}) or {}
            self.fast_path_hits += 1
            self.checks[(task_id, check_index)] = {
                "fast_path": True,
                "assisted": False,
                "patch_id": patch.get("patch_id"),
                "patch_task_id": patch.get("task_id"),
                "input_match": (
                    check_input == candidate.get("input")
                    if isinstance(candidate, dict) else False
                ),
                "injected_chars": 0,
                "injected_est_tokens": 0,
            }
            return output
        if task_id not in self.blocks:
            block, info = self._build_block(task)
            self.blocks[task_id] = block
            self.retrieval_by_task[task_id] = info
        block = self.blocks[task_id]
        call_task = task
        if block:
            call_task = dict(task)
            call_task["prompt"] = block + task.get("prompt", "")
        output = self.inner.solve(call_task, check_index, check_input)
        self.assisted_calls += 1
        info = self.retrieval_by_task[task_id]
        self.checks[(task_id, check_index)] = {
            "fast_path": False,
            "assisted": True,
            "patch_id": None,
            "patch_task_id": None,
            "input_match": False,
            "injected_chars": info.get("injected_chars", 0),
            "injected_est_tokens": info.get("injected_est_tokens", 0),
        }
        return output


def make_assisted_builder(condition, patch_mem, text_mem):
    """Return build_block(task) -> (block, info) for assisted checks."""

    def build_block(task):
        if condition == "A":
            return "", {
                "condition": "A",
                "retrieval": "none",
                "injected_chars": 0,
                "injected_est_tokens": 0,
            }
        if condition == "C":
            patches = patch_mem.retrieve(task, limit=PATCH_TOP_K)
            block = patch_mem.format_block(
                patches, max_chars_per_example=PATCH_MAX_CHARS
            )
            return block, {
                "condition": "C",
                "patches": [
                    {
                        "patch_id": p.get("patch_id"),
                        "task_id": p.get("task_id"),
                    }
                    for p in patches
                ],
                "injected_chars": len(block),
                "injected_est_tokens": text_mem_mod.estimate_tokens(block),
            }
        if condition == "D":
            retrieved = text_mem.retrieve(task, top_k=TEXT_TOP_K)
            block = text_mem.format_block_concise(
                retrieved, max_chars_per_example=TEXT_MAX_CHARS
            )
            return block, {
                "condition": "D",
                "text": [
                    {
                        "task_id": item["entry"].get("task_id"),
                        "check": item["entry"].get("check"),
                        "overlap": item["overlap"],
                        "score": item["score"],
                    }
                    for item in retrieved
                ],
                "injected_chars": len(block),
                "injected_est_tokens": text_mem_mod.estimate_tokens(block),
            }
        raise ValueError("unknown condition %r" % (condition,))

    return build_block


def summarize_condition(condition, records):
    """Aggregate per-task records into a condition summary (offline)."""
    tasks_total = len(records)
    passed = sum(
        1 for r in records if r["summary"]["tasks"][0]["passed"]
    )
    usage_entries = [
        u for r in records for u in r["summary"]["tasks"][0].get("usage", [])
    ]
    usage = runner.aggregate_usage(usage_entries)
    fail_ids = sorted(
        r["summary"]["tasks"][0]["id"]
        for r in records
        if not r["summary"]["tasks"][0]["passed"]
    )
    fp_hits = sum(r["memory"]["fast_path_hits"] for r in records)
    assisted = sum(r["memory"]["assisted_calls"] for r in records)
    checks_total = fp_hits + assisted
    injected = [
        c.get("injected_est_tokens", 0)
        for r in records for c in r["memory"]["checks"]
    ]
    fp_input_match = sum(
        1 for r in records for c in r["memory"]["checks"]
        if c.get("fast_path") and c.get("input_match")
    )
    return {
        "condition": condition,
        "label": CONDITION_LABELS[condition],
        "tasks_total": tasks_total,
        "tasks_passed": passed,
        "success_rate": round(passed / tasks_total, 4) if tasks_total else 0.0,
        "fail_ids": fail_ids,
        "fast_path_hits": fp_hits,
        "assisted_calls": assisted,
        "checks_total": checks_total,
        "fast_path_hit_rate": (
            round(fp_hits / checks_total, 4) if checks_total else 0.0
        ),
        "fast_path_input_matches": fp_input_match,
        "usage_totals": usage,
        "per_task_mean": {
            "calls": round(usage["calls"] / tasks_total, 3) if tasks_total else 0.0,
            "input_tokens": round(usage["input_tokens"] / tasks_total, 1)
            if tasks_total else 0.0,
            "output_tokens": round(usage["output_tokens"] / tasks_total, 1)
            if tasks_total else 0.0,
            "total_tokens": round(usage["total_tokens"] / tasks_total, 1)
            if tasks_total else 0.0,
            "latency_ms": round(usage["latency_ms"] / tasks_total, 1)
            if tasks_total else 0.0,
            "cost_usd": round(usage["cost_usd"] / tasks_total, 6)
            if tasks_total else 0.0,
        },
        "mean_injected_est_tokens": (
            round(sum(injected) / len(injected), 1) if injected else 0.0
        ),
    }


def compute_deltas(summaries, baseline="A"):
    """Condition-minus-baseline deltas for success rate and resources."""
    if baseline not in summaries:
        return []
    base = summaries[baseline]
    deltas = []
    for condition in CONDITIONS:
        if condition == baseline or condition not in summaries:
            continue
        cur = summaries[condition]
        deltas.append({
            "condition": condition,
            "baseline": baseline,
            "success_rate_delta": round(
                cur["success_rate"] - base["success_rate"], 4),
            "tasks_delta": cur["tasks_passed"] - base["tasks_passed"],
            "tokens_per_task_delta": round(
                cur["per_task_mean"]["total_tokens"]
                - base["per_task_mean"]["total_tokens"], 1),
            "calls_per_task_delta": round(
                cur["per_task_mean"]["calls"]
                - base["per_task_mean"]["calls"], 3),
        })
    return deltas


def run_condition(condition, tasks, out_root, make_inner, patch_mem,
                  fp_history, text_mem, a_totals):
    """Run one condition over the transfer tasks; return (summary, n_calls)."""
    out_dir = Path(out_root) / condition
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    inner = make_inner()
    builder = make_assisted_builder(condition, patch_mem, text_mem)
    if condition == "A":
        adapter = FastPathAdapter(
            inner,
            patch_mem_mod.PatchMemory(
                str(Path(out_root) / "memory" / "patches-empty-a")),
            {}, builder, "matchA",
        )
        # Condition A must see no memory at all: the empty store has no
        # patches, so find_fast_path always misses and no block is built.
    else:
        adapter = FastPathAdapter(
            inner, patch_mem, fp_history, builder, "match" + condition)
    records = []
    for task in sorted(tasks, key=lambda t: t["id"]):
        summary = runner.run(
            [task], adapter, verbose=False, strip_fences=True
        )
        task_summary = summary["tasks"][0]
        task_passed = task_summary["passed"]
        check_recs = []
        for j in range(len(task["checks"])):
            rec = dict(adapter.checks.get((task["id"], j), {}))
            rec["check"] = j
            check_recs.append(rec)
        fp_hits = sum(1 for c in check_recs if c.get("fast_path"))
        assisted = sum(1 for c in check_recs if c.get("assisted"))
        retrieval = adapter.retrieval_by_task.get(task["id"], {})
        # Reuse accounting: one row per involved patch (fast-path patch
        # or retrieved patches on assisted tasks), helped = task passed.
        if condition in ("C", "D"):
            involved = []
            for rec in check_recs:
                if rec.get("fast_path") and rec.get("patch_id"):
                    involved.append((rec["patch_id"], True))
            if assisted:
                for item in retrieval.get("patches", []) or []:
                    involved.append((item.get("patch_id"), False))
            seen = set()
            for patch_id, via_fast_path in involved:
                if not patch_id or patch_id in seen:
                    continue
                seen.add(patch_id)
                patch = patch_mem.store.get_patch(patch_id) or {
                    "patch_id": patch_id
                }
                a_total = (a_totals.get(task["id"], {}) or {})
                patch_mem.record_reuse(
                    patch, task, helped=task_passed,
                    fast_path=via_fast_path,
                    tokens_saved=(
                        (a_total.get("total_tokens") or 0)
                        - task_summary["totals"]["total_tokens"]
                    ),
                    latency_saved_ms=(
                        (a_total.get("latency_ms") or 0.0)
                        - task_summary["totals"]["latency_ms"]
                    ),
                )
        record = {
            "condition": condition,
            "label": CONDITION_LABELS[condition],
            "adapter": adapter.name,
            "max_tokens": getattr(adapter, "max_tokens", None),
            "strip_fences": True,
            "temperature": 0.0,
            "reasoning": "off",
            "memory": {
                "retrieval": retrieval,
                "checks": check_recs,
                "fast_path_hits": fp_hits,
                "assisted_calls": assisted,
            },
            "summary": summary,
        }
        out_path = out_dir / (task["id"] + ".json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        records.append(record)
        print("  wrote %s" % out_path)
    summary = summarize_condition(condition, records)
    n_calls = sum(
        r["summary"]["tasks"][0]["totals"]["calls"] for r in records
    )
    return summary, n_calls


def load_a_totals(out_root):
    """Per-task totals from this run's condition-A files (tokens_saved basis)."""
    totals = {}
    for path in sorted((Path(out_root) / "A").glob("A-*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            tasks = data.get("summary", {}).get("tasks", [])
            if tasks:
                task = tasks[0]
                totals[task.get("id", path.stem)] = dict(
                    task.get("totals", {})
                )
        except (OSError, ValueError):
            continue
    return totals


def run_all(tasks, out_root, make_inner, max_calls=MAX_CALLS):
    """Run conditions A/C/D; write summary.json; return the payload."""
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    mem_root = out_root / "memory"

    # Offline memory setup (zero model calls): Phase-1-only snapshots.
    snapshots = {}
    patch_mems = {}
    fp_histories = {}
    for condition in ("C", "D"):
        dst = mem_root / ("patches-" + condition.lower() + "-fp")
        n_patches, n_rows = snapshot_phase1_patch_store(PATCH_SRC, dst)
        patch_mems[condition] = patch_mem_mod.PatchMemory(str(dst))
        fp_histories[condition] = patch_mems[condition].reuse_history()
        snapshots[condition] = {
            "src": str(PATCH_SRC),
            "dst": str(dst),
            "phase1_patches": n_patches,
            "history_rows": n_rows,
        }
        print("condition %s: snapshotted %d Phase-1 patches, %d history rows"
              % (condition, n_patches, n_rows))
    text_p1_dst = mem_root / "text_memory_p1.json"
    shutil.copy2(TEXT_P1_SRC, text_p1_dst)
    text_mem = text_mem_mod.TextMemory.load(str(text_p1_dst))
    print("text memory: %d frozen Phase-1 entries" % len(text_mem))

    summaries = {}
    total_calls = 0
    a_totals = {}
    for condition in CONDITIONS:
        print("condition %s (%s): %d transfer tasks" % (
            condition, CONDITION_LABELS[condition], len(tasks)))
        summary, n_calls = run_condition(
            condition, tasks, out_root, make_inner,
            patch_mems.get(condition), fp_histories.get(condition, {}),
            text_mem, a_totals,
        )
        summaries[condition] = summary
        total_calls += n_calls
        print("condition %s: %d/%d passed, %d calls (%d fast-path hits)" % (
            condition, summary["tasks_passed"], summary["tasks_total"],
            n_calls, summary["fast_path_hits"]))
        if total_calls > max_calls:
            raise RuntimeError(
                "call budget exceeded: %d > %d" % (total_calls, max_calls)
            )
        if condition == "A":
            # Later conditions score tokens_saved against THIS run's A.
            a_totals = load_a_totals(out_root)
    payload = {
        "experiment": "matched-rerun",
        "task_ids": [t["id"] for t in sorted(tasks, key=lambda t: t["id"])],
        "conditions": list(CONDITIONS),
        "condition_labels": CONDITION_LABELS,
        "standard_conditions": {
            "scoring": "stripped",
            "temperature": 0.0,
            "reasoning": "off",
            "max_tokens": getattr(make_inner(), "max_tokens", None),
            "calls_per_check_max": 1,
        },
        "call_budget": {"max_calls": max_calls, "total_calls": total_calls},
        "snapshots": snapshots,
        "text_p1_src": str(TEXT_P1_SRC),
        "text_p1_entries": len(text_mem),
        "reference_bars": {
            "a_calls_per_task": A_BAR_CALLS_PER_TASK,
            "a_tokens_per_task": A_BAR_TOKENS_PER_TASK,
        },
        "summaries": summaries,
        "deltas_vs_a": compute_deltas(summaries, baseline="A"),
    }
    out_path = out_root / "summary.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print("wrote %s (%d total calls)" % (out_path, total_calls))
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Matched rerun: fast-path reuse vs FAIL criteria #2/#3")
    parser.add_argument("--tasks", default=str(
        _ROOT / "benchmarks" / "family-a"))
    parser.add_argument("--adapter", choices=("stub", "cerebras"),
                        default="stub")
    parser.add_argument("--recorded", default=None)
    parser.add_argument("--artifacts", default=str(
        _ROOT / "artifacts" / "matched-rerun"))
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--max-calls", type=int, default=MAX_CALLS)
    parser.add_argument("--only", default=None,
                        help="run one condition only (A, C, or D)")
    args = parser.parse_args(argv)

    global CONDITIONS  # noqa: PLW0603 (single-condition rerun)
    if args.only:
        if args.only not in CONDITIONS:
            print("bad --only %r (want one of A C D)" % args.only,
                  file=sys.stderr)
            return 2
        CONDITIONS = (args.only,)

    tasks = runner.load_tasks(Path(args.tasks))
    problems = runner.validate_tasks(tasks)
    if problems:
        print("task schema problems:", file=sys.stderr)
        for problem in problems:
            print("  %s" % problem, file=sys.stderr)
        return 2
    pilot = [t for t in tasks if t["id"] in PILOT_TASK_IDS]
    if len(pilot) != len(PILOT_TASK_IDS):
        print("expected %d transfer tasks, got %d" % (
            len(PILOT_TASK_IDS), len(pilot)), file=sys.stderr)
        return 2

    def make_inner():
        if args.adapter == "stub":
            if not args.recorded:
                print("--recorded FILE is required with --adapter stub",
                      file=sys.stderr)
                raise SystemExit(2)
            return runner.StubAdapter(Path(args.recorded))
        return runner.CerebrasAdapter(max_tokens=args.max_tokens)

    # Fail fast on a bad stub path before any live call.
    make_inner()
    payload = run_all(pilot, args.artifacts, make_inner,
                      max_calls=args.max_calls)
    ok = all(
        s["tasks_passed"] == s["tasks_total"]
        for s in payload["summaries"].values()
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
