# Baseline A — Family A, No Persistent Memory (35 tasks)

Full run for todos.md "Run baseline A: no persistent memory" (Qwen solves
every task from scratch). Supersedes the 5-task pilot
(`documents/baseline-a-pilot.md`); this run also settles the pilot's open
fence/normalization question empirically by running every task under both
compare conditions.

## Conditions

- Model: `qwen-3.8-27b` via Cerebras, one call per check, no memory, no
  retries, no repair (true from-scratch Baseline A).
- Decoding: `temperature=0.0`, `reasoning_effort="none"`, `max_tokens=128`.
- Tasks: all 35 Family A tasks (18 exposure + 8 transfer + 5 adversarial +
  4 equivalent-transform), 72 checks (46 `json`, 26 `exact`).
- Two compare conditions per task, interleaved (raw then stripped):
  - **raw**: byte output compared as-is (pilot behavior).
  - **stripped**: `--strip-fences` removes exactly one pair of
    ```json...``` fences before compare; per-check `stripped` flags recorded.
- Commands per task (70 invocations, 144 live calls, no retries):
  `uv run python benchmarks/runner.py --adapter cerebras --only <ID>
  --max-tokens 128 [--strip-fences] --json-out
  artifacts/baseline-a/{raw,stripped}/<ID>.json`

## Results

| Condition | Tasks | Success rate | Exposure | Transfer | Adversarial | Eq-transform |
|---|---|---|---|---|---|---|
| raw | 22/35 | 62.9% | 13/18 (72.2%) | 2/8 (25.0%) | 4/5 (80.0%) | 3/4 (75.0%) |
| stripped | 29/35 | 82.9% | 16/18 (88.9%) | 5/8 (62.5%) | 4/5 (80.0%) | 4/4 (100%) |

Raw failures (13): A-ADV-02, A-EQV-01, A-EXP-05, A-EXP-06, A-EXP-08,
A-EXP-12, A-EXP-16, A-TRN-02, A-TRN-03, A-TRN-04, A-TRN-06, A-TRN-07,
A-TRN-08. Stripped failures (6): A-ADV-02, A-EXP-08, A-EXP-16, A-TRN-02,
A-TRN-06, A-TRN-08.

Per-task resource means (identical token/cost totals in both conditions;
per-task output tokens matched 35/35 across conditions, so the two runs
are effectively paired — see notes):

| Condition | Calls/task | Tokens/task (in/out/total) | Latency/task | Cost/task |
|---|---|---|---|---|
| raw | 2.057 | 205.4 / 59.4 / 264.7 | 1223.3 ms | $0.000292 |
| stripped | 2.057 | 205.4 / 59.4 / 264.7 | 1080.9 ms | $0.000292 |

Totals per condition: 72 calls, 7188 in / 2078 out tokens, $0.010209.
Lane spend (both conditions): **144 calls**, 14376 in / 4156 out tokens,
~80.6 s inference latency, **$0.020418** — within the 160-call budget.
Per-call latency spanned 172–3281 ms (19x). `finish_reason`: 71 `stop` +
1 `length` per condition (A-TRN-08 check 0 hit the 128-token cap both
times). 144 unique request ids.

Raw outputs: `artifacts/baseline-a/raw/A-*.json` and
`artifacts/baseline-a/stripped/A-*.json` (70 per-task files: per-check
text, per-check `stripped` flags, usage, request ids) plus
`artifacts/baseline-a/{raw,stripped}-summary.json` (machine-readable
aggregates: per-split rates, means, latency spread, finish reasons).

## Raw-vs-stripped comparison

Stripping flipped **7 tasks** from FAIL to PASS (22→29, +20.0 pp):
A-EQV-01, A-EXP-05, A-EXP-06, A-EXP-12, A-TRN-03, A-TRN-04, A-TRN-07.
Every one was verified by offline paired re-evaluation (strip the recorded
raw actual, re-compare): all 7 raw failures pass after stripping, i.e.
100% of the delta is fence wrapping around otherwise-correct JSON, not
sampling noise. 15/72 checks (20.8%) were fenced — all `json`-compare
(15/46, 32.6%); 0/26 `exact` checks were fenced. No task passed raw and
failed stripped.

The 6 remaining (genuine) failures, identical in both conditions:

| Task | Failure |
|---|---|
| A-ADV-02 | Accepted invalid date `2026-04-31` instead of `ERROR` |
| A-EXP-08 | `[{}]` — dropped null-valued keys instead of explicit nulls |
| A-EXP-16 | Wrong weekdays (`saturday/saturday/friday` vs `monday/saturday/thursday`) |
| A-TRN-02 | ISO week date off (`2020-12-27` vs `2020-12-31`) |
| A-TRN-06 | Postal `N0154` vs `N-0154` (dropped hyphen) |
| A-TRN-08 | Check 0 truncated at 128 tokens (`finish_reason=length`); check 1 `[]` |

Transfer is the weakest split under both conditions (raw 25.0%, stripped
62.5%) — expected for held-out variants, and the headroom baselines B–D
must close.

## Recommended normalization policy for baselines B–D

**Use `--strip-fences` (strip exactly one ```json fence pair before
compare) as the single scoring condition for baselines B–D**, and keep
reporting raw scores alongside for continuity. Rationale:

1. Fence wrapping is the model's dominant output habit (1 in 3 JSON
   checks), not task confusion — stripped content was correct in every
   flipped case.
2. Raw scoring conflates format discipline with task competence: the
   transfer split reads 25% raw vs 62.5% stripped, which would mislead
   learning-delta claims in B–D.
3. The rule is minimal and auditable (one layer only, no recursion,
   per-check `stripped` flags in every output file), so stripped scores
   stay reproducible and fence-rate remains measurable as its own metric.
4. Keep the raw condition as a secondary series (cheap: same calls,
   compare-time only) so B–D can report both "task competence" (stripped)
   and "format discipline" (raw−stripped gap).

Do not extend the rule (no bare-fence variants beyond the implemented
```/```json pair, no JSON repair, no trailing-text trimming) without a
new protocol decision — each extension needs its own empirical delta.

## Honest notes

- **Single run per condition.** One sample of 35 tasks; no confidence
  intervals. Temperature 0.0 made the two runs token-identical (35/35
  per-task output tokens match), which pairs the fence comparison cleanly
  but also means we have measured run-to-run variance at ~zero for this
  decoding — a temperature > 0 repeat would be needed to bound
  nondeterminism honestly.
- **Latency variance is large.** Per-call latency spanned 172–3281 ms
  (19x) within one run on identical settings, so the latency/task means
  (~1.1–1.2 s) are noisy point estimates; experiment 5
  (time-to-verified-mutation) needs repeated runs.
- **One truncation.** A-TRN-08 check 0 hit `max_tokens=128` in both
  conditions, so its failure is budget-censored, not a pure competence
  signal; a 256-token spot re-check would separate truncation from error.
- **Token/cost scale.** At $0.000292/task/condition, a full 35-task
  baseline costs ~$0.01/condition — measurement is negligible; the work is
  analysis, not inference spend.
