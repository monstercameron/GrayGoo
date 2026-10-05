"""Pilot for the lesson key experiment (memory.md section 34).

Runs a PILOT subset (the 8 Family A transfer tasks, 16 checks) under 4
conditions — A no-lesson, B raw previous transcripts (truncated
excerpts), C retrieved distilled lessons (lessons.py registry seeded +
tags), D lessons + executable patch memory — measuring first-pass
success, repair loops (0 here: no repair in protocol), tokens, calls,
latency, and held-out success (all 8 pilot tasks are held-out
transfer tasks, so held-out success == overall success here).

Baselines standard conditions (documents/baseline-a.md): stripped
scoring, temperature 0.0, reasoning off, max_tokens 128, one call per
check, no retries, no repair.

Live usage (64 calls for all 4 conditions):
    uv run python experiments/key_experiment.py --adapter cerebras

Offline flow check (no live calls):
    uv run python experiments/key_experiment.py --adapter stub \
        --recorded benchmarks/family-a/recorded/stub_all_pass.json

Outputs (all under artifacts/):
    artifacts/lesson-key/<A,B,C,D>/<TASK-ID>.json  per-task results
    artifacts/lesson-key/summary.json               condition table + deltas
    artifacts/lesson-key/memory/...                 patch store for condition D
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

import lessons  # noqa: E402

CONDITIONS = ("A", "B", "C", "D")
CONDITION_LABELS = {
    "A": "no-lesson memory",
    "B": "raw previous transcripts",
    "C": "retrieved distilled lessons",
    "D": "lessons + executable patch memory",
}

PILOT_TASK_IDS = tuple("A-TRN-%02d" % n for n in range(1, 9))

# Retrieval budgets (tiny contexts only).
LESSONS_TOP_K = 3
PATCHES_TOP_K = 2
MAX_CHARS_PER_EXAMPLE = 320
MAX_CHARS_LESSON_BLOCK = 900
MAX_CHARS_TRANSCRIPT_BLOCK = 900

# Condition B: raw previous-transcript excerpts, one pair per category.
# Synthesized offline fixtures modeling prior exposure-family mutation
# trajectories (goal -> attempt -> failure -> repair -> success); each
# excerpt is truncated at injection time. No model calls involved.
TRANSCRIPTS = {
    "dates": [
        ("A-EXP-02/traj", (
            "goal: convert 'YYYY-MM-DD HH:MM +HHMM' to UTC ISO-8601. "
            "attempt1: parsed with local tz attached, added offset "
            "instead of subtracting -> '2026-10-05T16:30:00Z' wrong. "
            "failure: wrong-output, offset sign flipped. repair: "
            "utc = local - offset; re-ran 3 cases incl day rollover. "
            "attempt2: '2026-10-05T12:30:00Z' pass. note: always "
            "subtract positive offsets; test midnight rollover."
        )),
        ("A-EXP-16/traj", (
            "goal: YYYY-MM-DD -> lowercase weekday. attempt1: "
            "Zeller table off by one for Jan/Feb (forgot month shift) "
            "-> wrong-output on 2/5 cases. failure: edge-case. "
            "repair: treat Jan/Feb as months 13/14 of prior year; "
            "verified against epoch 1970-01-01=thursday. attempt2 "
            "pass. note: anchor calendar math to a known weekday."
        )),
    ],
    "csv": [
        ("A-EXP-06/traj", (
            "goal: CSV with embedded newlines -> JSON. attempt1: "
            "split input on newlines first, then commas -> quoted "
            "multi-line field shattered into 3 rows. failure: "
            "wrong-output. repair: scan rows respecting quote state "
            "before splitting fields; keep \\n inside quoted values. "
            "attempt2 pass. note: never line-split before handling "
            "RFC4180 quoting."
        )),
        ("A-EXP-07/traj", (
            "goal: CSV doubled-quote unescaping. attempt1: stripped "
            "all quotes, so '\"\"hi\"\"' became 'hi' without inner "
            "quotes -> wrong-output. failure: wrong-output. repair: "
            "collapse paired quotes to one literal quote only inside "
            "quoted fields. attempt2 pass. note: unescape, don't strip."
        )),
    ],
    "records": [
        ("A-EXP-09/traj", (
            "goal: flatten nested JSON with [i] indices. attempt1: "
            "dropped empty arrays, emitted nothing for 'z': [] -> "
            "wrong-output. failure: edge-case. repair: preserve empty "
            "containers as values; recurse only into non-empty. "
            "attempt2 pass. note: empties are data, not absence."
        )),
        ("A-EXP-18/traj", (
            "goal: money strings -> integer cents. attempt1: float() "
            "then *100 -> 1234.56 gave 123455 (float error). failure: "
            "wrong-output. repair: parse dollars/cents as integers "
            "from the string; strip $/USD/commas first. attempt2 pass. "
            "note: never route money through floats."
        )),
    ],
    "logs": [
        ("A-EXP-13/traj", (
            "goal: Common Log Format -> JSON. attempt1: split on "
            "spaces -> request field '\"GET /a.gif HTTP/1.0\"' broke "
            "into 3 pieces. failure: wrong-output. repair: regex with "
            "quoted/bracket groups; map '-' user/bytes to null/0. "
            "attempt2 pass. note: match structure, don't word-split."
        )),
        ("A-EXP-17/traj", (
            "goal: keep ERROR lines as {ts, msg}. attempt1: matched "
            "'error' case-insensitively, caught ' Terror' message text "
            "-> wrong-output. failure: edge-case. repair: LEVEL field "
            "must equal ERROR exactly (field 2). attempt2 pass. note: "
            "match the field, not the substring."
        )),
    ],
}


def task_tags(task):
    """Offline tag query for lesson retrieval (category + family)."""
    tags = []
    for tag in (task.get("category", ""), "family-a", "parsing",
                "repair", "debugging"):
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def build_registry():
    """Seeded lesson registry (lessons.py L2 seeds, all active)."""
    return lessons.LessonRegistry(lessons.seed_10_20())


def format_lesson_block(retrieved, max_chars=MAX_CHARS_LESSON_BLOCK):
    """Format retrieved lessons as a tiny prompt-injection block."""
    if not retrieved:
        return ""
    lines = ["Relevant lessons (apply where they fit):"]
    for lesson in retrieved:
        text = "- [%s] %s (when: %s)" % (
            lesson.get("id", "?"),
            lesson.get("statement", ""),
            (lesson.get("applies_when") or {}).get("when", ""),
        )
        lines.append(text)
    block = "\n".join(lines) + "\n"
    if len(block) > max_chars:
        block = block[: max_chars - 1] + "…"
    return block


def format_transcript_block(task, max_chars=MAX_CHARS_TRANSCRIPT_BLOCK):
    """Format raw-transcript excerpts for condition B (truncated)."""
    excerpts = TRANSCRIPTS.get(task.get("category", ""), [])
    if not excerpts:
        return ""
    lines = ["Previous transcripts, same family (raw excerpts):"]
    for name, text in excerpts:
        lines.append("[%s] %s" % (name, text))
    block = "\n".join(lines) + "\n"
    if len(block) > max_chars:
        block = block[: max_chars - 1] + "…"
    return block


def _get_patch_store_cls():
    """Root patches.PatchStore, imported lazily (read-only reuse)."""
    import patches  # noqa: PLC0415

    cls = getattr(patches, "PatchStore", None)
    if cls is None:
        raise RuntimeError(
            "patch memory unavailable: patches.PatchStore not found"
        )
    return cls


class PilotPatchMemory:
    """Minimal executable patch memory over root PatchStore.

    Same layout as benchmarks/patch_memory_adapter.py (task_id,
    candidate prompt/input/output/check, family "A", tags
    [category, family-a, task-id]) so D-condition reuse stays
    comparable; retrieval is offline tag overlap, no model calls.
    """

    def __init__(self, directory):
        cls = _get_patch_store_cls()
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        self.store = cls(str(path))

    def add_success(self, task, check_index, check_input, output):
        task_id = task.get("id", "?")
        category = task.get("category", "")
        candidate = {
            "prompt": task.get("prompt", ""),
            "input": check_input,
            "output": output,
            "check": int(check_index),
        }
        tags = [t for t in (category, "family-a", task_id) if t]
        return self.store.save_patch(
            task_id, candidate, task_family="A", tags=tags
        )

    def retrieve(self, task, limit=PATCHES_TOP_K):
        category = task.get("category", "")
        patches = self.store.find_for_task(
            task_family="A", tags=[category] if category else None
        )
        if limit is not None and limit >= 0:
            patches = patches[:limit]
        return patches

    def format_block(self, patches, max_chars_per_example=320):
        if not patches:
            return ""
        lines = ["Retrieved procedures from solved tasks (adapt to this input):"]
        for i, patch in enumerate(patches, 1):
            candidate = patch.get("candidate", {}) or {}
            if not isinstance(candidate, dict):
                candidate = {"output": candidate}
            text = "Procedure %d (%s, %s):\ninput: %s\noutput: %s" % (
                i,
                patch.get("task_id", "?"),
                ",".join(patch.get("tags", [])[:2]),
                candidate.get("input", ""),
                candidate.get("output", ""),
            )
            if len(text) > max_chars_per_example:
                text = text[: max_chars_per_example - 1] + "…"
            lines.append(text)
        return "\n".join(lines) + "\n"

    def record_reuse(self, patch, task, helped, **extra):
        fields = {
            "held_out": task.get("split") == "transfer",
            "split": task.get("split"),
            "category": task.get("category"),
            "success": bool(helped),
        }
        fields.update(extra)
        return self.store.record_reuse(
            patch.get("patch_id"), task.get("id"), helped=bool(helped),
            **fields
        )


def seed_patch_memory(directory, exposure_tasks, recorded):
    """Persist verified exposure successes as executable-memory patches.

    A recorded output is stored only when it passes the task's own
    check offline (runner.compare on the recorded text), so every
    patch is a verified success. Returns (patch_memory, stored).
    """
    memory = PilotPatchMemory(directory)
    stored = 0
    for task in exposure_tasks:
        outputs = recorded.get(task["id"], [])
        for j, check in enumerate(task["checks"]):
            if j >= len(outputs):
                continue
            actual = outputs[j]
            if actual is None:
                continue
            if runner.compare(check["expected"], actual, check["compare"]):
                memory.add_success(task, j, check["input"], actual)
                stored += 1
    return memory, stored


class ConditionAdapter(runner.ModelAdapter):
    """Wrap an inner adapter with a per-condition prompt block.

    ``build_block(task)`` returns (block_text, retrieval_info).
    Retrieval runs once per task (first check) and is reused for
    later checks of the same task. Usage history delegates to the
    inner adapter (as runner.run expects).
    """

    def __init__(self, inner, build_block, name_suffix):
        self.inner = inner
        self._build_block = build_block
        self.name = "%s+%s" % (getattr(inner, "name", "?"), name_suffix)
        self.max_tokens = getattr(inner, "max_tokens", None)
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
        return self.inner.solve(call_task, check_index, check_input)


def make_block_builder(condition, registry, patch_memory):
    """Return build_block(task) -> (block, info) for one condition."""

    def build_block(task):
        if condition == "A":
            return "", {"condition": "A", "retrieval": "none"}
        if condition == "B":
            block = format_transcript_block(task)
            return block, {
                "condition": "B",
                "excerpts": [
                    name
                    for name, _ in TRANSCRIPTS.get(
                        task.get("category", ""), []
                    )
                ],
                "injected_chars": len(block),
            }
        if condition == "C":
            retrieved = registry.retrieve_for_task(
                task_tags(task), k=LESSONS_TOP_K
            )
            block = format_lesson_block(retrieved)
            return block, {
                "condition": "C",
                "lessons": [lesson["id"] for lesson in retrieved],
                "injected_chars": len(block),
            }
        if condition == "D":
            retrieved = registry.retrieve_for_task(
                task_tags(task), k=LESSONS_TOP_K
            )
            lesson_block = format_lesson_block(retrieved)
            patches = patch_memory.retrieve(task, limit=PATCHES_TOP_K)
            patch_block = patch_memory.format_block(
                patches, max_chars_per_example=MAX_CHARS_PER_EXAMPLE
            )
            block = lesson_block + patch_block
            return block, {
                "condition": "D",
                "lessons": [lesson["id"] for lesson in retrieved],
                "patches": [
                    {
                        "patch_id": p.get("patch_id"),
                        "task_id": p.get("task_id"),
                    }
                    for p in patches
                ],
                "injected_chars": len(block),
            }
        raise ValueError("unknown condition %r" % (condition,))

    return build_block


def summarize_condition(condition, records):
    """Aggregate per-task records into a condition summary (offline).

    Each record carries ``summary`` (runner.summarize output for one
    task) and ``memory`` (retrieval info). Returns tasks/success
    counts and rates, usage totals + per-task means, repair loops
    (always 0: no repair in protocol), held-out success (all pilot
    tasks are held-out transfer), and fail ids.
    """
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
        "condition": condition,
        "label": CONDITION_LABELS[condition],
        "tasks_total": tasks_total,
        "tasks_passed": passed,
        "success_rate": round(passed / tasks_total, 4) if tasks_total else 0.0,
        "first_pass_success_rate": (
            round(passed / tasks_total, 4) if tasks_total else 0.0
        ),
        "repair_loops": 0,
        "repair_loops_note": "no repair in protocol (one call per check)",
        "held_out_success_rate": (
            round(passed / tasks_total, 4) if tasks_total else 0.0
        ),
        "held_out_note": "all %d pilot tasks are held-out transfer" % tasks_total,
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


def compute_deltas(summaries, baseline="A"):
    """Condition-minus-baseline deltas for success rate and resources."""
    if baseline not in summaries:
        return []
    base = summaries[baseline]
    base_rate = base["success_rate"]
    base_tokens = base["per_task_mean"]["total_tokens"]
    base_calls = base["per_task_mean"]["calls"]
    base_latency = base["per_task_mean"]["latency_ms"]
    deltas = []
    for condition in CONDITIONS:
        if condition == baseline or condition not in summaries:
            continue
        cur = summaries[condition]
        deltas.append({
            "condition": condition,
            "baseline": baseline,
            "success_rate_delta": round(cur["success_rate"] - base_rate, 4),
            "tasks_delta": cur["tasks_passed"] - base["tasks_passed"],
            "tokens_per_task_delta": round(
                cur["per_task_mean"]["total_tokens"] - base_tokens, 1
            ),
            "calls_per_task_delta": round(
                cur["per_task_mean"]["calls"] - base_calls, 3
            ),
            "latency_ms_per_task_delta": round(
                cur["per_task_mean"]["latency_ms"] - base_latency, 1
            ),
        })
    return deltas


def run_condition(condition, tasks, out_root, inner, registry, patch_memory):
    """Run one condition over the pilot tasks; return (summary, records)."""
    out_dir = Path(out_root) / condition
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    builder = make_block_builder(condition, registry, patch_memory)
    adapter = ConditionAdapter(inner, builder, "key" + condition)
    records = []
    for task in sorted(tasks, key=lambda t: t["id"]):
        summary = runner.run(
            [task], adapter, verbose=False, strip_fences=True
        )
        task_summary = summary["tasks"][0]
        retrieval = adapter.retrieval_by_task.get(task["id"], {})
        if condition == "D" and patch_memory is not None:
            for item in retrieval.get("patches", []) or []:
                patch = patch_memory.store.get_patch(
                    item.get("patch_id")
                ) or {"patch_id": item.get("patch_id")}
                patch_memory.record_reuse(
                    patch, task, helped=task_summary["passed"]
                )
        record = {
            "condition": condition,
            "label": CONDITION_LABELS[condition],
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
    return summarize_condition(condition, records), records


def run_all(tasks, exposure_tasks, out_root, inner, recorded):
    """Run conditions A/B/C/D; write summary.json; return the payload."""
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    registry = build_registry()
    patch_memory = None
    stored = 0
    if recorded is not None:
        mem_dir = out_root / "memory" / "patches-d"
        if mem_dir.exists():
            shutil.rmtree(mem_dir)
        patch_memory, stored = seed_patch_memory(
            mem_dir, exposure_tasks, recorded
        )
        print("seeded %d verified patches from %d exposure tasks" % (
            stored, len(exposure_tasks)))
    summaries = {}
    for condition in CONDITIONS:
        if condition == "D" and patch_memory is None:
            raise RuntimeError(
                "condition D needs --recorded FILE to seed patch memory"
            )
        print("condition %s (%s): %d pilot tasks" % (
            condition, CONDITION_LABELS[condition], len(tasks)))
        summary, _records = run_condition(
            condition, tasks, out_root, inner, registry, patch_memory
        )
        summaries[condition] = summary
        print("condition %s: %d/%d passed" % (
            condition, summary["tasks_passed"], summary["tasks_total"]))
    payload = {
        "experiment": "lesson-key-pilot",
        "pilot_task_ids": [t["id"] for t in sorted(
            tasks, key=lambda t: t["id"])],
        "conditions": CONDITIONS,
        "condition_labels": CONDITION_LABELS,
        "patches_seeded": stored,
        "summaries": summaries,
        "deltas_vs_a": compute_deltas(summaries, baseline="A"),
    }
    out_path = out_root / "summary.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print("wrote %s" % out_path)
    return payload


def main(argv=None) -> int:
    global CONDITIONS  # noqa: PLW0603 (single-condition pilot rerun)
    parser = argparse.ArgumentParser(
        description="Lesson key experiment pilot (A/B/C/D)")
    parser.add_argument("--tasks", default=str(_ROOT / "benchmarks" / "family-a"))
    parser.add_argument("--adapter", choices=("stub", "cerebras"), default="stub")
    parser.add_argument("--recorded", default=None)
    parser.add_argument("--artifacts", default=str(
        _ROOT / "artifacts" / "lesson-key"))
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--only", default=None,
                        help="run one condition only (A, B, C, or D)")
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
    with open(args.recorded, encoding="utf-8") if args.recorded else open(
            _ROOT / "benchmarks" / "family-a" / "recorded"
            / "stub_all_pass.json", encoding="utf-8") as fh:
        recorded = json.load(fh)
    if args.recorded is None:
        print("note: seeding D patch memory from stub_all_pass.json "
              "(verified offline)")

    if args.only:
        if args.only not in CONDITIONS:
            print("bad --only %r (want one of A B C D)" % args.only,
                  file=sys.stderr)
            return 2
        CONDITIONS = (args.only,)
    payload = run_all(pilot, exposure, args.artifacts, inner, recorded)
    ok = all(
        s["tasks_passed"] == s["tasks_total"]
        for s in payload["summaries"].values()
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())


