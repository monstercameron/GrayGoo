"""Canonical A/B/C/D comparison on Family R (reuse thesis).

Arms (session goal 2026-10-05):

    A  no memory: every check solved from scratch by the model.
    B  prior code retrieved into prompt: top-2 exposure exemplars
       injected per task (offline keyword retrieval), model still
       solves every check.
    C  executable capability reuse: an applicable single capability
       executes with ZERO model calls; otherwise the model solves
       from scratch (no injection).
    D  executable capability composition: a matching composition
       executes first (zero calls), then single-capability reuse,
       then the model from scratch.

Outcomes per task (outcomes.py): REUSE / COMPOSE / ADAPT / NOVEL.
Task rollup: any model call -> ADAPT (if retrieved) else NOVEL;
else widest execution decides (width 1 -> REUSE, >1 -> COMPOSE).

Offline flow check (no live calls):

    uv run python benchmarks/run_abcd.py --adapter stub

Live (7 tasks, <= 13 model calls per arm):

    uv run python benchmarks/run_abcd.py --adapter cerebras --arms abcd

Outputs (all under artifacts/):

    artifacts/abcd/<a,b,c,d>-summary.json   arm aggregate + per-task rows
    artifacts/abcd/<a,b,c,d>/R-*.json        per-task results
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

import runner  # noqa: E402
import text_memory as text_mem_mod  # noqa: E402
import execaps  # noqa: E402
import outcomes  # noqa: E402

ARMS = ("a", "b", "c", "d")

# Exposure sources behind the seed capabilities (arm B memory + docs).
B_SOURCES = ("A-EXP-01", "A-EXP-05", "A-EXP-09", "A-EXP-13")


class CountingStub(runner.StubAdapter):
    """StubAdapter that counts calls and estimates tokens per solve.

    runner.run attributes usage ONLY via adapter.history, which the
    plain stub lacks -- without this wrapper the offline A/B arms
    would report 0 calls. Token figures are chars/4 ESTIMATES
    (entries carry "estimated": True); live runs use real usage.
    """

    def __init__(self, recorded_path):
        super().__init__(recorded_path)
        self.history = []

    def solve(self, task, check_index, check_input):
        output = super().solve(task, check_index, check_input)
        self.history.append({
            "input_tokens": text_mem_mod.estimate_tokens(
                task.get("prompt", "") + check_input),
            "output_tokens": text_mem_mod.estimate_tokens(output),
            "latency_ms": 0.0,
            "cost_usd": 0.0,
            "estimated": True,
        })
        return output


class InjectionAdapter(runner.ModelAdapter):
    """Arm B: inject retrieved exemplars, then solve via inner (a call)."""

    def __init__(self, inner, memory, top_k=2, max_chars=320):
        self.inner = inner
        self.memory = memory
        self.top_k = top_k
        self.max_chars = max_chars
        self.name = "%s+inject" % getattr(inner, "name", "?")
        self.max_tokens = getattr(inner, "max_tokens", None)
        self.retrieved_by_task = {}

    @property
    def history(self):
        return getattr(self.inner, "history", None)

    def solve(self, task, check_index, check_input):
        task_id = task.get("id", "?")
        if task_id not in self.retrieved_by_task:
            retrieved = self.memory.retrieve(task, top_k=self.top_k)
            block = self.memory.format_block(
                retrieved, max_chars_per_example=self.max_chars)
            self.retrieved_by_task[task_id] = (block, retrieved)
        block, retrieved = self.retrieved_by_task[task_id]
        call_task = task
        if block:
            call_task = dict(task)
            call_task["prompt"] = block + task.get("prompt", "")
        return self.inner.solve(call_task, check_index, check_input)


class ExecAdapter(runner.ModelAdapter):
    """Arms C/D: execute capabilities (zero calls) or fall back to inner.

    Arm C: single-capability reuse only (compositions disabled).
    Arm D: composition first, then single capability, then the model.
    ``checks`` records per-check (executed_width, retrieved) for the
    task rollup. Usage history delegates to inner, so executed checks
    cost nothing and fallback checks cost exactly one model call.
    """

    def __init__(self, inner, registry, allow_compose):
        self.inner = inner
        self.registry = registry
        self.allow_compose = allow_compose
        self.name = "%s+%s" % (getattr(inner, "name", "?"),
                               "compose" if allow_compose else "exec")
        self.max_tokens = getattr(inner, "max_tokens", None)
        self.checks = {}
        self.used_by_task = {}

    @property
    def history(self):
        return getattr(self.inner, "history", None)

    def solve(self, task, check_index, check_input):
        task_id = task.get("id", "?")
        key = (task_id, check_index)
        if self.allow_compose:
            comp = self.registry.find_composition(task, check_input)
            if comp is not None:
                try:
                    output = comp.execute(self.registry, check_input)
                except Exception:
                    comp = None
                else:
                    self.checks[key] = {"executed": comp.executed_count,
                                        "retrieved": False,
                                        "via": comp.id}
                    self.used_by_task.setdefault(task_id, []).append(
                        comp.id)
                    return output
        found = self.registry.find_for_task(task, check_input)
        if found:
            try:
                output = found[0].execute(check_input)
            except Exception:
                found = None
            else:
                self.checks[key] = {"executed": 1, "retrieved": False,
                                    "via": found[0].id}
                self.used_by_task.setdefault(task_id, []).append(
                    found[0].id)
                return output
        output = self.inner.solve(task, check_index, check_input)
        self.checks[key] = {"executed": 0, "retrieved": False, "via": None}
        return output


def seed_b_memory():
    """Arm B memory: verified exposure exemplars behind the seeds."""
    with open(_HERE / "family-a" / "exposure.json",
              encoding="utf-8") as fh:
        exposure = {t["id"]: t for t in json.load(fh)}
    with open(_HERE / "family-a" / "recorded" / "stub_all_pass.json",
              encoding="utf-8") as fh:
        recorded = json.load(fh)
    memory = text_mem_mod.TextMemory()
    for task_id in B_SOURCES:
        task = exposure[task_id]
        for j, check in enumerate(task["checks"]):
            outputs = recorded.get(task_id, [])
            memory.add_success(task, j, check["input"],
                               outputs[j] if j < len(outputs) else "")
    return memory


def rollup(task, task_summary, adapter):
    """Per-task outcome record: path + cost + pass/fail."""
    task_id = task["id"]
    checks = task_summary.get("checks", [])
    model_calls = 0
    width = 0
    retrieved = False
    vias = []
    per_check = getattr(adapter, "checks", {})
    for j in range(len(task["checks"])):
        info = per_check.get((task_id, j))
        if info is None:
            # Arms A/B solve every check via the model.
            model_calls += 1
            block_info = getattr(adapter, "retrieved_by_task", {}).get(
                task_id)
            if block_info and block_info[0]:
                retrieved = True
        elif info.get("executed", 0) > 0:
            width = max(width, info["executed"])
            vias.append(info.get("via"))
        else:
            model_calls += 1
    if model_calls > 0:
        outcome = (outcomes.ADAPT if retrieved else outcomes.NOVEL)
    elif width > 1:
        outcome = outcomes.COMPOSE
    elif width == 1:
        outcome = outcomes.REUSE
    else:
        outcome = outcomes.NOVEL
    totals = task_summary.get("totals", {})
    return {"id": task_id,
            "outcome": outcome,
            "via": vias,
            "calls": model_calls,
            "tokens": totals.get("total_tokens", 0),
            "passed": bool(task_summary.get("passed", False)),
            "checks": [{"passed": c.get("passed", False)} for c in checks]}


def run_arm(arm, tasks, make_inner, out_root):
    """Run one arm; write per-task files; return the summary dict."""
    out_dir = Path(out_root) / arm
    out_dir.mkdir(parents=True, exist_ok=True)
    inner = make_inner()
    registry = execaps.ExecRegistry()
    if arm == "a":
        adapter = inner
    elif arm == "b":
        adapter = InjectionAdapter(inner, seed_b_memory())
    elif arm == "c":
        adapter = ExecAdapter(inner, registry, allow_compose=False)
    elif arm == "d":
        adapter = ExecAdapter(inner, registry, allow_compose=True)
    else:
        raise ValueError("unknown arm: %r" % (arm,))
    summary = runner.run(tasks, adapter, strip_fences=True)
    by_id = {t["id"]: t for t in summary["tasks"]}
    records = []
    for task in tasks:
        record = rollup(task, by_id[task["id"]], adapter)
        records.append(record)
        with open(out_dir / (task["id"] + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"arm": arm, "record": record,
                       "summary": by_id[task["id"]]},
                      fh, indent=2)
    agg = outcomes.aggregate(records)
    solved = sum(1 for r in records if r["passed"])
    payload = {"arm": arm,
               "tasks_passed": "%d/%d" % (solved, len(records)),
               "aggregate": agg,
               "compression": outcomes.compression(
                   solved, registry.capability_count()
                   if arm in ("c", "d") else 0),
               "capability_count": (registry.capability_count()
                                    if arm in ("c", "d") else 0),
               "records": records,
               "usage": summary.get("usage", {})}
    with open(Path(out_root) / ("%s-summary.json" % arm), "w",
              encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="Canonical A/B/C/D")
    parser.add_argument("--tasks", default=str(_HERE / "family-r"))
    parser.add_argument("--adapter", choices=("stub", "cerebras"),
                        default="stub")
    parser.add_argument("--recorded", default=str(
        _HERE / "family-r" / "recorded" / "stub_all_pass.json"))
    parser.add_argument("--arms", default="abcd")
    parser.add_argument("--artifacts", default=str(_ROOT / "artifacts"
                                                   / "abcd"))
    parser.add_argument("--max-tokens", type=int, default=128)
    args = parser.parse_args(argv)

    arms = [a for a in args.arms.lower() if a in ARMS]
    if not arms:
        raise SystemExit("no valid arms in %r" % (args.arms,))
    tasks = sorted(runner.load_tasks(Path(args.tasks)),
                   key=lambda t: t["id"])
    problems = runner.validate_tasks(tasks)
    if problems:
        raise SystemExit("invalid tasks:\n" + "\n".join(problems))

    def make_inner():
        if args.adapter == "stub":
            return CountingStub(Path(args.recorded))
        return runner.CerebrasAdapter(max_tokens=args.max_tokens)

    out_root = Path(args.artifacts)
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    payloads = {}
    for arm in arms:
        payload = run_arm(arm, tasks, make_inner, out_root)
        payloads[arm] = payload
        agg = payload["aggregate"]
        print("arm %s: %s passed, outcomes=%s, calls/task=%.3f, "
              "tokens/task=%.1f" % (
                  arm.upper(), payload["tasks_passed"],
                  agg["outcomes"], agg["calls_per_task"],
                  agg["tokens_per_task"]), flush=True)
    with open(out_root / "comparison.json", "w", encoding="utf-8") as fh:
        json.dump({"arms": payloads}, fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
