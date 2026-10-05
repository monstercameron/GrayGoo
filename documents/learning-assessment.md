# Learning Assessment — Baselines A–D Evidence

Recomputed from raw artifacts with `metrics.py` (`python metrics.py`);
no value below is copied from doc tables. Every metric carries its
evidence basis (`{"value", "n", "basis"}`). Sources: 70
`artifacts/baseline-a` + 105 `artifacts/baselines-bcd` per-task JSONs,
2 `reuse.jsonl` ledgers (34 rows each), text-memory snapshots, patch
store file counts.

Critical caveat for the whole document: **A→B/C/D is a cross-condition
comparison, not a time series.** Each baseline ran once. Any "trend"
across A→D compares memory conditions, not learning over time.

## (a) Metrics table (recomputed)

### Task success

| Metric | A (strip) | B (text) | C (patch) | D (dual) | Basis |
|---|---|---|---|---|---|
| Success rate | 29/35 (82.9%) | 30/35 (85.7%) | 29/35 (82.9%) | 30/35 (85.7%) | n=35 task files/condition |
| Held-out (transfer) | 5/8 (62.5%) | 6/8 (75.0%) | 5/8 (62.5%) | 6/8 (75.0%) | n=8 transfer files/condition |
| Exposure | 16/18 | 16/18 | 16/18 | 16/18 | identical fail sets |

### Resources per task (means over 35 raw per-task totals)

| Metric | A | B | C | D |
|---|---|---|---|---|
| Calls/task | 2.057 | 2.057 | 2.057 | 2.057 |
| Tokens/task (in/out/total) | 205.4/59.4/264.7 | 389.4/61.3/450.7 | 378.2/58.3/436.5 | 562.2/61.3/623.5 |
| Latency/task (ms) | 1080.9 | 574.6 | 723.3 | 767.9 |
| Cost/task (USD) | $0.000292 | $0.000477 | $0.000461 | $0.000648 |

Cross-condition deltas (least-squares over A→B→C→D, n=4 points each):
calls **flat** (slope 0); tokens **increasing** (slope +106.2,
+71.8% relative); latency **decreasing** (slope −79.0) — but per-call
latency spans 157–3281 ms within runs, so the latency delta is noise,
not an efficiency win.

### Reuse / transfer (Phase-2, n=17 tasks/condition)

| Metric | B | C | D | Basis |
|---|---|---|---|---|
| Reuse rate (any retrieval) | 17/17 | 17/17 | 17/17 | per-task retrieval records |
| Text hit rate | 17/17 | 0/17 | 17/17 | keyword overlap > 0 |
| Patch reuse rate | 0/17 | 17/17 | 17/17 | ≥1 patch retrieved |
| Reuse helped (reusers passing) | 14/17 | 13/17 | 14/17 | retrieval + outcome join |
| Transfer success (held-out rows) | — | 10/16 (62.5%) | 12/16 (75.0%) | reuse.jsonl, held_out=true |
| Outcome neg. transfer | — | 8/34, 0 severe | 6/34, 0 severe | reuse.jsonl rows |
| **A-relative regressions** | **0** | **0** | **0** | A-passed→now-fails, n=17 |
| A-relative improvements | 1 (TRN-08) | 0 | 1 (TRN-08) | A-failed→now-passes |

100% retrieval coverage is by construction (same-category always
matches), not evidence of retrieval quality.

### Capability library

| Metric | B | C | D | Basis |
|---|---|---|---|---|
| P1 → final (text entries) | 32 → 62 (+30) | — | 32 → 62 (+30) | snapshots + per-task `stored` sums agree |
| P1 → final (patches) | — | 32 → 60 (+28) | 32 → 62 (+30) | patch-file counts + `stored` sums agree |
| Text reuse entropy (nats) | 1.7582 | — | 1.7582 | 7 source tasks, 34 obs |
| Patch reuse entropy (nats) | — | 2.6186 | 2.6186 | 16 patches, 34 obs; ledger and per-task routes agree |

### Time-to-verified-mutation

TBD (n=0): no verified-mutation timing samples exist anywhere in the
artifacts (plan.md §50 stage timings were never recorded; per-task
inference latency is not a substitute).

## (b) Experiment-success verdicts (todos.md, 8 criteria)

| # | Criterion | Verdict | Justification |
|---|---|---|---|
| 1 | Held-out success increases or stays stable | **PASS** | 5/8 → 6/8, 5/8, 6/8 meets the bar as stated — but fragile: n=8, one run, sole flip is budget-censored TRN-08 |
| 2 | Avg model calls/task decreases | **FAIL** | Flat 2.057 in all four conditions (identical by protocol design) |
| 3 | Avg tokens/task decreases | **FAIL** | Rose 65–135% over A (264.7 → 450.7/436.5/623.5); memory strictly costs context here |
| 4 | Capability reuse increases | **UNMEASURABLE-YET** | Single snapshot per condition at a by-construction 100% ceiling; "increases" needs a time series |
| 5 | Negative transfer below threshold | **PASS** | 0 A-relative regressions and 0 severe rows in all conditions (plan §30 gate: severe = 0) |
| 6 | Capability growth becomes sublinear | **UNMEASURABLE-YET** | One growth interval per condition; the observed interval is store-everything (≈linear), but one interval cannot establish curve shape |
| 7 | Zero canonical-state corruption | **UNMEASURABLE-YET** | Zero incidents recorded, but this evidence contains no rehearsal-boundary runs at all (v1 bar: 1000 adversarial executions) |
| 8 | Rollback works reliably | **UNMEASURABLE-YET** | No promotion/rollback exercise exists in the baseline artifacts (offline runs only) |

Score: 2 PASS (1 fragile) / 2 FAIL / 4 UNMEASURABLE-YET. The system is
safe-but-inert so far: memory caused zero regressions and fixed nothing
except one truncation artifact, at higher token cost.

## (c) What would flip each FAIL / UNMEASURABLE

- **#2 calls/task (FAIL):** reuse that answers checks without model
  calls (currently every check costs one call by design); needs a
  protocol where retrieved capabilities can satisfy checks directly.
  Re-run shows calls/task < 2.057 with success held.
- **#3 tokens/task (FAIL):** retrieval that shortens prompts or
  outputs net of its own context cost (smaller top-k, token-matched D,
  concise excerpts); re-run shows total/task < 264.7.
- **#4 reuse increases (UNMEASURABLE):** ≥2 timepoints of reuse rate on
  a growing task stream where coverage is not structurally 100%
  (cross-category/distractor queries); reuse rate or reuse-helped rate
  rising across runs flips to PASS.
- **#6 sublinear growth (UNMEASURABLE):** library size sampled across
  ≥3 cumulative task counts with consolidation/forgetting active;
  concave size-vs-tasks curve flips to PASS.
- **#7 corruption (UNMEASURABLE):** rehearsal attack-suite results
  (plan §86: 1000 adversarial candidates) plus a corruption-incident
  ledger; 1000/1000 contained flips to PASS.
- **#8 rollback (UNMEASURABLE):** N rollback drills (promote → break →
  roll back → verify) with a success rate; sustained 100% over a
  meaningful N flips to PASS.
- **#1 held-out (fragile PASS):** repeated runs (temperature > 0 or
  more transfer tasks) plus the 256-token TRN-08 re-check to confirm
  the +1 is competence, not a truncation artifact.
