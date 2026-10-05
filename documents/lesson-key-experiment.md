# Lesson Key Experiment — Pilot (memory.md §34)

Pilot for todos.md "Run the lesson key experiment" plus "Compare
semantic+code memory vs code-only memory", over the 8 Family A transfer
tasks (A-TRN-01…08, 16 checks). Full runs are future work; this pilot
validates the harness end to end within a 120-call budget.

## Conditions

Single standard protocol throughout (documents/baseline-a.md): stripped
scoring (`--strip-fences`), `temperature=0.0`, `reasoning_effort="none"`,
`max_tokens=128`, one call per check, no retries, no repair.

| Condition | Memory | Implementation |
|---|---|---|
| A (no-lesson) | None — solve from scratch | `experiments/key_experiment.py` |
| B (raw transcripts) | 2 truncated same-category trajectory excerpts (~674 chars injected/task) | `TRANSCRIPTS` fixtures in `key_experiment.py` |
| C (distilled lessons) | Top-3 lessons from `lessons.py` L2 seeds via `LessonRegistry.retrieve_for_task` (~767 chars/task) | `build_registry` + tag query |
| D (lessons + patches) | Same top-3 lessons + top-2 same-category executable patches (~1088 chars/task) | C plus `PilotPatchMemory` over root `patches.py` |

D's patch memory was seeded offline from 18 exposure tasks using recorded
outputs verified with `runner.compare` (36 verified patches, 0 live
calls). Retrieval everywhere is offline (tag overlap, no model calls).
Semantic pilot: `experiments/semantic_compare.py` runs **code-only**
(top-2 patches, same seeding) vs **semantic+code** (top-1 skill family
from `skills.py` seeds + 2 pilot records/logs families, plus top-2
patches) on the same 8 tasks.

## Results — lesson key pilot (stripped)

| Condition | Tasks | Held-out (= overall) | Repair loops | Tokens/task (in/out/total) | Latency/task | Cost/task |
|---|---|---|---|---|---|---|
| A | 5/8 (62.5%) | 62.5% | 0 | 234.1 / 73.8 / 307.9 | 691.4 ms | $0.000342 |
| B | 6/8 (75.0%) | 75.0% | 0 | 613.1 / 62.0 / 675.1 | 777.4 ms | $0.000699 |
| C | 5/8 (62.5%) | 62.5% | 0 | 541.1 / 65.2 / 606.4 | 1404.2 ms | $0.000633 |
| D | 6/8 (75.0%) | 75.0% | 0 | 857.1 / 67.4 / 924.5 | 744.2 ms | $0.000949 |

Fail sets: A and C fail {TRN-02, TRN-06, TRN-08}; B and D fail {TRN-02,
TRN-06}. Deltas vs A: B +1 task (+0.125), C +0, D +1 task (+0.125);
calls/task identical by design (2.0); memory strictly costs input tokens
(+119% C, +162% B, +200% D total tokens/task).

### Expectation check: D > C > A

Observed **D(6) > C(5) = A(5)** — the D>C leg holds by one task, the C>A
leg fails (equal). The only result flip anywhere is **A-TRN-08 → pass in
B and D**. TRN-08 check 0 is budget-censored: it hits `finish=length` at
exactly 128 output tokens in A and C, but fits (92–106 tokens, `stop`)
under the longer B/D prompts. The flip is therefore an output-brevity
effect of longer contexts, not demonstrated competence transfer — the
same mechanism `documents/baselines-bcd.md` identified for this task.
(C did fix TRN-08 check 1 while still failing check 0.) Neither genuine
error (TRN-02 ISO-week arithmetic, TRN-06 postal hyphen) was fixed by
any condition; memory changed TRN-02's wrong answer across conditions
(`2020-12-27` in A/C/code-only, `2021-01-07` in B/D, `2020-12-24` in
semantic+code) without fixing it, and TRN-06 output was byte-identical
everywhere.

### B-vs-C: transcript-vs-distilled result

**B(6/8) beat C(5/8)** — opposite to the memory.md expectation that raw
transcripts may perform worse than distilled lessons. Caveat: the margin
is exactly the TRN-08 brevity flip, C's injected context was actually
*longer* than B's (767 vs 674 chars), and n=8 single-run cannot separate
a real transcript advantage from prompt-length luck. No distilled-beats-
raw (or reverse) claim is supported; a token-matched B-vs-C rerun plus
the 256-token TRN-08 spot re-check are the cheapest discriminating
follow-ups.

## Results — semantic+code vs code-only pilot (stripped)

| Mode | Tasks | Fail ids | Tokens/task (in/out/total) | Latency/task | Cost/task |
|---|---|---|---|---|---|
| code-only | 6/8 (75.0%) | TRN-02, TRN-06 | 550.1 / 67.4 / 617.5 | 904.2 ms | $0.000645 |
| semantic+code | 6/8 (75.0%) | TRN-02, TRN-06 | 782.6 / 69.4 / 852.0 | 720.6 ms | $0.000878 |

Delta: **+0 tasks**, +234.5 total tokens/task for the semantic block.
Both modes flip TRN-08 identically (106/64 output tokens, `stop`), so
the patch context alone already captures the brevity effect and the
added skill-family procedure bought nothing here. Reuse accounting ran
end to end: 32 patch reuse rows and 8 skill-family outcomes recorded
(plus 16 D-condition reuse rows in the key pilot).

## Spend

96 live Cerebras calls of the 120 budget (64 key + 32 semantic), tiny
contexts only (max ~1088 injected chars/task):

| Run | Calls | In/out/total tokens | Cost | Latency |
|---|---|---|---|---|
| Key A/B/C/D | 64 | 17964 / 2147 / 20111 | $0.020982 | ~28.9 s |
| Semantic | 32 | 10662 / 1094 / 11756 | $0.012182 | ~13.0 s |
| **Total** | **96** | **28626 / 3241 / 31867** | **$0.033164** | **~41.9 s** |

## Artifacts + reproduction

- Per-task JSONs: `artifacts/lesson-key/{A,B,C,D}/A-*.json` (32 files:
  condition, retrieval record, full `runner.py` summary, usage) plus
  `artifacts/lesson-key/summary.json`; `artifacts/semantic-compare/
  {code-only,semantic+code}/A-*.json` (16 files) plus `summary.json`.
- Memory stores: `artifacts/lesson-key/memory/patches-d/` (36 patches,
  16 reuse rows), `artifacts/semantic-compare/memory/{patches,skills}/`
  (36 patches, 32 reuse rows, 5 families, 8 outcomes).
- Live commands (96 calls): `uv run python experiments/key_experiment.py
  --adapter cerebras --max-tokens 128` and `uv run python
  experiments/semantic_compare.py --adapter cerebras --max-tokens 128`.
- Offline checks (0 calls): same commands with `--adapter stub
  --recorded benchmarks/family-a/recorded/stub_all_pass.json` (8/8 all
  conditions/modes, verified), plus `uv run python -m unittest
  tests.test_experiments` (21 tests: aggregation math, delta
  computation, condition/mode labeling, truncation, offline seeding).

## Honest notes

- **Pilot n=8, single run, variance unmeasured.** Every success delta is
  ±1 task and rides entirely on the budget-censored TRN-08 truncation
  boundary. No condition shows a genuine-error fix; treat all
  orderings (D>B, B>C, D>C) as unmeasured noise, not findings.
- **Memory was safe but nearly inert**, consistent with baselines B–D:
  zero regressions vs A anywhere, zero genuine fixes, tokens/task up
  119–200%. Efficiency gains need reuse that shortens outputs — the
  TRN-08 brevity flip is that mechanism firing accidentally, not by
  design.
- **100% retrieval coverage is by construction** (same-category patches
  and excerpts always exist; seeded lessons always rank 3). It proves
  the plumbing, not retrieval quality.
- **B's transcripts are synthesized offline fixtures** modeling exposure
  trajectories, not captured live runs — labeled as such in
  `TRANSCRIPTS`. A full B condition would replay real prior transcripts.
- **D confounds lesson-type with context-size** (lessons + 2 patches ≈
  1.6x B's context). A token-matched D is future work, as is the full
  35-task × 4-condition run (~280 calls), repeated runs for variance,
  and the repair-loops metric (0 by protocol here — repair is disabled,
  so loop-count comparisons need a repair-enabled protocol).
