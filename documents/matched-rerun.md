# Matched Rerun — Fast-Path Reuse vs FAIL Criteria #2/#3

Token-matched rerun attacking the two FAILed success criteria from
`documents/learning-assessment.md`: #2 calls/task (flat 2.057) and #3
tokens/task (+65–135% over A). The 8 transfer tasks run under three
matched conditions with the standard protocol (stripped, temp 0.0,
reasoning off, max_tokens 128, ≤1 call/check).

## Conditions

| Condition | Memory behavior |
|---|---|
| A (no memory) | From scratch; reproduces the Baseline A transfer subset |
| C-fast-path | Patch fast-path: high-confidence hit (exact family+category-tag match, ≥1 helped, 0 hurt, never adversarial) → reuse recorded output with ZERO model calls; miss → assisted call with standard top-2 patch block |
| D-fast-path | Same patch fast-path (shared Phase-1 snapshot) + concise text memory (top-1 entry, 120-char budget, token estimates recorded); assisted calls inject ONLY the concise text block |

Fast-path memory is a frozen Phase-1-only snapshot (32 exposure patches +
34 history rows copied offline from `artifacts/baselines-bcd/c`, posthoc
Phase-2 patches excluded; confidence signal frozen before the run).
Driver: `experiments/matched_rerun.py`. New code is additive only:
`PatchMemory.reuse_history/find_fast_path/fast_path_output`,
`text_memory.estimate_tokens/format_block_concise` (+ `CONCISE_*` budgets).

## Results (live, per-condition table)

| Metric | A | C-fast-path | D-fast-path |
|---|---|---|---|
| Success | 5/8 (62.5%) | 3/8 (37.5%) | 4/8 (50.0%) |
| Fail ids | TRN-02, 06, 08 | TRN-02, 03, 04, 06, 08 | TRN-02, 03, 04, 06 |
| Calls/task | 2.000 | 1.500 | 1.500 |
| Tokens/task (in/out/total) | 234.1/73.8/307.9 | 483.8/60.1/543.9 | 301.0/54.6/355.6 |
| Fast-path hits / hit rate | 0/16 | 4/16 (25.0%) | 4/16 (25.0%) |
| Fast-path input matches | — | 0/4 | 0/4 |
| Cost/task | $0.000342 | $0.000568 | $0.000380 |
| Totals (calls/tokens/cost) | 16/2463/$0.002734 | 12/4351/$0.004547 | 12/2845/$0.003036 |

Reference bars (full-35-task A means): 2.057 calls/task, 264.7 tokens/task.
Matched A-rerun transfer-only means: 2.000 / 307.9.

## Verdicts (honest: neither FAIL flips)

- **#2 calls/task — STILL FAIL.** C/D cut calls/task to 1.5 (< 2.057),
  but success was NOT held (3/8, 4/8 vs A 5/8): all 4 fast-path hits
  fired on novel inputs (0/4 input matches) and returned the wrong
  recorded output (`A-EXP-07`'s `[{"a":"\"quoted\""}]` for four unrelated
  csv checks), breaking TRN-03 and TRN-04 which A passed. The flip bar is
  "calls/task < 2.057 with success held" — saving calls by emitting wrong
  answers does not meet it. Net: −2 tasks (C) / −1 task (D) vs matched A.
- **#3 tokens/task — STILL FAIL.** C 543.9 (+236.0 vs matched A) and D
  355.6 (+47.7) both exceed matched A (307.9) and the 264.7 bar. Even with
  25% zero-call checks, injection on the assisted checks dominates. The
  concise direction helps substantially (D +15% vs C +77% over matched A)
  but does not reach net-negative on its own.

Side observation: D passed TRN-08 while A and C hit the 128-token cap
(`length`) — a second output-brevity flip under memory-augmented prompts
(D check 0: 92 tokens, `stop`), mirroring the BCD TRN-08 story. It does
not change either verdict.

## Spend

40 Cerebras calls of the 80-call budget (A 16 + C 12 + D 12), tiny
contexts only (max_tokens 128): 8151 in / 1508 out tokens, $0.010317
total. All request ids unique (16/12/12 per condition).

## Verification evidence

- A-rerun reproduces Baseline A transfer exactly: 8/8 pass-match AND 8/8
  per-task output-token-match vs `artifacts/baseline-a/stripped/A-TRN-*`.
- Offline stub flow check first (0 calls): A 8/8, C/D 6/8 with identical
  4-hit fast-path patterns, confirming shared-snapshot accounting before
  any live call.
- Fast-path hits verified per-check in raw JSONs (patch id, source task,
  input_match=false on all 4); reused outputs recorded in failure rows.
- Full repo suite after the additive edits: 581 tests OK (2 expected
  failures, pre-existing), so no existing behavior changed.

## What remains

1. **Input-conditional gating.** Family+tags+history is far too coarse a
   confidence signal for zero-call reuse: it fires on any same-category
   task regardless of input. A sound trigger needs (near-)exact input
   match — which occurs 0/16 times on transfer tasks, so gating alone
   yields ~0 hits here, not savings.
2. **Reuse must adapt, not replay.** Verbatim output replay only answers
   repeated inputs. Real call/token savings need adaptation: rewrite the
   recorded procedure for the new input (rules or a small edit call) and
   count the net.
3. **Token-matched injection.** Concise top-1 (D) nearly closed the gap
   (+47.7 vs +236.0 for C). Pairing concise injection with a gated,
   adapting fast-path is the next combined test.
4. **Transfer-family coverage.** This family's transfer split needs fresh
   reasoning, not adaptation; a fast-path win needs tasks where Phase-1
   procedures actually transfer (or repeated-input workloads).

## Artifacts

- Raw per-task JSONs: `artifacts/matched-rerun/{A,C,D}/A-TRN-*.json`
  (24 files: per-check fast-path records, injected token estimates,
  full runner summaries with usage + request ids)
- Aggregate: `artifacts/matched-rerun/summary.json` (condition table,
  deltas vs A, snapshot provenance, budget)
- Snapshots: `artifacts/matched-rerun/memory/` (Phase-1 patch copies +
  frozen `text_memory_p1.json`)
