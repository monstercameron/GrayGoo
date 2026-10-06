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
    uv run python benchmarks/run_abcd.py --adapter stub --tasks \\
        benchmarks/family-w --split transfer --artifacts artifacts/abcd-w

Live (family-r: 8 tasks; family-w transfer: 6 tasks):

    uv run python benchmarks/run_abcd.py --adapter cerebras --arms abcd

Outputs (all under artifacts/):

    artifacts/abcd/<a,b,c,d>-summary.json   arm aggregate + per-task rows
    artifacts/abcd/<a,b,c,d>/R-*.json        per-task results
"""

import argparse
import json
import random
import shutil
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

import runner  # noqa: E402
import text_memory as text_mem_mod  # noqa: E402
import execaps  # noqa: E402
import outcomes  # noqa: E402
import distill  # noqa: E402

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
    """Arms C/D: the execution hierarchy (directive primary objective).

    Per check: composition (arm D only) -> single-capability reuse ->
    adaptation (closest retrieved exemplar + minimal model call) ->
    novel synthesis (model from scratch). ``adapt_memory`` None means
    no adaptation rung (scratch fallback, the pre-hierarchy path).
    ``checks`` records per-check (executed_width, retrieved) for the
    task rollup; ``adapted_by_task`` marks tasks that used the
    adaptation rung. Usage history delegates to inner, so executed
    checks cost nothing and model checks cost exactly one call.
    """

    def __init__(self, inner, registry, allow_compose, adapt_memory=None):
        self.inner = inner
        self.registry = registry
        self.allow_compose = allow_compose
        self.adapt_memory = adapt_memory
        self.name = "%s+%s" % (getattr(inner, "name", "?"),
                               "compose" if allow_compose else "exec")
        self.max_tokens = getattr(inner, "max_tokens", None)
        self.checks = {}
        self.used_by_task = {}
        self.adapted_by_task = {}
        self.declined_by_task = {}
        self._adapt_cache = {}

    def _adapt_block(self, task):
        """Minimal adaptation context (§11): top-1 exemplar + declines.

        Returns "" when nothing retrieves (the novelty rung). The
        declined line names same-category capabilities that abstained,
        so the model sees failed-reuse evidence instead of a dump.
        """
        task_id = task.get("id", "?")
        if task_id not in self._adapt_cache:
            retrieved = self.adapt_memory.retrieve(task, top_k=1)
            block = self.adapt_memory.format_block_concise(retrieved)
            declined = sorted(
                cap.id for cap in self.registry._caps.values()
                if cap.category == task.get("category"))
            self.declined_by_task[task_id] = declined
            if retrieved:
                if declined:
                    block += ("Direct execution declined for: %s "
                              "(contract mismatch).\n"
                              % ", ".join(declined))
                self.adapted_by_task[task_id] = block
                self._adapt_cache[task_id] = block
            else:
                self._adapt_cache[task_id] = ""
        return self._adapt_cache[task_id]

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
        block = ""
        if self.adapt_memory is not None:
            block = self._adapt_block(task)
        call_task = task
        if block:
            call_task = dict(task)
            call_task["prompt"] = block + task.get("prompt", "")
        output = self.inner.solve(call_task, check_index, check_input)
        self.checks[key] = {"executed": 0, "retrieved": bool(block),
                            "via": None}
        return output


def seed_b_memory(tasks_dir=None):
    """Arm B / adaptation memory: verified exposure exemplars.

    When ``tasks_dir`` holds its own exposure split (Family W), seed
    from every exposure task there; otherwise (Family R, all
    transfer) seed from the Family A exposure sources behind the
    seeds. Recorded outputs are the verified exemplars, so seeding
    costs zero model calls.
    """
    tasks_dir = Path(tasks_dir) if tasks_dir else None
    exposure_path = (tasks_dir / "exposure.json"
                     if tasks_dir else None)
    if exposure_path and exposure_path.exists():
        with open(exposure_path, encoding="utf-8") as fh:
            exposure = {t["id"]: t for t in json.load(fh)}
        with open(tasks_dir / "recorded" / "stub_all_pass.json",
                  encoding="utf-8") as fh:
            recorded = json.load(fh)
        sources = sorted(exposure)
    else:
        with open(_HERE / "family-a" / "exposure.json",
                  encoding="utf-8") as fh:
            exposure = {t["id"]: t for t in json.load(fh)}
        with open(_HERE / "family-a" / "recorded" / "stub_all_pass.json",
                  encoding="utf-8") as fh:
            recorded = json.load(fh)
        sources = list(B_SOURCES)
    memory = text_mem_mod.TextMemory()
    for task_id in sources:
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
            if info.get("retrieved"):
                retrieved = True
        else:
            model_calls += 1
            if info.get("retrieved"):
                retrieved = True
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
            "input_tokens": totals.get("input_tokens", 0),
            "output_tokens": totals.get("output_tokens", 0),
            "passed": bool(task_summary.get("passed", False)),
            "checks": [{"passed": c.get("passed", False)} for c in checks]}


def record_evidence(registry, tasks, records):
    """Post-scoring evidence: trusted pass/fail -> capability ledgers.

    Every executed task records positive evidence on its ``via``
    artifacts when the trusted checks pass, negative evidence when
    they fail (directive §1: verification failure is negative
    evidence). Composition outcomes propagate to member capabilities:
    composite reuse counts as reuse for promotion (§9).
    """
    for record in records:
        task_id = record["id"]
        helped = bool(record["passed"])
        for via in dict.fromkeys(record.get("via", [])):
            comp = registry._comps.get(via)
            if comp is not None:
                comp.record(task_id, helped)
                for member in comp.uses:
                    cap = registry.get(member)
                    if cap is not None:
                        cap.record(task_id, helped)
            else:
                cap = registry.get(via)
                if cap is not None:
                    cap.record(task_id, helped)


def evidence_summary(registry):
    """Per-artifact evidence envelopes for promotion accounting (§9)."""
    summary = {}
    for cap in registry._caps.values():
        summary[cap.id] = cap.evidence()
    for comp in registry._comps.values():
        summary[comp.id] = comp.evidence()
    return summary


def applicability_metrics(tasks, records, registry, adapter):
    """Applicability quality (directive §2), measured post-hoc.

    Precision/harmful-reuse come straight from trusted scoring of
    executed tasks. False negatives need a counterfactual: for each
    model-solved check, trial-execute every same-category capability
    and count checks where a non-firing capability WOULD have passed.
    This uses hidden answers for MEASUREMENT only -- it never alters
    the execution path, so the novelty of the run is intact.
    """
    by_id = {t["id"]: t for t in tasks}
    exec_tasks = [r for r in records if r.get("via")]
    exec_passed = sum(1 for r in exec_tasks if r.get("passed"))
    checks_info = getattr(adapter, "checks", {})
    fallback = 0
    false_negative = 0
    for task in tasks:
        for j, check in enumerate(task.get("checks", [])):
            info = checks_info.get((task["id"], j), {})
            if info.get("executed", 0) > 0:
                continue
            if task.get("id") not in by_id:
                continue
            fallback += 1
            for cap in registry._caps.values():
                if cap.category != task.get("category"):
                    continue
                try:
                    trial = cap.execute(check["input"])
                except Exception:
                    continue
                if execaps.compare(check["expected"], trial,
                                   check["compare"]):
                    false_negative += 1
                    break
    total = len(exec_tasks)
    return {
        "executed_tasks": total,
        "retrieval_precision": (exec_passed / total) if total else None,
        "correct_reuse_rate": (exec_passed / total) if total else None,
        "harmful_reuse_rate": ((total - exec_passed) / total)
        if total else None,
        "false_positive_tasks": total - exec_passed,
        "false_negative_checks": false_negative,
        "fallback_checks": fallback,
        "false_negative_rate": (false_negative / fallback)
        if fallback else None,
    }


def assess_promotion(evidence, arm_success_rate, baseline_success_rate,
                     min_reuse_tasks=2):
    """Promotion decisions from trusted evidence (directive §9).

    Per artifact: any negative evidence -> quarantined; at least
    ``min_reuse_tasks`` DISTINCT positive task ids plus arm success
    at or above baseline -> stable (SKILL); otherwise provisional
    (PATCH). Single-task success never promotes on its own, and all
    inputs are trusted task ids plus scored pass rates -- never
    model claims.
    """
    decisions = {}
    at_baseline = arm_success_rate >= baseline_success_rate
    for artifact, env in evidence.items():
        positive = env.get("positive", [])
        negative = env.get("negative", [])
        if negative:
            decisions[artifact] = {
                "status": "quarantined",
                "reasons": ["negative evidence on %s"
                            % ",".join(sorted(negative))],
            }
        elif len(set(positive)) >= min_reuse_tasks and at_baseline:
            decisions[artifact] = {
                "status": "stable",
                "reasons": ["%d independent reuse tasks"
                            % len(set(positive)),
                            "arm success %.3f >= baseline %.3f"
                            % (arm_success_rate, baseline_success_rate)],
            }
        else:
            reasons = []
            if len(set(positive)) < min_reuse_tasks:
                reasons.append("only %d reuse tasks (need %d)"
                               % (len(set(positive)), min_reuse_tasks))
            if not at_baseline:
                reasons.append("arm success %.3f < baseline %.3f"
                               % (arm_success_rate, baseline_success_rate))
            decisions[artifact] = {"status": "provisional",
                                   "reasons": reasons}
    return decisions


def thesis_verdict(payloads, reference="a", candidate="d",
                   max_harmful_reuse_rate=0.2):
    """Thesis verdict from arm payloads (directive §6 + §15).

    The thesis is SUPPORTED only if the candidate arm holds success
    at/above the reference while cutting calls/task AND tokens/task.
    Adds the §15 failure checks verbatim: flat/rising inference,
    falling success, one-for-one library growth, material negative
    transfer. Thresholds are fixed here, before results are read.
    """
    ref = payloads[reference]["aggregate"]
    cand = payloads[candidate]["aggregate"]
    checks = {}
    checks["success_held"] = bool(
        cand["success_rate"] >= ref["success_rate"])
    checks["calls_fell"] = bool(
        cand["calls_per_task"] < ref["calls_per_task"])
    checks["tokens_fell"] = bool(
        cand["tokens_per_task"] < ref["tokens_per_task"])
    growth = payloads[candidate].get("compression", {})
    ratio = growth.get("tasks_per_capability")
    checks["growth_sublinear"] = bool(not ratio or ratio >= 1.5)
    app = payloads[candidate].get("applicability", {})
    harmful = app.get("harmful_reuse_rate")
    checks["transfer_clean"] = bool(
        harmful is None or harmful <= max_harmful_reuse_rate)
    supported = bool(checks["success_held"] and checks["calls_fell"]
                     and checks["tokens_fell"])
    failed_15 = bool(
        not checks["calls_fell"] or not checks["tokens_fell"]
        or not checks["success_held"]
        or not checks["growth_sublinear"]
        or not checks["transfer_clean"])
    return {"reference": reference, "candidate": candidate,
            "checks": checks, "supported": supported,
            "failed_section_15": failed_15}


def run_arm(arm, tasks, make_inner, out_root, tasks_dir=None,
            capabilities_path=None):
    """Run one arm; write per-task files; return the summary dict.

    ``capabilities_path`` selects a learned registry (distill.py
    output) instead of the hand-written seeds -- the goal-4 rung:
    zero-LLM reuse from actually-synthesized capabilities.
    """
    out_dir = Path(out_root) / arm
    out_dir.mkdir(parents=True, exist_ok=True)
    inner = make_inner()
    if capabilities_path:
        learned, _meta = distill.load_learned(capabilities_path)
        registry = execaps.ExecRegistry(capabilities=learned)
        registry_source = str(capabilities_path)
    else:
        registry = execaps.ExecRegistry()
        registry_source = "seeds"
    if arm == "a":
        adapter = inner
    elif arm == "b":
        adapter = InjectionAdapter(inner, seed_b_memory(tasks_dir))
    elif arm == "c":
        adapter = ExecAdapter(inner, registry, allow_compose=False,
                              adapt_memory=seed_b_memory(tasks_dir))
    elif arm == "d":
        adapter = ExecAdapter(inner, registry, allow_compose=True,
                              adapt_memory=seed_b_memory(tasks_dir))
    else:
        raise ValueError("unknown arm: %r" % (arm,))
    records = []
    for task in tasks:
        # Per-task runs: identical scoring, plus wall time per task
        # (time-to-verified-result, solve + trusted verify).
        started = time.perf_counter()
        summary = runner.run([task], adapter, strip_fences=True)
        elapsed = time.perf_counter() - started
        task_summary = summary["tasks"][0]
        record = rollup(task, task_summary, adapter)
        record["seconds"] = round(elapsed, 3)
        records.append(record)
        with open(out_dir / (task["id"] + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"arm": arm, "record": record,
                       "summary": task_summary},
                      fh, indent=2)
    # Every adapter in this driver exposes history (native or
    # delegated); it accumulates exactly the model calls made.
    usage = runner.aggregate_usage(
        list(getattr(adapter, "history", None) or []))
    if arm in ("c", "d"):
        record_evidence(registry, tasks, records)
    agg = outcomes.aggregate(records)
    solved = sum(1 for r in records if r["passed"])
    payload = {"arm": arm,
               "tasks_passed": "%d/%d" % (solved, len(records)),
               "registry": {"source": registry_source,
                            "capabilities": len(registry._caps)},
               "aggregate": agg,
               "curves": outcomes.cumulative_curves(records),
               "compression": outcomes.compression(
                   solved, registry.capability_count()
                   if arm in ("c", "d") else 0),
               "capability_count": (registry.capability_count()
                                    if arm in ("c", "d") else 0),
               "evidence": evidence_summary(registry)
               if arm in ("c", "d") else {},
               "applicability": applicability_metrics(
                   tasks, records, registry, adapter)
               if arm in ("c", "d") else {},
               "records": records,
               "usage": usage}
    with open(Path(out_root) / ("%s-summary.json" % arm), "w",
              encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="Canonical A/B/C/D")
    parser.add_argument("--tasks", default=str(_HERE / "family-r"))
    parser.add_argument("--adapter", choices=("stub", "cerebras"),
                        default="stub")
    parser.add_argument("--recorded", default=None,
                        help="stub recorded file "
                             "(default: <tasks>/recorded/stub_all_pass.json)")
    parser.add_argument("--arms", default="abcd")
    parser.add_argument("--split", default="all",
                        help="run one split (e.g. transfer) or all tasks")
    parser.add_argument("--artifacts", default=str(_ROOT / "artifacts"
                                                   / "abcd"))
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--orders", type=int, default=1,
                        help="randomized task orders to run (directive §5)")
    parser.add_argument("--seed", type=int, default=0,
                        help="base seed for order shuffles")
    parser.add_argument("--capabilities", default=None,
                        help="learned registry JSON (distill.py output) "
                             "instead of hand-written seeds")
    args = parser.parse_args(argv)
    if args.orders < 1:
        raise SystemExit("--orders must be >= 1")

    arms = [a for a in args.arms.lower() if a in ARMS]
    if not arms:
        raise SystemExit("no valid arms in %r" % (args.arms,))
    tasks = sorted(runner.load_tasks(Path(args.tasks)),
                   key=lambda t: t["id"])
    problems = runner.validate_tasks(tasks)
    if problems:
        raise SystemExit("invalid tasks:\n" + "\n".join(problems))
    if args.split != "all":
        tasks = [t for t in tasks if t.get("split") == args.split]
        if not tasks:
            raise SystemExit("no tasks for split %r" % (args.split,))

    recorded = (Path(args.recorded) if args.recorded
                else Path(args.tasks) / "recorded" / "stub_all_pass.json")

    def make_inner():
        if args.adapter == "stub":
            return CountingStub(recorded)
        return runner.CerebrasAdapter(max_tokens=args.max_tokens)

    out_root = Path(args.artifacts)
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    order_payloads = []
    for order in range(args.orders):
        ordered = list(tasks)
        if args.orders > 1:
            random.Random(args.seed + order).shuffle(ordered)
        order_root = (out_root / ("order-%d" % order)
                      if args.orders > 1 else out_root)
        order_ids = [t["id"] for t in ordered]
        payloads = {}
        for arm in arms:
            payload = run_arm(arm, ordered, make_inner, order_root,
                              tasks_dir=Path(args.tasks),
                              capabilities_path=args.capabilities)
            payload["order"] = order_ids
            with open(order_root / ("%s-summary.json" % arm), "w",
                      encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            payloads[arm] = payload
            agg = payload["aggregate"]
            print("order %d arm %s: %s passed, outcomes=%s, "
                  "calls/task=%.3f, tokens/task=%.1f" % (
                      order, arm.upper(), payload["tasks_passed"],
                      agg["outcomes"], agg["calls_per_task"],
                      agg["tokens_per_task"]), flush=True)
        order_payloads.append(payloads)
    comparison = {"orders": args.orders, "seed": args.seed,
                  "by_order": [
                      {"arms": {arm: _slim(payload)
                               for arm, payload in payloads.items()}}
                      for payloads in order_payloads]}
    if "a" in arms:
        baseline = sum(p["a"]["aggregate"]["success_rate"]
                       for p in order_payloads) / len(order_payloads)
        for payloads, order in zip(order_payloads,
                                   range(args.orders)):
            order_root = (out_root / ("order-%d" % order)
                          if args.orders > 1 else out_root)
            for arm in ("c", "d"):
                if arm not in payloads:
                    continue
                payload = payloads[arm]
                payload["promotion"] = assess_promotion(
                    payload.get("evidence", {}),
                    payload["aggregate"]["success_rate"], baseline)
                with open(order_root / ("%s-summary.json" % arm), "w",
                          encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
    if "a" in arms and "d" in arms:
        comparison["verdicts"] = [
            thesis_verdict(payloads) for payloads in order_payloads]
        mean_a = _mean_aggregate(
            [p["a"] for p in order_payloads])
        mean_d = _mean_aggregate(
            [p["d"] for p in order_payloads])
        comparison["verdict_mean"] = thesis_verdict(
            {"a": {"aggregate": mean_a},
             "d": {"aggregate": mean_d,
                   "compression": order_payloads[0]["d"].get(
                       "compression", {}),
                   "applicability": order_payloads[0]["d"].get(
                       "applicability", {})}})
        print("verdict (mean over %d orders): %s"
              % (args.orders, comparison["verdict_mean"]), flush=True)
    with open(out_root / "comparison.json", "w", encoding="utf-8") as fh:
        json.dump(comparison, fh, indent=2)
    return 0


def _slim(payload):
    """Comparison-sized arm summary (records live in arm files)."""
    return {"arm": payload["arm"],
            "tasks_passed": payload["tasks_passed"],
            "aggregate": payload["aggregate"],
            "capability_count": payload.get("capability_count", 0)}


def _mean_aggregate(payloads):
    """Mean aggregate over orders (verdict input, not a run)."""
    keys = ("success_rate", "calls_per_task", "tokens_per_task")
    mean = {}
    for key in keys:
        vals = [p["aggregate"][key] for p in payloads]
        mean[key] = sum(vals) / len(vals) if vals else 0.0
    return mean


if __name__ == "__main__":
    raise SystemExit(main())
