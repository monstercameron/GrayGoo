# Supplemental transfer set: validation + memory runs (2026-10-05)

Doubled the held-out set (A-TRN-09..16; generator + freeze proof in
`benchmarks/family-a/README.md`, tests in `tests/test_transfer2.py`)
because transfer recall cannot settle at n=8. Three questions: do the
new tasks validate (fair, deterministic A outcomes)? Does memory move
any of them? Does the TRN-08 edge replicate into a second edge?

## A validation (`artifacts/transfer2/`, 64 calls, $0.0096)

All 16 transfer tasks × raw/stripped, standard conditions:

- Old 8 replicate EXACTLY: raw fails {02,03,04,06,07,08}, stripped
  5/8 (fails {02,06,08}) — identical to Baseline A. Temp-0.0 stable
  again (third replication: B-rerun, repeat-stream, now this).
- New 8: raw 4/8 (11 fails on fences, 09/10/15 fail), stripped 6/8
  (fails {09,10}).
- TRN-09/10 fails are fair model arithmetic errors (epoch-day miscount
  by 32h; EST→UTC off-by-one-hour with right date rollover).
- TRN-15 caught one genuine spec ambiguity pre-baseline ('-' user:
  null vs verbatim); fixed by stating the null mapping explicitly
  (consistent with the task's own bytes rule), re-passes both
  conditions. Documented in the commit, not silently tuned.

## Memory runs B/D on TRN-09..16 (`artifacts/baselines-transfer2/`)

New `--phase2-ids` partial runs (full 18-exposure Phase 1 each for
identical seeding; `transfer2_vs_a` evidence tables in the
summaries; driver changes + 6 tests committed separately):

- B: 6/8, improvements=[], regressions=[] — memory-invariant.
- D: 6/8, improvements=[], regressions=[] — memory-invariant.
- C skipped deliberately: with B and D both null, a patch-only flip
  either way would be uninterpretable single-task noise ($0.013
  saved; the call is recorded here, not hidden).

## Reading: memory moves exactly 1/16 transfer tasks

Combined transfer picture (16 tasks): A 11/16, B 12/16, D 12/16 —
the SOLE memory edge remains TRN-08 (replicated 4× now: B, B-rerun,
D, gated-D). 15 of 16 held-out tasks are memory-invariant, including
both A-failures that memory could have fixed (TRN-09/10: retrieval
cannot fix arithmetic) and all 6 A-passes (no regressions — the #5
safety claim extends to the new set).

Criterion #1 firms further (edge replicates, zero regressions at
n=16) but stays a single-task edge: the hoped-for second transfer
edge did not appear. The gate's transfer premise is unchanged —
still closed, now with twice the evidence.

Cost: 168 calls, **$0.032639**. Session total ≈ **$0.33 of $50**.
