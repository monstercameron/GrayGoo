"""Pilot: semantic+code memory vs code-only memory (same 8-task subset).

Compares two retrieval modes over the 8 Family A transfer tasks:
    code-only      top-2 executable patch exemplars only
    semantic+code  top-1 semantic skill family (intent + abstract
                   procedure) plus the same top-2 patch exemplars

Measures success + tokens (plus calls/latency for context). Same
baselines standard as key_experiment.py: stripped, temperature 0.0,
reasoning off, max_tokens 128, one call per check.

Live usage (32 calls for both modes):
    uv run python experiments/semantic_compare.py --adapter cerebras

Offline flow check (no live calls):
    uv run python experiments/semantic_compare.py --adapter stub \
        --recorded benchmarks/family-a/recorded/stub_all_pass.json

Outputs (all under artifacts/):
    artifacts/semantic-compare/<code-only,semantic+code>/<TASK-ID>.json
    artifacts/semantic-compare/summary.json
    artifacts/semantic-compare/memory/...  patch + skill stores
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

from experiments.key_experiment import (  # noqa: E402
    MAX_CHARS_PER_EXAMPLE,
    PATCHES_TOP_K,
    PILOT_TASK_IDS,
    ConditionAdapter,
    PilotPatchMemory,
    seed_patch_memory,
)

MODES = ("code-only", "semantic+code")
MODE_LABELS = {
    "code-only": "executable patch exemplars only",
    "semantic+code": "skill family procedure + patch exemplars",
}

SEMANTIC_TOP_K = 1
MAX_CHARS_SEMANTIC_BLOCK = 500

# Two pilot families extending skills.SEED_FAMILIES so every Family A
# category (dates, csv, records, logs) has a matching semantic entry.
# Narrow applicability, code-free procedures (skills.py schema).
EXTRA_FAMILIES = [
    {
        "family_id": "family-a-records-transform",
        "intent": "Reshape JSON records (flatten, unflatten, project, "
                  "rename, deep-merge, normalize values) without losing "
                  "or inventing fields.",
        "when": ["records", "family-a"],
        "when_not": ["csv", "logs", "dates", "binary"],
        "abstract_procedure": [
            "Identify the reshape kind: flatten, unflatten, project, "
            "merge, or normalize.",
            "Carry every input field to exactly one output slot; "
            "missing projected keys become null.",
            "Deep-merge recursively; replace arrays wholesale.",
            "Normalize values only as specified (trim, case, strip).",
            "Verify output parses as JSON before finishing.",
        ],
        "contracts": [
            "No input field silently dropped or invented.",
            "Array values replaced wholesale, never element-merged.",
        ],
        "known_failure_modes": [
            "dropped-null-keys",
            "array-element-merge",
            "alias-miss",
        ],
    },
    {
        "family_id": "family-a-log-parsing",
        "intent": "Parse semi-structured log lines (Common Log Format, "
                  "key=value, syslog, level filtering) into JSON.",
        "when": ["logs", "family-a"],
        "when_not": ["csv", "records", "dates", "binary"],
        "abstract_procedure": [
            "Match the line structure (brackets, quotes, fields) "
            "before splitting anything.",
            "Extract named fields with one anchored pattern.",
            "Map '-' placeholders to null/0 as specified.",
            "Compare filter fields for exact equality.",
            "Verify output parses as JSON before finishing.",
        ],
        "contracts": [
            "Quoted/bracketed spans stay whole during field split.",
            "Filter matches the field value, not a substring.",
        ],
        "known_failure_modes": [
            "word-split-quoted-field",
            "substring-filter",
            "space-padded-day",
        ],
    },
]


def semantic_task_tags(task):
    """Offline tag query for skill-family retrieval."""
    tags = []
    for tag in (task.get("category", ""), "family-a"):
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def build_skill_store(directory):
    """SkillStore with default seeds + pilot records/logs families."""
    import skills  # noqa: PLC0415

    store = skills.SkillStore(str(directory))
    skills.seed_default_families(store)
    for spec in EXTRA_FAMILIES:
        if store.get_family(spec["family_id"]) is None:
            store.save_family(
                spec["family_id"], spec["intent"],
                when=spec["when"], when_not=spec["when_not"],
                abstract_procedure=spec["abstract_procedure"],
                contracts=spec["contracts"],
                known_failure_modes=spec["known_failure_modes"],
            )
    return store


def format_semantic_block(families, max_chars=MAX_CHARS_SEMANTIC_BLOCK):
    """Format retrieved skill families as a tiny prompt block."""
    if not families:
        return ""
    lines = ["Relevant procedure (adapt the steps, not the code):"]
    for family in families:
        lines.append("- [%s] %s" % (
            family.get("family_id", "?"), family.get("intent", "")))
        for i, step in enumerate(
                family.get("abstract_procedure", []), 1):
            lines.append("  %d. %s" % (i, step))
    block = "\n".join(lines) + "\n"
    if len(block) > max_chars:
        block = block[: max_chars - 1] + "…"
    return block


def make_block_builder(mode, skill_store, patch_memory):
    """Return build_block(task) -> (block, info) for one mode."""
    if mode not in MODES:
        raise ValueError("unknown mode %r" % (mode,))

    def build_block(task):
        patches = patch_memory.retrieve(task, limit=PATCHES_TOP_K)
        patch_block = patch_memory.format_block(
            patches, max_chars_per_example=MAX_CHARS_PER_EXAMPLE
        )
        info = {
            "mode": mode,
            "patches": [
                {"patch_id": p.get("patch_id"), "task_id": p.get("task_id")}
                for p in patches
            ],
        }
        if mode == "code-only":
            info["injected_chars"] = len(patch_block)
            return patch_block, info
        if mode == "semantic+code":
            families = skill_store.find_for_task(
                semantic_task_tags(task), limit=SEMANTIC_TOP_K
            )
            semantic_block = format_semantic_block(families)
            info["families"] = [
                f.get("family_id") for f in families
            ]
            block = semantic_block + patch_block
            info["injected_chars"] = len(block)
            return block, info
        raise ValueError("unknown mode %r" % (mode,))

    return build_block


def summarize_mode(mode, records):
    """Aggregate per-task records into a mode summary (offline)."""
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
    injected = [
        r["memory"].get("retrieval", {}).get("injected_chars", 0)
        for r in records
    ]
    return {
        "mode": mode,
        "label": MODE_LABELS[mode],
        "tasks_total": tasks_total,
        "tasks_passed": passed,
        "success_rate": round(passed / tasks_total, 4) if tasks_total else 0.0,
        "fail_ids": fail_ids,
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
        "mean_injected_chars": (
            round(sum(injected) / len(injected), 1) if injected else 0.0
        ),
    }


def compute_mode_deltas(summaries, baseline="code-only"):
    """Mode-minus-baseline deltas for success rate and resources."""
    if baseline not in summaries:
        return []
    base = summaries[baseline]
    deltas = []
    for mode in MODES:
        if mode == baseline or mode not in summaries:
            continue
        cur = summaries[mode]
        deltas.append({
            "mode": mode,
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
            "latency_ms_per_task_delta": round(
                cur["per_task_mean"]["latency_ms"]
                - base["per_task_mean"]["latency_ms"], 1),
        })
    return deltas


def run_mode(mode, tasks, out_root, inner, skill_store, patch_memory):
    """Run one mode over the pilot tasks; return (summary, records)."""
    out_dir = Path(out_root) / mode
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    builder = make_block_builder(mode, skill_store, patch_memory)
    adapter = ConditionAdapter(inner, builder, "sem" + mode[:4])
    records = []
    for task in sorted(tasks, key=lambda t: t["id"]):
        summary = runner.run(
            [task], adapter, verbose=False, strip_fences=True
        )
        task_summary = summary["tasks"][0]
        retrieval = adapter.retrieval_by_task.get(task["id"], {})
        for item in retrieval.get("patches", []) or []:
            patch = patch_memory.store.get_patch(
                item.get("patch_id")
            ) or {"patch_id": item.get("patch_id")}
            patch_memory.record_reuse(
                patch, task, helped=task_summary["passed"]
            )
        for family_id in retrieval.get("families", []) or []:
            for item in retrieval.get("patches", []) or []:
                try:
                    skill_store.link_implementation(
                        family_id, item.get("patch_id"))
                except KeyError:
                    pass
            try:
                skill_store.record_outcome(
                    family_id, task["id"],
                    worked=task_summary["passed"],
                    split=task.get("split"),
                    category=task.get("category"))
            except KeyError:
                pass
        record = {
            "mode": mode,
            "label": MODE_LABELS[mode],
            "adapter": adapter.name,
            "max_tokens": getattr(adapter, "max_tokens", None),
            "strip_fences": True,
            "temperature": 0.0,
            "reasoning": "off",
            "memory": {"retrieval": retrieval},
            "summary": summary,
        }
        out_path = out_dir / (task["id"] + ".json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        records.append(record)
        print("  wrote %s" % out_path)
    return summarize_mode(mode, records), records


def run_all(tasks, exposure_tasks, out_root, inner, recorded):
    """Run both modes; write summary.json; return the payload."""
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    mem_dir = out_root / "memory" / "patches"
    if mem_dir.exists():
        shutil.rmtree(mem_dir)
    patch_memory, stored = seed_patch_memory(
        mem_dir, exposure_tasks, recorded
    )
    print("seeded %d verified patches from %d exposure tasks" % (
        stored, len(exposure_tasks)))
    skill_dir = out_root / "memory" / "skills"
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    skill_store = build_skill_store(skill_dir)
    print("skill families: %d" % len(skill_store.list_families()))
    summaries = {}
    for mode in MODES:
        print("mode %s (%s): %d pilot tasks" % (
            mode, MODE_LABELS[mode], len(tasks)))
        summary, _records = run_mode(
            mode, tasks, out_root, inner, skill_store, patch_memory
        )
        summaries[mode] = summary
        print("mode %s: %d/%d passed" % (
            mode, summary["tasks_passed"], summary["tasks_total"]))
    payload = {
        "experiment": "semantic-compare-pilot",
        "pilot_task_ids": [t["id"] for t in sorted(
            tasks, key=lambda t: t["id"])],
        "modes": MODES,
        "mode_labels": MODE_LABELS,
        "patches_seeded": stored,
        "summaries": summaries,
        "deltas_vs_code_only": compute_mode_deltas(
            summaries, baseline="code-only"),
    }
    out_path = out_root / "summary.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print("wrote %s" % out_path)
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Semantic+code vs code-only pilot")
    parser.add_argument("--tasks", default=str(_ROOT / "benchmarks" / "family-a"))
    parser.add_argument("--adapter", choices=("stub", "cerebras"), default="stub")
    parser.add_argument("--recorded", default=None)
    parser.add_argument("--artifacts", default=str(
        _ROOT / "artifacts" / "semantic-compare"))
    parser.add_argument("--max-tokens", type=int, default=128)
    args = parser.parse_args(argv)

    tasks = runner.load_tasks(Path(args.tasks))
    problems = runner.validate_tasks(tasks)
    if problems:
        print("task schema problems:", file=sys.stderr)
        for problem in problems:
            print("  %s" % problem, file=sys.stderr)
        return 2
    pilot = [t for t in tasks if t["id"] in PILOT_TASK_IDS]
    if len(pilot) != len(PILOT_TASK_IDS):
        print("expected %d pilot tasks, got %d" % (
            len(PILOT_TASK_IDS), len(pilot)), file=sys.stderr)
        return 2
    exposure = [t for t in tasks if t["split"] == "exposure"]

    if args.adapter == "stub":
        if not args.recorded:
            print("--recorded FILE is required with --adapter stub",
                  file=sys.stderr)
            return 2
        inner = runner.StubAdapter(Path(args.recorded))
    else:
        inner = runner.CerebrasAdapter(max_tokens=args.max_tokens)
    recorded_path = args.recorded or str(
        _ROOT / "benchmarks" / "family-a" / "recorded"
        / "stub_all_pass.json")
    if args.recorded is None:
        print("note: seeding patch memory from stub_all_pass.json "
              "(verified offline)")
    with open(recorded_path, encoding="utf-8") as fh:
        recorded = json.load(fh)
    payload = run_all(pilot, exposure, args.artifacts, inner, recorded)
    ok = all(
        s["tasks_passed"] == s["tasks_total"]
        for s in payload["summaries"].values()
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

