# Baseline A Pilot — Family A Exposure (5 tasks)

Pilot run for todos.md "Run baseline A: no persistent memory" (Qwen solves
every task from scratch). Full Baseline A is still open; this pilot validates
the live adapter path, the cost-tracking plumbing, and the per-task metrics.

## Conditions

- Model: `qwen-3.8-27b` via Cerebras, one call per check, no memory, no
  retries, no repair (true from-scratch Baseline A).
- Decoding: `temperature=0.0`, `reasoning_effort="none"`, `max_tokens=128`.
- Tasks: 5 exposure tasks covering all 4 Family A categories and both compare
  modes: A-EXP-01 (dates/exact), A-EXP-03 (dates/json), A-EXP-05 (csv/json),
  A-EXP-11 (records/json), A-EXP-15 (logs/json).
- Command per task:
  `python benchmarks/runner.py --adapter cerebras --only <ID> --max-tokens 128
  --json-out artifacts/baseline-a-pilot/<ID>.json`

## Results

| Task | Result | Calls | In/out tokens | Latency | Cost |
|---|---|---|---|---|---|
| A-EXP-01 (dates) | PASS (2/2 checks) | 2 | 255 / 66 | 625.0 ms | $0.000351 |
| A-EXP-03 (dates) | PASS (2/2 checks) | 2 | 149 / 36 | 828.0 ms | $0.000201 |
| A-EXP-05 (csv) | FAIL (0/2 checks) | 2 | 171 / 108 | 1219.0 ms | $0.000330 |
| A-EXP-11 (records) | PASS (2/2 checks) | 2 | 190 / 21 | 688.0 ms | $0.000219 |
| A-EXP-15 (logs) | PASS (2/2 checks) | 2 | 121 / 17 | 921.0 ms | $0.000145 |
| **Total (5 tasks)** | **4/5 (80%)** | **10** | **886 / 248 (1134)** | **4281.0 ms** | **$0.001246** |
| Mean per task | — | 2.0 | 177.2 / 49.6 (226.8) | 856.2 ms | $0.000249 |

Raw outputs: `artifacts/baseline-a-pilot/A-EXP-*.json` (per-check text,
usage, request ids) plus `artifacts/baseline-a-pilot/pilot-summary.json`
(machine-readable aggregate). All 10 calls returned `finish_reason=stop`
(no truncation at 128 tokens) on `qwen-3.8-27b` with 10 unique request ids.

## Honest notes

- **Tiny pilot.** 5 of 18 exposure tasks, exposure split only. No transfer,
  adversarial, or equivalent-transform evidence, so this says nothing about
  generalization — the point of Baseline A. Treat 80% as a plumbing
  confirmation, not a success-rate estimate.
- **The one failure is format discipline, not semantics.** A-EXP-05 produced
  the correct JSON content wrapped in ```json fences, which the runner's
  `json` compare (raw `json.loads`) rejects. Recorded as FAIL as-run; whether
  the full baseline strips fences is a protocol decision for the coordinator
  (stripping would be a different, non-raw condition).
- **Latency variance is large.** Per-call latency spanned 172–1016 ms (5.9x)
  within minutes on identical settings, so per-task latency needs repeated
  runs before it can support experiment 5 (time-to-verified-mutation).
- **Single run, one decoding.** Temperature 0.0 reduces but does not remove
  API nondeterminism; no confidence intervals are claimed.
- **Token/cost scale.** At the pilot mean ($0.000249/task), a full 35-task
  Baseline A would cost roughly $0.01 in inference — measurement cost is
  negligible; the work is in running and analyzing all splits.

## What a full Baseline A still needs

1. All 35 Family A tasks (18 exposure + 8 transfer + 5 adversarial + 4
   equivalent-transform, ~70 calls) under these same conditions.
2. A fence/normalization policy decided up front and applied uniformly.
3. Per-split success rates plus calls/tokens/latency/cost per task (todos.md
   items: calls/task, tokens/task, latency/task, success rate, cost/task).
4. At least one repeat of a latency-sensitive subset to bound variance.
5. Comparison scaffolding so baselines B–D can reuse the same runner,
   `--json-out` format, and aggregation.

## Lane spend

11 Cerebras calls total (1 adapter smoke + 10 pilot), 903 input / 250 output
tokens, ~5109 ms inference latency, ~$0.001266 — within the 20-call budget.
