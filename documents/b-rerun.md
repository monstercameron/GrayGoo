# Baseline B rerun + TRN-08 budget re-check (2026-10-05)

Two follow-up experiments attacking the fragility caveat on success
criterion #1 (held-out success): the entire B/D edge over A was a
single task flip (A-TRN-08), and TRN-08's Baseline-A failure included
a 128-token truncation — so the +1 might have been a budget artifact,
or run-to-run noise.

## 1. TRN-08 budget re-check: the flip is NOT a truncation artifact

Ran A-TRN-08 alone at `--max-tokens 256` (same temp-0.0 / reasoning-off
conditions as Baseline A), both raw and stripped:

- `artifacts/trn08-recheck/raw/A-TRN-08.json` — FAIL
- `artifacts/trn08-recheck/stripped/A-TRN-08.json` — FAIL

At 256 tokens check 0 now passes (134 output tokens, `finish=stop` —
truncation cured) but check 1 still returns `[]` (2 tokens). A fails
TRN-08 at both 128 and 256. Meanwhile B and D at 128 produced a
genuine 57-token answer for check 1. Memory changed the check-1
output; budget did not. Cost: 4 calls, $0.001194.

## 2. Full Baseline B rerun: the flip replicates exactly

`uv run python benchmarks/run_bcd.py --baseline b --adapter cerebras
--artifacts artifacts/baselines-b-rerun --max-tokens 128` — the exact
B protocol (two-phase, frozen P1 text memory, top-2, stripped),
fresh artifact root so the original B files (pinned by
`tests/verify_baselines_bcd.py`) are untouched.

- Rerun: **30/35, delta +1 vs A**, TRN-08 PASSED again.
- Fail sets **identical**: {A-ADV-02, A-EXP-08, A-EXP-16, A-TRN-02,
  A-TRN-06} in both runs.
- Determinism: 35/35 same verdicts AND 35/35 same per-check
  output-token counts across the two runs (temp-0.0 is stable here;
  per-task files don't store output text, so token counts are the
  finest available comparison).
- Cost: 72 calls, 15773 tokens, 36.1 s latency, **$0.016685**.

## Effect on criterion #1

Old standing: PASS (fragile — n=8, one run, sole flip possibly a
truncation artifact). New standing: **PASS (firmer)** — the flip
replicates 2/2 B-runs with identical fail sets, temp-0 is
run-stable, and the truncation-artifact alternative is ruled out by
the 256-token A re-check. Remaining caveat (honest): it is still a
single-task edge (transfer 5/8 → 6/8); competence-vs-luck at n=1
cannot be fully settled without more transfer tasks.

Total new spend: **$0.017879**. Session total ≈ $0.25 of the $50 cap.
