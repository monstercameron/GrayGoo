"""Two-phase driver for Baselines B/C/D (Family A, stripped condition).

Method mirrors Baseline A exactly: temperature 0.0, reasoning off,
max_tokens 128, one model call per check, ``--strip-fences`` scoring.
Each baseline runs Phase 1 (18 exposure tasks from scratch, memory
accumulates) then Phase 2 (8 transfer + 5 adversarial + 4
equivalent-transform WITH memory retrieved from frozen Phase 1
successes). Retrieval is offline (keyword/tag overlap, no model calls).

Memory kinds:
    b  textual memory only (benchmarks/text_memory.py)
    c  executable patch memory only (benchmarks/patch_memory_adapter.py)
    d  dual: text + patches

Live usage (72 calls per baseline, 216 for all three):
    uv run python benchmarks/run_bcd.py --baseline b --adapter cerebras
    uv run python benchmarks/run_bcd.py --baseline c --adapter cerebras
    uv run python benchmarks/run_bcd.py --baseline d --adapter cerebras

Offline flow check (no live calls):
    uv run python benchmarks/run_bcd.py --baseline all --adapter stub \\
        --recorded benchmarks/family-a/recorded/stub_all_pass.json

Outputs (all under artifacts/):
    artifacts/baselines-bcd/<b,c,d>/<TASK-ID>.json   per-task results
    artifacts/baselines-bcd/<b,c,d>-summary.json     aggregate + memory metrics
    artifacts/baselines-bcd/<b,c,d>/memory/...       frozen/live memory stores
"""

import argparse
import json
import math
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patch_memory_adapter as patch_mem_mod
import runner
import text_memory as text_mem_mod

BASELINES = ("b", "c", "d")
MEMORY_KIND = {"b": "text", "c": "patch", "d": "dual"}
PHASE1_SPLIT = "exposure"

# Baseline A stripped reference (documents/baseline-a.md + stripped-summary).
# Used only for comparison deltas; the driver also tries to load the
# machine-readable summary for per-task token/latency deltas.
A_STRIPPED = {
    "tasks_passed": 29,
    "tasks_total": 35,
    "by_split": {
        "exposure": {"passed": 16, "total": 18},
        "transfer": {"passed": 5, "total": 8},
        "adversarial": {"passed": 4, "total": 5},
        "equivalent-transform": {"passed": 4, "total": 4},
    },
    "fail_ids": {
        "A-ADV-02", "A-EXP-08", "A-EXP-16",
        "A-TRN-02", "A-TRN-06", "A-TRN-08",
    },
    "usage_totals": {
        "calls": 72, "input_tokens": 7188, "output_tokens": 2078,
        "total_tokens": 9266, "cost_usd": 0.010209,
    },
    "per_task_mean": {
        "calls": 2.057, "input_tokens": 205.4, "output_tokens": 59.4,
        "total_tokens": 264.7, "latency_ms": 1080.9, "cost_usd": 0.000292,
    },
}


class MemoryAdapter(runner.ModelAdapter):
    """Wrap an inner adapter with frozen-memory prompt augmentation.

    ``build_block(task)`` returns (block_text, retrieval_info). Retrieval
    runs once per task (first check) and is reused for later checks of
    the same task. Raw outputs are captured for verified-success storage.
    Usage history delegates to the inner adapter (as runner.run expects).
    """

    def __init__(self, inner, build_block, name_suffix):
        self.inner = inner
        self._build_block = build_block
        self.name = "%s+%s" % (getattr(inner, "name", "?"), name_suffix)
        self.max_tokens = getattr(inner, "max_tokens", None)
        self.outputs = {}
        self.blocks = {}
        self.retrieval_by_task = {}

    @property
    def history(self):
        return getattr(self.inner, "history", None)

    def solve(self, task, check_index, check_input):
        task_id = task.get("id", "?")
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
        self.outputs[(task_id, check_index)] = output if output is not None else ""
        return output


def entropy(counts):
    """Shannon entropy (nats) of a Counter of reuse counts."""
    total = sum(counts.values())
    if total <= 0:
        return 0.0
    return -sum(
        (c / total) * math.log(c / total) for c in counts.values() if c > 0
    )


def load_baseline_a_task_totals(tasks_dir):
    """Per-task (tokens, latency) from Baseline A stripped files, if present."""
    totals = {}
    base = Path(tasks_dir).resolve().parent.parent / "artifacts" / "baseline-a"
    stripped = base / "stripped"
    if not stripped.is_dir():
        return totals
    for path in stripped.glob("A-*.json"):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            tasks = data.get("summary", {}).get("tasks", [])
            if tasks:
                task = tasks[0]
                totals[task.get("id", path.stem)] = {
                    "total_tokens": task.get("totals", {}).get("total_tokens", 0),
                    "latency_ms": task.get("totals", {}).get("latency_ms", 0.0),
                }
        except (OSError, ValueError):
            continue
    return totals


def run_baseline(baseline, tasks, out_root, inner, a_totals, top_k=2,
                 max_chars=320):
    """Run one baseline's two phases; return the summary dict."""
    out_dir = Path(out_root) / baseline
    if out_dir.exists():
        shutil.rmtree(out_dir)
    mem_dir = out_dir / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)

    kind = MEMORY_KIND[baseline]
    use_text = kind in ("text", "dual")
    use_patch = kind in ("patch", "dual")

    text_mem = text_mem_mod.TextMemory() if use_text else None
    patch_mem = (
        patch_mem_mod.PatchMemory(str(mem_dir / ("patches-" + baseline)))
        if use_patch
        else None
    )

    # Frozen retrieval views (snapshotted after Phase 1; Phase 2
    # successes are stored post-hoc for growth metrics only and are
    # never retrieved during the run).
    frozen = {"text_entries": None, "patch_ids": None, "enabled": False}

    text_reuse_counts: Counter = Counter()
    patch_reuse_counts: Counter = Counter()
    per_task_records = []
    # Phase 2 successes held pending and flushed only AFTER the run, so
    # the live stores stay identical to the frozen Phase 1 views during
    # all Phase 2 retrieval (PatchStore ranks newest-first; mid-run
    # inserts would otherwise push frozen patches out of top-k).
    pending: list = []

    def build_block(task):
        if not frozen["enabled"]:
            return "", {"retrieval": "disabled-phase1"}
        info = {}
        blocks = []
        if use_text:
            view = text_mem_mod.TextMemory()
            view.entries = frozen["text_entries"] or []
            retrieved = view.retrieve(task, top_k=top_k)
            block = view.format_block(
                retrieved, max_chars_per_example=max_chars
            )
            blocks.append(block)
            for item in retrieved:
                text_reuse_counts[item["entry"].get("task_id", "?")] += 1
            info["text"] = {
                "retrieved": [
                    {
                        "task_id": item["entry"].get("task_id"),
                        "check": item["entry"].get("check"),
                        "overlap": item["overlap"],
                        "score": item["score"],
                    }
                    for item in retrieved
                ],
                "hit": any(item["overlap"] > 0 for item in retrieved),
            }
        if use_patch:
            retrieved = [
                p
                for p in patch_mem.retrieve(task, limit=top_k)
                if p.get("patch_id") in (frozen["patch_ids"] or set())
            ]
            block = patch_mem.format_block(
                retrieved, max_chars_per_example=max_chars
            )
            blocks.append(block)
            for patch in retrieved:
                patch_reuse_counts[patch.get("patch_id", "?")] += 1
            info["patches"] = {
                "retrieved": [
                    {
                        "patch_id": p.get("patch_id"),
                        "task_id": p.get("task_id"),
                    }
                    for p in retrieved
                ],
                "reuse": bool(retrieved),
            }
        combined = "".join(blocks)
        info["injected_chars"] = len(combined)
        return combined, info

    adapter = MemoryAdapter(inner, build_block, "mem" + baseline)

    def store_successes(task, summary, posthoc):
        """Store verified-successful checks; return count stored.

        Phase 2 successes are held in ``pending`` (flushed after the
        run) so retrieval stays frozen on Phase 1 during the run.
        """
        if not summary["tasks"] or not summary["tasks"][0]["passed"]:
            return 0
        task_id = task["id"]
        stored = 0
        for j in range(len(task["checks"])):
            output = adapter.outputs.get((task_id, j), "")
            if posthoc:
                pending.append((task, j, task["checks"][j]["input"], output))
            else:
                if use_text:
                    assert text_mem is not None
                    text_mem.add_success(
                        task, j, task["checks"][j]["input"], output
                    )
                if use_patch:
                    assert patch_mem is not None
                    patch_mem.add_success(
                        task, j, task["checks"][j]["input"], output
                    )
            stored += 1
        return stored

    def flush_pending():
        """Persist held Phase 2 successes (growth accounting only)."""
        for task, j, check_input, output in pending:
            if use_text:
                assert text_mem is not None
                text_mem.add_success(task, j, check_input, output)
            if use_patch:
                assert patch_mem is not None
                patch_mem.add_success(task, j, check_input, output)
        pending.clear()

    def run_task(task, phase):
        summary = runner.run(
            [task], adapter, verbose=False, strip_fences=True
        )
        task_summary = summary["tasks"][0]
        passed = task_summary["passed"]
        retrieval = adapter.retrieval_by_task.get(task["id"], {})
        posthoc = phase != PHASE1_SPLIT
        stored = store_successes(task, summary, posthoc)
        # Record patch reuse outcomes (one row per retrieved patch).
        if posthoc and use_patch:
            assert patch_mem is not None
            for item in (retrieval.get("patches", {}).get("retrieved") or []):
                patch = patch_mem.store.get_patch(item["patch_id"]) or {
                    "patch_id": item["patch_id"]
                }
                a_total = a_totals.get(task["id"], {})
                patch_mem.record_reuse(
                    patch,
                    task,
                    helped=passed,
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
            "baseline": baseline,
            "phase": phase,
            "adapter": adapter.name,
            "max_tokens": getattr(adapter, "max_tokens", None),
            "strip_fences": True,
            "memory": {
                "kind": kind if posthoc else "none",
                "retrieval": retrieval,
                "stored": stored,
                "stored_posthoc": posthoc and stored > 0,
            },
            "summary": summary,
        }
        out_path = out_dir / (task["id"] + ".json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        per_task_records.append(record)
        print("  wrote %s" % out_path)
        return passed

    exposure = sorted(
        (t for t in tasks if t["split"] == PHASE1_SPLIT),
        key=lambda t: t["id"],
    )
    phase2 = sorted(
        (t for t in tasks if t["split"] != PHASE1_SPLIT),
        key=lambda t: t["id"],
    )
    print("baseline %s (%s): phase 1 exposure x%d (no memory)" % (
        baseline, kind, len(exposure)))
    for task in exposure:
        run_task(task, PHASE1_SPLIT)

    # Freeze retrieval views after Phase 1.
    p1_text = len(text_mem.entries) if text_mem is not None else 0
    p1_patches = patch_mem.capability_count() if patch_mem is not None else 0
    if text_mem is not None:
        frozen["text_entries"] = list(text_mem.entries)
        text_mem.save(str(mem_dir / "text_memory_p1.json"))
    if patch_mem is not None:
        frozen["patch_ids"] = {
            p.get("patch_id") for p in patch_mem.store.list_patches()
        }
    frozen["enabled"] = True
    print("baseline %s: frozen after phase 1: %d text entries, %d patches" % (
        baseline, p1_text, p1_patches))
    print("baseline %s: phase 2 x%d (with %s memory)" % (
        baseline, len(phase2), kind))
    for task in phase2:
        run_task(task, task["split"])
    flush_pending()

    if text_mem is not None:
        text_mem.save(str(mem_dir / "text_memory.json"))

    return summarize_baseline(
        baseline, kind, per_task_records, out_root,
        p1_text, p1_patches, text_mem, patch_mem,
        text_reuse_counts, patch_reuse_counts,
    )


def summarize_baseline(baseline, kind, records, out_root,
                       p1_text, p1_patches, text_mem, patch_mem,
                       text_reuse_counts, patch_reuse_counts):
    """Aggregate per-task records into a baseline summary dict + file."""
    total = len(records)
    passed = sum(1 for r in records if r["summary"]["tasks"][0]["passed"])
    by_split = {}
    for split in runner.SPLITS:
        subset = [r for r in records if r["summary"]["tasks"][0]["split"] == split]
        if not subset:
            continue
        fails = [
            r["summary"]["tasks"][0]["id"] for r in subset
            if not r["summary"]["tasks"][0]["passed"]
        ]
        ok = len(subset) - len(fails)
        by_split[split] = {
            "passed": ok, "total": len(subset),
            "rate": round(ok / len(subset), 4), "fail_ids": sorted(fails),
        }
    usage_entries = [
        u for r in records for u in r["summary"]["tasks"][0].get("usage", [])
    ]
    usage = runner.aggregate_usage(usage_entries)
    checks_total = sum(
        len(r["summary"]["tasks"][0]["checks"]) for r in records
    )
    checks_stripped = sum(
        1 for r in records for c in r["summary"]["tasks"][0]["checks"]
        if c.get("stripped")
    )
    latencies = [
        u.get("latency_ms") for u in usage_entries
        if isinstance(u.get("latency_ms"), (int, float))
    ]
    finish = Counter(
        u.get("finish_reason") for u in usage_entries if u.get("finish_reason")
    )
    request_ids = [u.get("request_id") for u in usage_entries if u.get("request_id")]

    phase2 = [r for r in records if r["phase"] != PHASE1_SPLIT]
    # Text retrieval hits (genuine keyword overlap) on Phase 2 tasks.
    text_hits = sum(
        1 for r in phase2
        if r["memory"].get("retrieval", {}).get("text", {}).get("hit")
    )
    text_retrieved_tasks = sum(
        1 for r in phase2
        if r["memory"].get("retrieval", {}).get("text", {}).get("retrieved")
    )
    # Patch reuse on Phase 2 tasks.
    patch_reuse_tasks = sum(
        1 for r in phase2
        if r["memory"].get("retrieval", {}).get("patches", {}).get("reuse")
    )
    patch_helped_tasks = sum(
        1 for r in phase2
        if r["memory"].get("retrieval", {}).get("patches", {}).get("reuse")
        and r["summary"]["tasks"][0]["passed"]
    )
    # Transfer deltas vs Baseline A stripped (Phase 2 only).
    neg_transfer = sorted(
        r["summary"]["tasks"][0]["id"] for r in phase2
        if r["summary"]["tasks"][0]["id"] not in A_STRIPPED["fail_ids"]
        and not r["summary"]["tasks"][0]["passed"]
    )
    pos_transfer = sorted(
        r["summary"]["tasks"][0]["id"] for r in phase2
        if r["summary"]["tasks"][0]["id"] in A_STRIPPED["fail_ids"]
        and r["summary"]["tasks"][0]["passed"]
    )
    # Patch reuse outcomes via the root transfer module.
    reuse_rows = patch_mem.get_reuses() if patch_mem is not None else []
    transfer_metrics = patch_mem_mod.summarize_transfer_outcomes(
        reuse_rows, baseline_success=A_STRIPPED["tasks_passed"] / 35.0
    ) if patch_mem is not None else {}
    n2_text = len(text_mem.entries) if text_mem is not None else 0
    n2_patches = patch_mem.capability_count() if patch_mem is not None else 0

    summary = {
        "baseline": baseline,
        "memory": kind,
        "condition": "stripped",
        "files": total,
        "tasks_total": total,
        "tasks_passed": passed,
        "success_rate": round(passed / total, 4) if total else 0.0,
        "by_split": by_split,
        "checks_total": checks_total,
        "checks_stripped": checks_stripped,
        "usage_totals": usage,
        "per_task_mean": {
            "calls": round(usage["calls"] / total, 3) if total else 0.0,
            "input_tokens": round(usage["input_tokens"] / total, 1) if total else 0.0,
            "output_tokens": round(usage["output_tokens"] / total, 1) if total else 0.0,
            "total_tokens": round(usage["total_tokens"] / total, 1) if total else 0.0,
            "latency_ms": round(usage["latency_ms"] / total, 1) if total else 0.0,
            "cost_usd": round(usage["cost_usd"] / total, 6) if total else 0.0,
        },
        "call_latency_ms": {
            "min": min(latencies) if latencies else 0.0,
            "max": max(latencies) if latencies else 0.0,
            "mean": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        },
        "finish_reasons": dict(finish),
        "request_ids_unique": len(set(request_ids)) == len(request_ids),
        "request_ids_n": len(request_ids),
        "memory_metrics": {
            "phase1_text_entries": p1_text,
            "phase1_patches": p1_patches,
            "final_text_entries": n2_text,
            "capability_count": n2_patches,
            "library_growth": n2_patches - p1_patches,
            "phase2_tasks": len(phase2),
            "text_retrieved_tasks": text_retrieved_tasks,
            "text_hit_tasks": text_hits,
            "text_hit_rate": round(text_hits / len(phase2), 4) if phase2 else 0.0,
            "patch_reuse_tasks": patch_reuse_tasks,
            "patch_helped_tasks": patch_helped_tasks,
            "transfer_outcomes": transfer_metrics,
            "negative_transfer_count": len(neg_transfer),
            "negative_transfer_ids": neg_transfer,
            "positive_transfer_count": len(pos_transfer),
            "positive_transfer_ids": pos_transfer,
            "text_reuse_entropy": round(entropy(text_reuse_counts), 4),
            "capability_entropy": round(entropy(patch_reuse_counts), 4),
        },
        "comparison_vs_a_stripped": {
            "tasks_delta": passed - A_STRIPPED["tasks_passed"],
            "rate_delta": round(passed / total - A_STRIPPED["tasks_passed"] / 35.0, 4)
            if total else 0.0,
            "by_split_delta": {
                split: (
                    by_split[split]["passed"]
                    - A_STRIPPED["by_split"][split]["passed"]
                )
                for split in by_split
                if split in A_STRIPPED["by_split"]
            },
        },
    }
    out_path = Path(out_root) / ("%s-summary.json" % baseline)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    print("wrote %s" % out_path)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Baselines B/C/D two-phase driver")
    parser.add_argument("--baseline", choices=("b", "c", "d", "all"), default="all")
    parser.add_argument("--tasks", default=str(
        Path(__file__).resolve().parent / "family-a"))
    parser.add_argument("--adapter", choices=("stub", "cerebras"), default="stub")
    parser.add_argument("--recorded", default=None)
    parser.add_argument("--artifacts", default=str(
        Path(__file__).resolve().parent.parent / "artifacts" / "baselines-bcd"))
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--top-k", type=int, default=2)
    parser.add_argument("--max-chars", type=int, default=320)
    args = parser.parse_args(argv)

    tasks = runner.load_tasks(Path(args.tasks))
    problems = runner.validate_tasks(tasks)
    if problems:
        print("task schema problems:", file=sys.stderr)
        for problem in problems:
            print("  %s" % problem, file=sys.stderr)
        return 2
    if len(tasks) != 35:
        print("expected 35 tasks, got %d" % len(tasks), file=sys.stderr)
        return 2

    baselines = BASELINES if args.baseline == "all" else (args.baseline,)
    a_totals = load_baseline_a_task_totals(args.tasks)
    print("baseline A per-task totals loaded: %d/35" % len(a_totals))

    overall_ok = True
    for baseline in baselines:
        if args.adapter == "stub":
            if not args.recorded:
                print("--recorded FILE is required with --adapter stub",
                      file=sys.stderr)
                return 2
            inner = runner.StubAdapter(Path(args.recorded))
        else:
            inner = runner.CerebrasAdapter(max_tokens=args.max_tokens)
        summary = run_baseline(
            baseline, tasks, args.artifacts, inner, a_totals,
            top_k=args.top_k, max_chars=args.max_chars,
        )
        print("baseline %s: %d/%d stripped (delta vs A: %+d)" % (
            baseline, summary["tasks_passed"], summary["tasks_total"],
            summary["comparison_vs_a_stripped"]["tasks_delta"]))
        overall_ok = overall_ok and summary["tasks_passed"] == summary["tasks_total"]
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
