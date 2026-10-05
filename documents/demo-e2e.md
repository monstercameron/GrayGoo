# First End-to-End Self-Learning Demo (plan.md §§80, 92)

Runner: `demo.py` (root). All run outputs: `artifacts/demo-e2e/`.
Model: `qwen-3.8-27b` via Cerebras, `temperature=0.0`,
`reasoning_effort="none"`, `max_tokens=2048`.

## 1. Baseline-A independent verification — MATCH (68/68)

An independent script (`tests/verify_baseline_a.py`,
stdlib only, no imports from `benchmarks/`) recomputed every number
claimed in `documents/baseline-a.md` directly from
`artifacts/baseline-a/raw/A-*.json` and `stripped/A-*.json`:

- per-split success rates (raw 22/35, stripped 29/35, all four splits
  both conditions) and fail-id lists — match;
- per-task means (calls 2.057, tokens 205.4/59.4/264.7, latency
  1223.3/1080.9 ms, cost $0.000292) and per-condition totals (72
  calls, 7188/2078 tokens, $0.010209) — match;
- cross-condition claims: 7 flipped tasks (exact ids), 0 regressions,
  15/72 fenced checks all `json` (0/26 `exact`), 35/35 paired
  per-task output tokens, 144 calls / 144 unique request ids lane-wide,
  latency span 172–3281 ms, 71 `stop` + 1 `length` per condition,
  A-TRN-08 check-0 truncation both conditions — all match.

Verdict `MATCH`, 68/68 claims:
`artifacts/demo-e2e/verification.json`. No mismatches found.

## 2. Demo transcript summary

`uv run python demo.py --live` wires the real chain per task —
`retrieve` → `context.compile_context` → `cerebras.generate_candidate`
→ `s_expr` validate → `risk.classify` →
`pipeline.run_candidate(worker_fn=workers.run_lisp)` →
`repair_loop(max_repairs=1)` → `patches.save_patch` →
`transfer.record_reuse` — over three related Family-A CSV tasks in
sequence (A-EXP-05 → A-EXP-07 → A-EXP-08). The learned skill is the
CSV parser: tests prepend a fixed harness (`gg-emit-json` JSON
emitter + `gg-json-normalize` comparator), so the model writes only
parsing code; the harness is never persisted as a patch/skill.

| Task | Outcome | Calls | Patch reused | Transfer (seed patch) |
|---|---|---|---|---|
| A-EXP-05 | fresh success (repair 1) | 2 | n/a (none existed) | — |
| A-EXP-07 | **reused, 0 calls** | 0 | yes (`patch-9b13…`, score 9.50) | direct/helped |
| A-EXP-08 | adapted, 1 call, no repair | 1 | no (direct failed `""/null`, then adapted) | direct/missed + adaptation/helped |

Per-task detail:

- **A-EXP-05** (`artifacts/demo-e2e/A-EXP-05.json`). Retrieve: no
  match. Generate (938/853 tok): fenced, `#\"` literals, 2 parens
  short — all three documented normalizations applied, then
  rehearsed; direct failed with an actual-vs-expected `:mismatch`
  counterexample. Repair (2523/809 tok): unfenced, 1 paren completed,
  then **parse/risk/direct all pass**, risk R1, patch persisted.
- **A-EXP-07** (`artifacts/demo-e2e/A-EXP-07.json`). Retrieve ranked
  the seed 9.50; direct reuse of the stored candidate text as-is
  **passed all stages, 0 model calls**, risk R1. This is the §80
  "Task 2 uses fewer tokens" step at its limit: zero.
- **A-EXP-08** (`artifacts/demo-e2e/A-EXP-08.json`). Direct reuse
  **failed honestly** (emits `""` where the task needs `null`;
  counterexample quotes both). Adaptation with the seed in context
  (754 words) produced raw-valid output in **1 call, 0 repairs**:
  direct **and regression (tasks 1+2 checks) pass**, risk R1, second
  patch persisted — reuse with adaptation and no regressions.

Totals (final run): **3 calls** (budget 35), 7908 tokens (5429 in /
2479 out), 3.5 s inference latency, **$0.009068**, 8.3 s elapsed —
`artifacts/demo-e2e/summary.json`, `transcript.log`. Whole lane
including 7 single-task iterations: 23 calls, ~$0.061, within budget.

## 3. Honest notes

- **Thresholds NOT hit for full skill promotion.** Transfer counts
  only: the seed patch has 3 outcomes / 2 independent reuses
  (A-EXP-07 direct/helped, A-EXP-08 direct/missed +
  adaptation/helped) → gate decision `hold` (needs 3 per plan.md
  §30). The task-3 patch has 0 reuses → `hold`. Nothing was
  promoted; no zero-call "future equivalent task" step ran.
- **Output normalizations were load-bearing.** Raw model output was
  valid only 1/3 times (the adaptation). The other two needed the
  documented, per-call-reported chain: one fence-pair strip (the
  baseline-A rule), `#\"`/`#\,`→`code-char` rewrite (bridges two
  known `s_expr` gaps vs real CL: no `#\"` literal, `,` as
  whitespace), and ≤6 paren completion at EOF (unique minimal
  completion, verified by re-parse + rehearsal). Raw texts and flags
  are in every `calls[]` entry; logic is 100% model's, and rehearsal
  still gates everything. Iteration transcripts: `iter1/`–`iter7/`.
- **Single repair budget.** `max_repairs=1` per §92; task 1 spent it
  on semantics (syntax came from normalization), task 3 needed
  none. A task failing twice still escalates, as the early
  iterations show.
- **Narrow scope.** One CSV family, 2 checks/task, R1 pure-ish code,
  no property/differential/performance stages, no composition,
  no skill generalization (§80 task 3). Temperature 0.0:
  deterministic but single-sample.
- **What a full §92 run needs:** ≥3 independent reuses to flip the
  gate to `promote`; a generalized family abstraction; a held-out
  equivalent task executing with 0 calls post-promotion; and either
  a more robust emitter (fewer normalizations) or `s_expr` support
  for `#\"`/`,` so raw validity stops depending on the bridge.
