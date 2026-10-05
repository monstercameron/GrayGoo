# Baselines B/C/D — Family A, With Memory (35 tasks each, stripped)

Two-phase memory baselines over all 35 Family A tasks, scored under the
single standard condition from `documents/baseline-a.md`: stripped
(`--strip-fences`), `temperature=0.0`, `reasoning_effort="none"`,
`max_tokens=128`, one call per check, no retries, no repair.

## Design

Each baseline runs two phases with the same frozen protocol:

- **Phase 1:** solve the 18 exposure tasks from scratch (no memory).
  Fully-passed tasks contribute their verified check outputs to memory.
- **Freeze:** retrieval views are snapshotted. Phase 2 successes are
  held pending and flushed only after the run (growth accounting), so
  Phase 2 retrieval sees exactly the Phase 1 memory.
- **Phase 2:** solve 8 transfer + 5 adversarial + 4
  equivalent-transform tasks with memory injected into the system
  prompt (top-k retrieval, truncated excerpts, tiny contexts).

| Baseline | Memory | Module |
|---|---|---|
| B (textual) | (prompt → successful output) text pairs; top-2 by keyword overlap + same-category bonus | `benchmarks/text_memory.py` |
| C (executable) | Phase-1 successes persisted via `patches.py` `PatchStore` (family `A`, tags `[category, family-a, task-id]`); top-2 same-category patches, `derived_from` preferred for eqv tasks; reuse recorded via `record_reuse` | `benchmarks/patch_memory_adapter.py` |
| D (dual) | Both memories fully active (top-2 text + top-2 patches) | both |

Retrieval is fully offline (no model calls). A retrieval "hit" (B/D)
means genuine keyword overlap > 0; patch "reuse" (C/D) means ≥1 patch
retrieved; "helped" means the reusing task passed. Driver:
`benchmarks/run_bcd.py` (reuses `runner.py` scoring; no `runner.py`
changes). Negative transfer is reported two ways: **A-relative**
(A-stripped-passed Phase-2 task now fails — the causal regression
signal) and **outcome-level** (reuse rows with `helped=false` from the
root `transfer.py` accounting — includes tasks A also failed).

## Results

### Headline (stripped)

| Baseline | Tasks | Success | Exposure | Transfer | Adversarial | Eq-transform |
|---|---|---|---|---|---|---|
| A (no memory) | 29/35 | 82.9% | 16/18 (88.9%) | 5/8 (62.5%) | 4/5 (80.0%) | 4/4 (100%) |
| B (text) | 30/35 | 85.7% (+1) | 16/18 (88.9%) | 6/8 (75.0%) | 4/5 (80.0%) | 4/4 (100%) |
| C (patch) | 29/35 | 82.9% (+0) | 16/18 (88.9%) | 5/8 (62.5%) | 4/5 (80.0%) | 4/4 (100%) |
| D (dual) | 30/35 | 85.7% (+1) | 16/18 (88.9%) | 6/8 (75.0%) | 4/5 (80.0%) | 4/4 (100%) |

Fail sets: B and D fail {A-EXP-08, A-EXP-16, A-TRN-02, A-TRN-06,
A-ADV-02} — A minus A-TRN-08. C fails the identical 6 tasks as A.
The **sole result flip in 216 calls is A-TRN-08 → pass in B and D**;
every other failure is byte-identical to A's output (verified by
offline per-check comparison of recorded failing actuals).

### Resources (per task mean; 72 calls / 2.057 per task each)

| Baseline | In/out/total tokens | Latency | Cost | Fenced checks |
|---|---|---|---|---|
| A | 205.4 / 59.4 / 264.7 | 1080.9 ms | $0.000292 | 15/72 |
| B | 389.4 / 61.3 / 450.7 | 574.6 ms | $0.000477 | 16/72 |
| C | 378.2 / 58.3 / 436.5 | 723.3 ms | $0.000461 | 10/72 |
| D | 562.2 / 61.3 / 623.5 | 767.9 ms | $0.000648 | 16/72 |

Totals: B 13629/2144 tok $0.016685; C 13236/2042 tok $0.016146; D
19677/2144 tok $0.022672. Phase-2 input tokens vs A: B +173%, C +163%,
D +336% — memory strictly costs context here; no token savings were
observed (outcome-level `tokens_saved` is negative: −12024 C, −25110 D
at 2 rows/task). Per-call latency spanned 157–2531 ms across runs
(same 19x noise as A). `finish_reason`: B/D 72 `stop`; C 71 `stop` + 1
`length` (A-TRN-08 check 0, same truncation as A). 216/216 unique
request ids.

### Memory metrics

| Metric | B (text) | C (patch) | D (dual) |
|---|---|---|---|
| Phase-1 stored (entries / patches) | 32 / — | — / 32 | 32 / 32 |
| Final library (entries / capability count) | 62 / — | — / 60 | 62 / 62 |
| Library growth (Phase-2 adds) | +30 | +28 | +30 / +30 |
| Retrieval coverage (Phase-2 tasks) | 17/17 | 17/17 | 17/17 + 17/17 |
| Hit rate (keyword overlap > 0) | 17/17 (1.0) | — | 17/17 (1.0) |
| Reuse helped (tasks) | — | 13/17 | 14/17 |
| Held-out reuse success (transfer rows) | — | 10/16 (62.5%) | 12/16 (75.0%) |
| Outcome-level negative transfer (rows) | — | 8/34, 0 severe | 6/34, 0 severe |
| **A-relative negative transfer (tasks)** | **0** | **0** | **0** |
| A-relative positive transfer (tasks) | 1 (A-TRN-08) | 0 | 1 (A-TRN-08) |
| Text reuse entropy (nats) | 1.7582 | — | 1.7582 |
| Capability (patch reuse) entropy (nats) | — | 2.6186 | 2.6186 |

(Todos mapping: B re-run/compare-no-memory/compare-executable,
C re-run/reuse/negative-transfer/capability-count/growth, and D
dual/held-out/transfer/tokens-per-task/entropy are all in the tables
above; held-out = transfer split.)

## Comparison

- **vs A:** B +1, C +0, D +1; zero A-relative regressions anywhere.
  Exposure is exactly A in all three (16/18, same fails) — expected,
  since Phase 1 is the no-memory protocol.
- **B vs C (text vs executable):** differ by one task (TRN-08) — no
  meaningful superiority either way at n=1.
- **D vs B/C:** D equals B on results at ~2x the memory-token cost
  (Phase-2 +336% vs +173%); the added patch context bought nothing
  here. D confounds memory-type with context-size (2+2 examples);
  a token-matched D is future work.
- **Transfer (held-out) success:** A 5/8, B 6/8, C 5/8, D 6/8 — the
  only movement is the TRN-08 flip.
- **Effect isolation is clean:** Phase-1 outputs are bit-identical to
  A in all three runs (18/18 exposure output-token matches), so every
  Phase-2 delta is attributable to memory, not resampling. Memory
  altered surface form on a few still-passing Phase-2 tasks (12–13/17
  output-token-identical to A) and C fenced less (10/72 vs 15/72),
  but fixed none of the 5 genuine errors.

## Artifacts + reproduction

- Per-task JSONs: `artifacts/baselines-bcd/{b,c,d}/A-*.json` (105
  files: baseline/phase, retrieval record, stored counts, full
  `runner.py` summary with per-check `stripped` flags, usage,
  request ids) plus `{b,c,d}-summary.json` aggregates.
- Memory stores: `artifacts/baselines-bcd/{b,c,d}/memory/`
  (`text_memory_p1.json` frozen view, `text_memory.json` final,
  `patches-{c,d}/` PatchStore dirs with `reuse.jsonl` outcomes).
- Live commands (72 calls each, 216 total of the 240 budget):
  `uv run python benchmarks/run_bcd.py --baseline {b,c,d} --adapter
  cerebras --max-tokens 128 --artifacts artifacts/baselines-bcd`.
  Offline flow check (stub, zero calls): `--adapter stub --recorded
  benchmarks/family-a/recorded/stub_all_pass.json` (+ mixed/all-fail
  fixtures verified transfer math and the empty-memory edge).

## Honest notes

- **Single run each; the effect is n=1.** The +1 (TRN-08) is not
  statistically significant and TRN-08 is the budget-censored task:
  its check 0 hits `max_tokens=128` in A and C (`finish=length`)
  but fits under text-memory prompts in B/D. The flip may be an
  output-brevity effect of longer prompts, not competence transfer.
  The 256-token spot re-check proposed in baseline-a.md would
  separate truncation from error; it was not run (frozen protocol).
- **Memory was safe but nearly inert.** Zero regressions, but also
  zero fixes to the genuine errors (weekday arithmetic, ISO week
  dates, postal hyphen, invalid-date acceptance). Temperature 0.0
  determinism (bit-identical Phase 1 across 4 runs) pairs the
  comparison cleanly while leaving run-to-run variance unmeasured
  for the new memory-augmented prompts.
- **No efficiency win.** Calls/task identical by design (2.057);
  tokens/task rose 65–135%; latency differences are noise.
  Efficiency gains need reuse that shortens outputs — not observed.
- **100% retrieval coverage is by construction** (same-category
  always matches; shared family vocabulary), not evidence of
  retrieval quality. Harder tests need cross-category or
  distractor queries, and tasks where Phase-1 procedures actually
  transfer (this family's transfer split mostly needs fresh
  reasoning, not adaptation).
- **What this proves:** the two-phase harness and both adapters run
  end-to-end within budget; patch persistence/reuse/transfer
  accounting works through the repo's own `patches.py`/`transfer.py`;
  results are reproducible. **What it doesn't:** any general memory
  benefit, text-vs-patch superiority, or scaling beyond Family A.
