"""Learning-measurement harness (plan.md sections 64-67).

Loaders recompute every metric from raw per-task JSON artifacts plus
reuse/transfer ledgers -- never from doc tables. Every metric returns
an evidence envelope::

    {"value": ..., "n": int, "basis": str}

``value`` is None when the data cannot support the metric (see
:func:`tbd`). Stdlib only.
"""

import glob
import json
import math
import os
import statistics

ARTIFACTS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "artifacts")
BASELINE_A = os.path.join(ARTIFACTS, "baseline-a")
BASELINES_BCD = os.path.join(ARTIFACTS, "baselines-bcd")

HELD_OUT_SPLIT = "transfer"


def metric(value, n, basis):
    """Wrap a computed metric with its evidence basis."""
    return {"value": value, "n": n, "basis": basis}


def tbd(reason):
    """Envelope for a metric the current evidence cannot support."""
    return {"value": None, "n": 0, "basis": "TBD: %s" % reason}


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_task_file(path):
    """Normalize one raw per-task JSON into a flat task record."""
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    summary = raw.get("summary", {})
    tasks = summary.get("tasks", [])
    task = tasks[0] if tasks else {}
    totals = task.get("totals", {}) or summary.get("usage", {}) or {}
    checks = task.get("checks", [])
    memory = raw.get("memory", {}) or {}
    retrieval = memory.get("retrieval", {}) or {}
    record = {
        "id": task.get("id") or os.path.basename(path)[:-len(".json")],
        "source": path,
        "split": task.get("split"),
        "category": task.get("category"),
        "passed": bool(task.get("passed", False)),
        "baseline": raw.get("baseline"),
        "phase": raw.get("phase"),
        "calls": totals.get("calls", 0),
        "input_tokens": totals.get("input_tokens", 0),
        "output_tokens": totals.get("output_tokens", 0),
        "total_tokens": totals.get("total_tokens", 0),
        "latency_ms": totals.get("latency_ms", 0.0),
        "cost_usd": totals.get("cost_usd", 0.0),
        "checks": len(checks),
        "fenced_checks": sum(1 for c in checks if c.get("stripped")),
        "text_hit": bool((retrieval.get("text") or {}).get("hit", False)),
        "text_sources": ["%s:%s" % (r.get("task_id"), r.get("check"))
                         for r in (retrieval.get("text") or {}).get(
                             "retrieved", [])],
        "patch_reuse": bool((retrieval.get("patches") or {}).get(
            "reuse", False)),
        "patch_sources": [r.get("patch_id")
                          for r in (retrieval.get("patches") or {}).get(
                              "retrieved", [])],
        "memory_kind": memory.get("kind"),
        "stored": memory.get("stored", 0),
    }
    return record


def load_task_dir(path):
    """Load every ``A-*.json`` task file in a directory, sorted by id."""
    records = [load_task_file(found)
               for found in sorted(glob.glob(os.path.join(path, "A-*.json")))]
    return records


def load_baseline_a(root=BASELINE_A):
    """Load Baseline A raw + stripped task records from raw files."""
    return {"raw": load_task_dir(os.path.join(root, "raw")),
            "stripped": load_task_dir(os.path.join(root, "stripped"))}


def load_baselines_bcd(root=BASELINES_BCD):
    """Load Baseline B/C/D task records from raw files."""
    return {name: load_task_dir(os.path.join(root, name))
            for name in ("b", "c", "d")}


def load_reuse_ledger(path):
    """Load a ``reuse.jsonl`` ledger (one outcome object per line)."""
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_transfer_file(path):
    """Load a transfer-tracker JSON (``{patch_id: [outcomes]}``)."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    outcomes = []
    if isinstance(data, dict):
        for rows in data.values():
            outcomes.extend(rows)
    elif isinstance(data, list):
        outcomes = data
    return outcomes


def load_text_memory(path):
    """Load a text-memory JSON (``{"entries": [...]}``)."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle).get("entries", [])


def _helped(row):
    for key in ("helped", "success", "succeeded"):
        if key in row:
            return bool(row[key])
    return False


# ---------------------------------------------------------------------------
# Task / held-out success
# ---------------------------------------------------------------------------

def success_rate(tasks, split=None):
    """Fraction of tasks passed, optionally restricted to one split."""
    selected = [t for t in tasks
                if split is None or t.get("split") == split]
    if not selected:
        return tbd("no tasks for split %r" % (split,))
    passed = sum(1 for t in selected if t.get("passed"))
    label = "all splits" if split is None else "split %r" % split
    return metric(passed / len(selected), len(selected),
                  "%d/%d passed (%s) across %d task files"
                  % (passed, len(selected), label, len(selected)))


def held_out_success(tasks):
    """Success rate on the held-out (transfer) split."""
    return success_rate(tasks, split=HELD_OUT_SPLIT)


def held_out_series(conditions):
    """Per-condition held-out success fractions from raw task records.

    ``conditions`` maps label -> task list. Returns one envelope whose
    value maps label -> {"passed", "total", "rate"}.
    """
    value = {}
    total = 0
    for label, tasks in conditions.items():
        selected = [t for t in tasks if t.get("split") == HELD_OUT_SPLIT]
        passed = sum(1 for t in selected if t.get("passed"))
        total += len(selected)
        value[label] = {"passed": passed, "total": len(selected),
                        "rate": (passed / len(selected)) if selected
                        else None}
    if total == 0:
        return tbd("no held-out tasks in any condition")
    return metric(value, total,
                  "held-out (transfer-split) outcomes recomputed from "
                  "%d task files across %d conditions"
                  % (total, len(conditions)))


# ---------------------------------------------------------------------------
# Reuse / transfer
# ---------------------------------------------------------------------------

def _phase2(tasks):
    """Phase-2 (memory-assisted) tasks: any non-exposure phase/split."""
    phased = [t for t in tasks if t.get("phase") is not None]
    if phased:
        return [t for t in phased if t.get("phase") != "exposure"]
    return [t for t in tasks if t.get("split") != "exposure"]


def reuse_rate(tasks):
    """Phase-2 tasks with any memory retrieval / total Phase-2 tasks."""
    selected = _phase2(tasks)
    if not selected:
        return tbd("no Phase-2 tasks found")
    reused = sum(1 for t in selected
                 if t.get("text_hit") or t.get("patch_reuse"))
    return metric(reused / len(selected), len(selected),
                  "%d/%d Phase-2 tasks retrieved memory (text hit or "
                  "patch reuse) from per-task retrieval records"
                  % (reused, len(selected)))


def text_hit_rate(tasks):
    """Phase-2 tasks with a text-memory hit / total Phase-2 tasks."""
    selected = _phase2(tasks)
    if not selected:
        return tbd("no Phase-2 tasks found")
    hits = sum(1 for t in selected if t.get("text_hit"))
    return metric(hits / len(selected), len(selected),
                  "%d/%d Phase-2 tasks with keyword-overlap text hit"
                  % (hits, len(selected)))


def patch_reuse_rate(tasks):
    """Phase-2 tasks reusing >=1 patch / total Phase-2 tasks."""
    selected = _phase2(tasks)
    if not selected:
        return tbd("no Phase-2 tasks found")
    reused = sum(1 for t in selected if t.get("patch_reuse"))
    return metric(reused / len(selected), len(selected),
                  "%d/%d Phase-2 tasks reused >=1 patch"
                  % (reused, len(selected)))


def reuse_helped_rate(tasks):
    """Reusing Phase-2 tasks that passed / reusing Phase-2 tasks."""
    reusing = [t for t in _phase2(tasks)
               if t.get("text_hit") or t.get("patch_reuse")]
    if not reusing:
        return tbd("no reusing Phase-2 tasks found")
    helped = sum(1 for t in reusing if t.get("passed"))
    return metric(helped / len(reusing), len(reusing),
                  "%d/%d reusing Phase-2 tasks passed"
                  % (helped, len(reusing)))


def transfer_success(rows):
    """Held-out reuse rows with helped=true / held-out reuse rows."""
    selected = [r for r in rows if r.get("held_out")]
    if not selected:
        return tbd("no held-out rows in reuse/transfer ledger")
    helped = sum(1 for r in selected if _helped(r))
    return metric(helped / len(selected), len(selected),
                  "%d/%d held-out ledger rows helped"
                  % (helped, len(selected)))


def negative_transfer_outcome(rows):
    """Outcome-level negative transfer from a reuse/transfer ledger."""
    if not rows:
        return tbd("reuse/transfer ledger is empty or missing")
    bad = sum(1 for r in rows if not _helped(r))
    severe = sum(1 for r in rows
                 if not _helped(r) and r.get("severe", False))
    return metric({"rate": bad / len(rows), "bad": bad,
                   "severe": severe, "total": len(rows)}, len(rows),
                  "%d/%d ledger rows with helped=false (%d severe); "
                  "includes tasks the no-memory baseline also failed"
                  % (bad, len(rows), severe))


def negative_transfer_a_relative(base_tasks, exp_tasks, task_ids=None):
    """A-relative transfer: regressions/improvements vs a baseline.

    A regression is a task the baseline passed that the experiment
    fails; an improvement is the reverse. Defaults to the experiment's
    Phase-2 task ids (the memory-treated set).
    """
    if task_ids is None:
        task_ids = [t.get("id") for t in _phase2(exp_tasks)]
    base = {t.get("id"): bool(t.get("passed")) for t in base_tasks}
    exp = {t.get("id"): bool(t.get("passed")) for t in exp_tasks}
    comparable = [i for i in task_ids if i in base and i in exp]
    if not comparable:
        return tbd("no comparable task ids between baseline and "
                   "experiment")
    regressed = [i for i in comparable if base[i] and not exp[i]]
    improved = [i for i in comparable if exp[i] and not base[i]]
    return metric({"regressions": len(regressed),
                   "improvements": len(improved),
                   "regressed_ids": sorted(regressed),
                   "improved_ids": sorted(improved)},
                  len(comparable),
                  "%d regressions / %d improvements over %d comparable "
                  "tasks (baseline-passed-now-fail = causal signal)"
                  % (len(regressed), len(improved), len(comparable)))


# ---------------------------------------------------------------------------
# Resources + trends
# ---------------------------------------------------------------------------

def _mean(values):
    return sum(values) / len(values) if values else 0.0


def resources_per_task(tasks):
    """Per-task mean calls/tokens/latency/cost recomputed from raw."""
    if not tasks:
        return tbd("no task records")
    value = {
        "calls": _mean([t.get("calls", 0) for t in tasks]),
        "input_tokens": _mean([t.get("input_tokens", 0) for t in tasks]),
        "output_tokens": _mean([t.get("output_tokens", 0) for t in tasks]),
        "total_tokens": _mean([t.get("total_tokens", 0) for t in tasks]),
        "latency_ms": _mean([t.get("latency_ms", 0.0) for t in tasks]),
        "cost_usd": _mean([t.get("cost_usd", 0.0) for t in tasks]),
    }
    return metric(value, len(tasks),
                  "means over %d raw per-task totals" % len(tasks))


def trend(series, labels=None, tolerance=0.05):
    """Classify an ordered series as increasing/decreasing/flat.

    Least-squares slope, normalized by series length and mean so the
    ``tolerance`` is scale-free. Fewer than 2 points is UNMEASURABLE.
    """
    points = list(series)
    if len(points) < 2:
        return tbd("trend needs >=2 timepoints, have %d" % len(points))
    if any(v is None for v in points):
        return tbd("trend series contains missing values")
    count = len(points)
    mean_x = (count - 1) / 2.0
    mean_y = _mean(points)
    denom = sum((x - mean_x) ** 2 for x in range(count))
    slope = (sum((x - mean_x) * (y - mean_y)
                 for x, y in enumerate(points)) / denom) if denom else 0.0
    scale = abs(mean_y) if mean_y != 0 else 1.0
    relative = slope * (count - 1) / scale
    if abs(relative) < tolerance:
        direction = "flat"
    else:
        direction = "increasing" if slope > 0 else "decreasing"
    value = {"direction": direction, "slope": slope,
             "relative_change": relative,
             "first": points[0], "last": points[-1]}
    basis = ("least-squares slope %.4g over %d points"
             % (slope, count))
    if labels:
        basis += " (%s)" % " -> ".join(labels)
    return metric(value, count, basis)


#: Time-to-verified-mutation stage vocabulary (issues.md #34).
#: Samples carry optional ``"stages": {stage: ms}`` dicts; unknown
#: stage names are ignored so dashboards keep a fixed schema.
TTVM_STAGES = ("context", "model", "worker", "compile", "test",
               "eval", "promotion")


def ttvm_breakdown(samples):
    """Per-stage TTVM stats over samples carrying ``"stages"`` dicts.

    Returns an envelope whose value maps every :data:`TTVM_STAGES`
    entry to ``{"mean_ms", "median_ms", "min_ms", "max_ms", "n",
    "share"}`` (stats are None when a stage never appears; ``share``
    is the stage mean over the sum of stage means, None when the sum
    is 0). Non-numeric or negative stage values are ignored. Samples
    without a ``"stages"`` dict contribute nothing; when none
    contribute, the metric is UNMEASURABLE (tbd), not zero.
    """
    buckets = {stage: [] for stage in TTVM_STAGES}
    contributing = 0
    for sample in samples:
        stages = sample.get("stages") if isinstance(sample, dict) else None
        if not isinstance(stages, dict):
            continue
        seen = False
        for stage in TTVM_STAGES:
            value = stages.get(stage)
            if isinstance(value, bool) \
                    or not isinstance(value, (int, float)):
                continue
            if value < 0:
                continue
            buckets[stage].append(float(value))
            seen = True
        if seen:
            contributing += 1
    if contributing == 0:
        return tbd("no samples carry per-stage timing breakdowns")
    means = {stage: (statistics.fmean(vals) if vals else None)
             for stage, vals in buckets.items()}
    total = sum(mean for mean in means.values() if mean is not None)
    value = {}
    for stage in TTVM_STAGES:
        vals = buckets[stage]
        value[stage] = {
            "mean_ms": means[stage],
            "median_ms": statistics.median(vals) if vals else None,
            "min_ms": min(vals) if vals else None,
            "max_ms": max(vals) if vals else None,
            "n": len(vals),
            "share": (means[stage] / total
                      if vals and total else None),
        }
    return metric(value, contributing,
                  "per-stage stats over %d samples with breakdowns "
                  "(%s)" % (contributing, ", ".join(TTVM_STAGES)))


def time_to_verified_mutation(samples):
    """Stats over verified-mutation durations (plan.md section 50).

    ``samples`` are dicts with at least ``total_ms``; samples may also
    carry ``"stages": {stage: ms}`` breakdowns (see
    :func:`ttvm_breakdown`). Empty input is UNMEASURABLE, not zero.
    The value gains a ``"breakdown"`` key: per-stage stats, or None
    when no sample carries stage data (additive: existing keys
    unchanged).
    """
    durations = [s.get("total_ms") for s in samples
                 if s.get("total_ms") is not None]
    if not durations:
        return tbd("no verified-mutation timing samples recorded; "
                   "per-task inference latency is not a substitute")
    breakdown = ttvm_breakdown(samples)
    value = {"mean_ms": statistics.fmean(durations),
             "median_ms": statistics.median(durations),
             "min_ms": min(durations),
             "max_ms": max(durations),
             "breakdown": breakdown["value"]}
    return metric(value, len(durations),
                  "stats over %d verified-mutation durations" % len(durations))


# ---------------------------------------------------------------------------
# Capability library: count / growth / entropy
# ---------------------------------------------------------------------------

def capability_growth(p1_count, final_count):
    """Library growth between the frozen Phase-1 view and final state."""
    if p1_count is None or final_count is None:
        return tbd("missing Phase-1 or final library snapshot")
    added = final_count - p1_count
    value = {"p1": p1_count, "final": final_count, "added": added,
             "growth_factor": (final_count / p1_count) if p1_count else None}
    return metric(value, final_count,
                  "frozen Phase-1 snapshot (%d) vs final library (%d)"
                  % (p1_count, final_count))


def library_growth_from_tasks(tasks):
    """Library growth from per-task ``stored`` counts (raw records).

    Phase-1 (exposure) stored counts are the frozen library; Phase-2
    stored counts are the post-freeze adds.
    """
    p1 = [t for t in tasks if t.get("phase") == "exposure"
          or (t.get("phase") is None and t.get("split") == "exposure")]
    p2 = _phase2(tasks)
    if not p1 and not p2:
        return tbd("no task records with stored counts")
    p1_count = sum(t.get("stored", 0) for t in p1)
    added = sum(t.get("stored", 0) for t in p2)
    return capability_growth(p1_count, p1_count + added)


def learning_efficiency(base_passed, base_total, run_passed, run_total,
                        base_tokens, run_tokens):
    """Held-out improvement per extra token vs a no-memory baseline.

    ``(run_rate - base_rate) / max(1, run_tokens - base_tokens)`` —
    how much held-out success each additional token buys. Positive
    means learning paid for its context; zero/negative means the
    memory cost tokens without improving (or while harming) held-out
    success. When the run uses FEWER tokens and still improves, the
    denominator clamps to 1 (pure win, value == rate delta). TBD
    when either population is empty (issues.md #105).
    """
    if not base_total or not run_total:
        return tbd("empty baseline or run population")
    improvement = run_passed / run_total - base_passed / base_total
    extra = run_tokens - base_tokens
    return metric(improvement / max(1, extra),
                  run_total + base_total,
                  "held-out rate delta %.4f over %d extra tokens "
                  "(%d/%d vs %d/%d)" % (
                      improvement, extra, run_passed, run_total,
                      base_passed, base_total))


def entropy_from_counts(counts):
    """Shannon entropy (nats) of a category-count distribution."""
    total = sum(counts)
    if total == 0:
        return tbd("empty count distribution")
    entropy = -sum((c / total) * math.log(c / total)
                   for c in counts if c > 0)
    return metric(entropy, total,
                  "Shannon entropy over %d categories (%d observations)"
                  % (len(counts), total))


def _entropy_of_keys(keys):
    from collections import Counter
    counts = Counter(keys)
    return entropy_from_counts(list(counts.values()))


def reuse_entropy_from_ledger(rows, key="patch_id"):
    """Entropy of the reuse distribution across capabilities in a ledger."""
    keys = [r.get(key) for r in rows if r.get(key) is not None]
    if not keys:
        return tbd("no %r keys in ledger rows" % key)
    result = _entropy_of_keys(keys)
    result["basis"] += "; key=%r from %d ledger rows" % (key, len(rows))
    return result


def text_reuse_entropy(tasks):
    """Entropy of text-memory reuse across source tasks.

    Keyed by source task id (both retrieved checks of one task count
    toward that task), so the unit is the reusable procedure -- the
    analogue of one patch id for executable memory.
    """
    keys = []
    for task in _phase2(tasks):
        keys.extend(source.split(":")[0]
                    for source in task.get("text_sources", []))
    if not keys:
        return tbd("no text-memory retrievals in Phase-2 records")
    result = _entropy_of_keys(keys)
    result["basis"] += ("; text source tasks from Phase-2 retrieval "
                        "records")
    return result


def patch_reuse_entropy_from_tasks(tasks):
    """Entropy of patch reuse across patch ids in per-task records."""
    keys = []
    for task in _phase2(tasks):
        keys.extend(task.get("patch_sources", []))
    if not keys:
        return tbd("no patch retrievals in Phase-2 retrieval records")
    result = _entropy_of_keys(keys)
    result["basis"] += "; patch ids from Phase-2 retrieval records"
    return result


# ---------------------------------------------------------------------------
# Full recomputation report
# ---------------------------------------------------------------------------

def full_report(root_a=BASELINE_A, root_bcd=BASELINES_BCD):
    """Recompute every learning metric from raw artifacts."""
    report = {}
    base_a = load_baseline_a(root_a)
    bcd = load_baselines_bcd(root_bcd)
    report["baseline_a"] = {
        condition: {
            "success": success_rate(tasks),
            "held_out": held_out_success(tasks),
            "resources": resources_per_task(tasks),
        } for condition, tasks in base_a.items()
    }
    for name, tasks in bcd.items():
        report["baseline_%s" % name] = {
            "success": success_rate(tasks),
            "held_out": held_out_success(tasks),
            "resources": resources_per_task(tasks),
            "reuse": reuse_rate(tasks),
            "text_hit": text_hit_rate(tasks),
            "patch_reuse": patch_reuse_rate(tasks),
            "reuse_helped": reuse_helped_rate(tasks),
            "a_relative": negative_transfer_a_relative(
                base_a["stripped"], tasks),
        }
    stripped_a = base_a["stripped"]
    conditions = {"A": stripped_a, "B": bcd["b"], "C": bcd["c"],
                  "D": bcd["d"]}
    report["held_out_series"] = held_out_series(conditions)
    tokens = [resources_per_task(tasks)["value"]["total_tokens"]
              for tasks in conditions.values()]
    calls = [resources_per_task(tasks)["value"]["calls"]
             for tasks in conditions.values()]
    latency = [resources_per_task(tasks)["value"]["latency_ms"]
               for tasks in conditions.values()]
    labels = list(conditions)
    report["tokens_trend"] = trend(tokens, labels=labels)
    report["calls_trend"] = trend(calls, labels=labels)
    report["latency_trend"] = trend(latency, labels=labels)
    for name in ("c", "d"):
        path = os.path.join(root_bcd, name, "memory",
                            "patches-%s" % name, "reuse.jsonl")
        rows = load_reuse_ledger(path) if os.path.exists(path) else []
        report["ledger_%s" % name] = {
            "transfer_success": transfer_success(rows),
            "negative_transfer": negative_transfer_outcome(rows),
            "reuse_entropy": reuse_entropy_from_ledger(rows),
        }
    report["text_entropy_b"] = text_reuse_entropy(bcd["b"])
    report["text_entropy_d"] = text_reuse_entropy(bcd["d"])
    report["patch_task_entropy_c"] = patch_reuse_entropy_from_tasks(bcd["c"])
    report["patch_task_entropy_d"] = patch_reuse_entropy_from_tasks(bcd["d"])
    for name in ("b", "d"):
        mem = os.path.join(root_bcd, name, "memory")
        p1_path = os.path.join(mem, "text_memory_p1.json")
        final_path = os.path.join(mem, "text_memory.json")
        p1 = len(load_text_memory(p1_path)) if os.path.exists(p1_path) \
            else None
        final = len(load_text_memory(final_path)) \
            if os.path.exists(final_path) else None
        report["text_growth_%s" % name] = capability_growth(p1, final)
    for name, tasks in bcd.items():
        report["growth_from_tasks_%s" % name] = \
            library_growth_from_tasks(tasks)
    for name in ("c", "d"):
        patch_dir = os.path.join(root_bcd, name, "memory",
                                 "patches-%s" % name, "patches")
        final = len(glob.glob(os.path.join(patch_dir, "*"))) \
            if os.path.isdir(patch_dir) else None
        report["capability_count_%s" % name] = (
            metric(final, final, "%d patch files in %s" % (final, patch_dir))
            if final is not None else tbd("patch dir missing: %s" % patch_dir)
        )
    report["time_to_verified_mutation"] = time_to_verified_mutation([])
    return report


def main():
    """Print the recomputed metrics report as JSON."""
    print(json.dumps(full_report(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
