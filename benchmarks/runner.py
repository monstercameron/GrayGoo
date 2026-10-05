"""Offline benchmark runner for Family A (parsing / data transformation).

Loads machine-readable task files from benchmarks/family-a/, runs each
check against a swappable model adapter, and reports per-task pass/fail
plus summary counts (total and per-split).

Usage (offline, default):
    python benchmarks/runner.py --recorded benchmarks/family-a/recorded/stub_all_pass.json

Live pilot example (Baseline A: no memory, from scratch, tiny budget):
    python benchmarks/runner.py --adapter cerebras --only A-EXP-01
        --max-tokens 128 --json-out artifacts/baseline-a-pilot/A-EXP-01.json

Adapters:
    stub      Return pre-recorded outputs (default; fully offline).
    cerebras  Call the root cerebras_client (opt-in via --adapter cerebras;
              makes live API calls, so it is never the default).

A task passes iff ALL of its checks pass. Exit status is 0 when every
selected task passes, 1 otherwise.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

SPLITS = ("exposure", "transfer", "adversarial", "equivalent-transform")
COMPARE_MODES = ("exact", "json")
# Task families: A = parsing/data-transformation procedures,
# R = reuse-designed held-out tasks (same-procedure REUSE + COMPOSE).
FAMILIES = ("A", "R")

# Mirrors cerebras_client qwen-3.8-27b pricing (USD per million tokens).
# Kept local (not imported) so the offline path never touches the client.
COST_INPUT_USD_PER_MTOK = 0.99
COST_OUTPUT_USD_PER_MTOK = 1.49


def _num(value):
    """Numeric value or 0 (None/bool/non-numeric usage entries)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def aggregate_usage(entries: list) -> dict:
    """Sum per-call usage entries into totals.

    Each entry may carry ``input_tokens``, ``output_tokens``,
    ``latency_ms``, and ``cost_usd`` (missing/``None`` counts as zero,
    except ``cost_usd`` which falls back to local pricing from the
    entry's own token counts). Returns ``calls``, ``input_tokens``,
    ``output_tokens``, ``total_tokens``, ``latency_ms``, ``cost_usd``.
    """
    input_tokens = 0
    output_tokens = 0
    latency_ms = 0.0
    cost_usd = 0.0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        inp = _num(entry.get("input_tokens"))
        out = _num(entry.get("output_tokens"))
        input_tokens += inp
        output_tokens += out
        latency_ms += _num(entry.get("latency_ms"))
        cost = entry.get("cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            cost = (inp * COST_INPUT_USD_PER_MTOK + out * COST_OUTPUT_USD_PER_MTOK) / 1_000_000
        cost_usd += cost
    return {
        "calls": sum(1 for e in entries if isinstance(e, dict)),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "latency_ms": round(latency_ms, 1),
        "cost_usd": round(cost_usd, 6),
    }


class ModelAdapter:
    """Interface every model adapter implements."""

    name = "base"

    def solve(self, task: dict, check_index: int, check_input: str) -> str:
        """Return the model's output text for one check input."""
        raise NotImplementedError


class StubAdapter(ModelAdapter):
    """Offline adapter: replay recorded outputs keyed by task id.

    Recorded file format: {"TASK-ID": ["output for check 0", ...], ...}.
    Missing entries yield "" (which fails the check, honestly).
    """

    name = "stub"

    def __init__(self, recorded_path: Path):
        with open(recorded_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("recorded file must be a JSON object")
        self._recorded = data
        self.path = str(recorded_path)

    def solve(self, task: dict, check_index: int, check_input: str) -> str:
        outputs = self._recorded.get(task["id"], [])
        if check_index < len(outputs):
            return outputs[check_index]
        return ""


class CerebrasAdapter(ModelAdapter):
    """Live adapter over the root cerebras_client (opt-in only).

    Imported lazily so the default offline path never touches it; the
    root module may be edited concurrently by another lane, so attribute
    access is defensive and failures raise a clear error.
    """

    name = "cerebras"

    def __init__(self, max_tokens: int = 256):
        self.max_tokens = max_tokens
        # One usage/accounting entry per solve() call, in call order.
        # The runner snapshots this around each check; entries carry
        # input_tokens/output_tokens/latency_ms/cost_usd/request_id plus
        # model/finish_reason when the client reports them.
        self.history: list = []
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import cerebras_client  # noqa: PLC0415
        except Exception as exc:
            raise RuntimeError(f"cerebras adapter unavailable: {exc}") from exc
        generate = getattr(cerebras_client, "generate", None)
        if not callable(generate):
            raise RuntimeError(
                "cerebras adapter unavailable: cerebras_client.generate not found"
            )
        self._generate = generate

    def solve(self, task: dict, check_index: int, check_input: str) -> str:
        result = self._generate(
            check_input,
            system=task["prompt"],
            max_tokens=self.max_tokens,
            temperature=0.0,
            reasoning_effort="none",
        )
        if isinstance(result, dict):
            self.history.append(
                {
                    "input_tokens": result.get("input_tokens"),
                    "output_tokens": result.get("output_tokens"),
                    "latency_ms": result.get("latency_ms"),
                    "cost_usd": result.get("cost_usd"),
                    "request_id": result.get("request_id"),
                    "model": result.get("model"),
                    "finish_reason": result.get("finish_reason"),
                }
            )
            return result.get("text", "") or ""
        self.history.append(
            {
                "input_tokens": None,
                "output_tokens": None,
                "latency_ms": None,
                "cost_usd": None,
                "request_id": None,
                "model": None,
                "finish_reason": None,
            }
        )
        return str(result)


def strip_code_fences(text: str) -> tuple:
    """Remove exactly one pair of ```json...``` fences from model output.

    Rule: strip surrounding whitespace; if the remainder starts with an
    opening fence line (``` plus an optional ``json`` tag, case-insensitive)
    and ends with a closing ``` fence, remove the opening line and the final
    fence and strip again. Only one layer is ever removed (no recursion), and
    single-line outputs are left untouched. Returns (text, applied).
    """
    s = text.strip()
    if not s.startswith("```"):
        return text, False
    nl = s.find("\n")
    if nl == -1:
        return text, False
    tag = s[3:nl].strip().lower()
    if tag not in ("", "json"):
        return text, False
    rest = s[nl + 1:].rstrip()
    if not rest.endswith("```"):
        return text, False
    return rest[:-3].strip(), True


def compare(expected: str, actual: str, mode: str) -> bool:
    if mode == "exact":
        if not isinstance(actual, str) or not isinstance(expected, str):
            return False
        return actual.strip("\n") == expected.strip("\n")
    if mode == "json":
        try:
            return json.loads(actual) == json.loads(expected)
        except (json.JSONDecodeError, TypeError):
            return False
    raise ValueError(f"unknown compare mode: {mode!r}")


def load_tasks(tasks_dir: Path) -> list:
    """Load every *.json task list directly under tasks_dir (not subdirs)."""
    tasks = []
    for path in sorted(tasks_dir.glob("*.json")):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError(f"{path.name}: top level must be a JSON array")
        for task in data:
            task["_source"] = path.name
        tasks.extend(data)
    return tasks


def validate_tasks(tasks: list) -> list:
    """Return a list of schema problems (empty means valid)."""
    problems = []
    seen = set()
    for i, task in enumerate(tasks):
        where = f"task[{i}]"
        for key in ("id", "family", "split", "prompt", "checks", "notes"):
            if key not in task:
                problems.append(f"{where}: missing key {key!r}")
        task_id = task.get("id", f"#{i}")
        if task_id in seen:
            problems.append(f"{task_id}: duplicate id")
        seen.add(task_id)
        if task.get("family") not in FAMILIES:
            problems.append(f"{task_id}: family must be one of {FAMILIES}")
        if task.get("split") not in SPLITS:
            problems.append(f"{task_id}: bad split {task.get('split')!r}")
        checks = task.get("checks", None)
        if not isinstance(checks, list) or not checks:
            problems.append(f"{task_id}: 'checks' must be a non-empty list")
            continue
        for j, check in enumerate(checks):
            for key in ("input", "expected", "compare"):
                if key not in check:
                    problems.append(f"{task_id} check {j}: missing key {key!r}")
            for key in ("input", "expected"):
                if key in check and not isinstance(check[key], str):
                    problems.append(
                        f"{task_id} check {j}: {key!r} must be a string"
                    )
            if check.get("compare") not in COMPARE_MODES:
                problems.append(
                    f"{task_id} check {j}: bad compare {check.get('compare')!r}"
                )
    return problems


def run(
    tasks: list,
    adapter: ModelAdapter,
    verbose: bool = False,
    strip_fences: bool = False,
) -> dict:
    results = []
    for task in tasks:
        failures = []
        usage = []
        stripped_flags = []
        for j, check in enumerate(task["checks"]):
            history = getattr(adapter, "history", None)
            before = len(history) if isinstance(history, list) else None
            try:
                actual = adapter.solve(task, j, check["input"])
            except Exception as exc:  # adapter errors fail the check, loudly
                actual = f"<adapter error: {exc}>"
            if actual is None:
                actual = ""
            if before is not None and isinstance(history, list):
                usage.extend(history[before:])
            compared = actual
            stripped = False
            if strip_fences and isinstance(actual, str):
                compared, stripped = strip_code_fences(actual)
            stripped_flags.append(stripped)
            ok = compare(check["expected"], compared, check["compare"])
            if not ok:
                failures.append((j, check["expected"], actual))
            if verbose:
                status = "ok  " if ok else "FAIL"
                print(f"    [{status}] check {j} ({check['compare']})")
        passed = not failures
        results.append(
            {
                "task": task,
                "passed": passed,
                "failures": failures,
                "usage": usage,
                "stripped": stripped_flags,
            }
        )
        print(f"[{'PASS' if passed else 'FAIL'}] {task['id']} ({task['split']})")
        for j, expected, actual in failures:
            print(f"      check {j}: expected {expected!r} got {actual!r}")
    return summarize(results)


def summarize(results: list) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    by_split = Counter()
    by_split_pass = Counter()
    for r in results:
        split = r["task"]["split"]
        by_split[split] += 1
        if r["passed"]:
            by_split_pass[split] += 1
    usage_entries = [u for r in results for u in r.get("usage", [])]
    usage = aggregate_usage(usage_entries)
    print(f"\nsummary: {passed}/{total} tasks passed")
    for split in SPLITS:
        if by_split[split]:
            print(f"  {split}: {by_split_pass[split]}/{by_split[split]}")
    if usage["calls"]:
        print(
            f"  usage: {usage['calls']} calls, "
            f"{usage['input_tokens']}/{usage['output_tokens']} in/out tokens, "
            f"{usage['latency_ms']} ms, ${usage['cost_usd']:.6f}"
        )
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "by_split": {
            s: {"passed": by_split_pass[s], "total": by_split[s]}
            for s in SPLITS
            if by_split[s]
        },
        "usage": usage,
        "tasks": [
            {
                "id": r["task"]["id"],
                "split": r["task"].get("split"),
                "category": r["task"].get("category"),
                "passed": r["passed"],
                "failures": [
                    {"check": j, "expected": exp, "actual": act}
                    for j, exp, act in r["failures"]
                ],
                "checks": [
                    {
                        "check": j,
                        "compare": r["task"]["checks"][j].get("compare"),
                        "stripped": j < len(r.get("stripped", []))
                        and bool(r["stripped"][j]),
                        "passed": all(j != f[0] for f in r["failures"]),
                    }
                    for j in range(len(r["task"]["checks"]))
                ],
                "usage": r.get("usage", []),
                "totals": aggregate_usage(r.get("usage", [])),
            }
            for r in results
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Family A offline benchmark runner")
    parser.add_argument(
        "--tasks",
        default=str(Path(__file__).resolve().parent / "family-a"),
        help="directory of task JSON files",
    )
    parser.add_argument("--adapter", choices=("stub", "cerebras"), default="stub")
    parser.add_argument(
        "--recorded",
        default=None,
        help="recorded-output JSON for the stub adapter (required with --adapter stub)",
    )
    parser.add_argument("--split", choices=SPLITS, default=None, help="run one split only")
    parser.add_argument("--only", default=None, help="run one task id only")
    parser.add_argument("--list", action="store_true", help="list tasks and exit")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=256,
        help="max tokens per call for --adapter cerebras (default 256)",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="write raw per-task results JSON to FILE (use artifacts/...)",
    )
    parser.add_argument(
        "--strip-fences",
        action="store_true",
        help="strip one pair of ```json...``` fences before compare",
    )
    args = parser.parse_args(argv)

    tasks = load_tasks(Path(args.tasks))
    problems = validate_tasks(tasks)
    if problems:
        print("task schema problems:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 2
    if args.split:
        tasks = [t for t in tasks if t["split"] == args.split]
    if args.only:
        tasks = [t for t in tasks if t["id"] == args.only]
    if args.list:
        for task in tasks:
            print(f"{task['id']}  {task['split']}  {task.get('category', '?')}")
        print(f"{len(tasks)} tasks")
        return 0
    if not tasks:
        print("no tasks selected (check --split/--only filters)", file=sys.stderr)
        return 2
    if args.adapter == "stub":
        if not args.recorded:
            print("--recorded FILE is required with --adapter stub", file=sys.stderr)
            return 2
        adapter = StubAdapter(Path(args.recorded))
    else:
        adapter = CerebrasAdapter(max_tokens=args.max_tokens)
    print(f"adapter: {adapter.name}  tasks: {len(tasks)}")
    summary = run(tasks, adapter, verbose=args.verbose, strip_fences=args.strip_fences)
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "adapter": adapter.name,
            "max_tokens": getattr(adapter, "max_tokens", None),
            "strip_fences": args.strip_fences,
            "summary": summary,
        }
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"wrote {out_path}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
