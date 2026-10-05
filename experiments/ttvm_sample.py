"""Time-to-verified-mutation sampler (todos.md: Measure true learning).

Runs N end-to-end verified mutations through the REAL path stage by
stage — retrieve/context compile, live Cerebras generation (tiny),
s_expr parse, real SBCL rehearsal via pipeline.run_candidate — and
records per-stage wall times plus totals into artifacts/ttvm/.

Scope (honest): a sample counts as a verified mutation when the
candidate passes its rehearsal checks. The hidden-evaluator (eval)
and promotion stages are NOT run per sample — promotion scores the
fixed hidden corpus, which task-scoped samples do not target — so
those stages are absent (UNMEASURABLE, not zero) in the breakdown.
See documents/ttvm.md.

Usage:
    uv run python experiments/ttvm_sample.py          # N=10 live samples
    uv run python experiments/ttvm_sample.py --n 4    # fewer samples

Live-call budget: at most 2 attempts per sample (initial + 1 retry);
each call is tiny (<= 128 completion tokens, reasoning off).
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cerebras_client
import context as context_mod
import metrics
import pipeline
import retrieve
import risk
import s_expr
import workers
from demo import dumps_body, normalize_candidate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "artifacts", "ttvm")

GEN_MAX_TOKENS = 128
GEN_TEMPERATURES = (0.0, 0.7)  # escalate on retry: temp-0 retries repeat
GEN_REASONING = "none"
WORKER_TIMEOUT_S = 30.0
MAX_ATTEMPTS = 2

# (task_id, instruction, expected PRIN1 string). Trivial by design: the
# measured object is pipeline latency, not model reasoning power.
TASKS = [
    ("ttvm-01", "compute 6*7", "42"),
    ("ttvm-02", "first element of the list (10 20 30)", "10"),
    ("ttvm-03", "length of the list (a b c d)", "4"),
    ("ttvm-04", "reverse the list (1 2 3)", "(3 2 1)"),
    ("ttvm-05", "compute 100-37", "63"),
    ("ttvm-06", "second element of the list (5 6 7 8)", "6"),
    ("ttvm-07", "concatenate the lists (1 2) and (3 4)", "(1 2 3 4)"),
    ("ttvm-08", "compute 9*8", "72"),
    ("ttvm-09", "last element of the list (a b c)", "C"),
    ("ttvm-10", "length of the list (1 (2 3) 4)", "3"),
]

PROMPT = ("Return ONLY a Common Lisp expression that %s. "
          "No explanation, no code fences, just the expression. "
          "Example: for 'compute 1+1' return exactly: (+ 1 1)")


def _worker_elapsed_ms(evidence):
    total = 0.0
    if not isinstance(evidence, dict):
        return total
    for stage in evidence.values():
        if not isinstance(stage, dict):
            continue
        for item in stage.get("items", []) or []:
            if isinstance(item, dict):
                elapsed = item.get("elapsed_ms", 0.0) or 0.0
                total += float(elapsed)
    return total


DEBUG = False


def run_sample(task_id, instruction, expected):
    """Run one verified mutation; return (sample, attempts, verified)."""
    stages = {}
    attempts = 0
    verified = False
    candidate = None
    t0 = time.perf_counter()

    t_start = time.perf_counter()
    if not hasattr(run_sample, "_index"):
        run_sample._index = retrieve.CapabilityIndex()
    found = retrieve.find_capabilities(
        instruction, {"index": run_sample._index}, k=3)
    ctx = context_mod.compile_context(
        instruction, found, [], ["pure"],
        {"max_tokens": 500, "max_capabilities": 2})
    stages["context"] = (time.perf_counter() - t_start) * 1000.0

    while attempts < MAX_ATTEMPTS and not verified:
        attempts += 1
        t_start = time.perf_counter()
        out = cerebras_client.generate_candidate(
            PROMPT % instruction, max_tokens=GEN_MAX_TOKENS,
            temperature=GEN_TEMPERATURES[min(
                attempts - 1, len(GEN_TEMPERATURES) - 1)],
            reasoning_effort=GEN_REASONING)
        stages["model"] = stages.get("model", 0.0) + (
            time.perf_counter() - t_start) * 1000.0
        if DEBUG:
            print("  raw=%r" % ((out.get("raw") or "")[:200],))

        t_start = time.perf_counter()
        candidate, _norm = normalize_candidate(out.get("raw") or "")
        # The model sometimes returns a full (candidate ...) envelope
        # instead of a bare expression: unwrap its :definition forms
        # into executable code (bare expressions pass through).
        code = candidate
        try:
            envelope = s_expr.parse_candidate(candidate)
            code = dumps_body(envelope.get("definition") or [])
        except Exception:
            code = candidate
        cand_doc = ("(candidate (:target %s) (:parent 0) "
                    "(:definition %s))" % (task_id, code))
        try:
            parsed = s_expr.parse_candidate(cand_doc)
            parse_ok = isinstance(parsed, dict)
        except Exception:
            parsed, parse_ok = None, False
        stages["compile"] = stages.get("compile", 0.0) + (
            time.perf_counter() - t_start) * 1000.0
        if DEBUG:
            print("  code=%r parse_ok=%s" % (code[:200], parse_ok))
        if not parse_ok or not candidate:
            continue

        level = risk.classify(parsed, {"effects": ["pure"]}).get(
            "level", "R0")
        if DEBUG:
            print("  risk=%s" % (level,))
        if level != "R0":
            continue
        t_start = time.perf_counter()
        result = pipeline.run_candidate(
            cand_doc, tests={"direct": [{"code": code,
                                         "expect": expected}]},
            worker_fn=lambda code: workers.run_lisp(
                code, timeout_s=WORKER_TIMEOUT_S),
            risk_fn=lambda p: {"level": "R0"})
        rehearsal_wall = (time.perf_counter() - t_start) * 1000.0
        worker_ms = _worker_elapsed_ms(result.get("evidence"))
        stages["worker"] = stages.get("worker", 0.0) + worker_ms
        stages["test"] = stages.get("test", 0.0) + max(
            0.0, rehearsal_wall - worker_ms)
        verified = bool(result.get("ok"))
        if DEBUG:
            print("  ok=%s verdict=%r" % (
                verified, result.get("verdict")))

    sample = {"task_id": task_id, "verified": verified,
              "attempts": attempts,
              "total_ms": (time.perf_counter() - t0) * 1000.0,
              "stages": stages}
    return sample, verified


def main(argv):
    global DEBUG
    n = 10
    if "--n" in argv:
        n = int(argv[argv.index("--n") + 1])
    if "--debug" in argv:
        DEBUG = True
    tasks = TASKS[:max(1, n)]
    os.makedirs(OUT_DIR, exist_ok=True)

    samples = []
    for task_id, instruction, expected in tasks:
        sample, verified = run_sample(task_id, instruction, expected)
        samples.append(sample)
        print("sample %s verified=%s attempts=%d total_ms=%.0f" % (
            task_id, verified, sample["attempts"], sample["total_ms"]),
            flush=True)

    verified_only = [s for s in samples if s["verified"]]
    report = {
        "n_requested": len(tasks),
        "n_verified": len(verified_only),
        "samples": samples,
        "ttvm": metrics.time_to_verified_mutation(verified_only),
    }
    with open(os.path.join(OUT_DIR, "samples.json"), "w",
              encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print("verified %d/%d; wrote %s" % (
        len(verified_only), len(tasks),
        os.path.join(OUT_DIR, "samples.json")))
    value = report["ttvm"].get("value") or {}
    print("ttvm median_ms=%s mean_ms=%s" % (
        value.get("median_ms"), value.get("mean_ms")))
    return 0 if verified_only else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
