# Gated matched rerun: input-match fast-path fix (2026-10-05)

Follow-up to `documents/matched-rerun.md`, whose patch fast-path fired
on family+category+confidence alone: 4/16 hits, **0/4 input-matched**,
returned wrong recorded outputs (`A-EXP-07`'s JSON for four unrelated
csv checks), and broke TRN-03/TRN-04 that A passed.

## Fix

`PatchMemory.find_fast_path` gained `check_input`: when provided, the
recorded `candidate["input"]` must EQUAL the current check input or
the patch does not fire (`benchmarks/patch_memory_adapter.py`).
`experiments/matched_rerun.py` passes it. Legacy callers (no
`check_input`) keep the old rule. Pinned by
`tests/test_patch_memory.py` (4 tests: legacy fires, novel blocked,
exact fires, adversarial never).

## Re-run (same protocol, fresh root)

`uv run python experiments/matched_rerun.py --adapter cerebras
--artifacts artifacts/matched-rerun-gated` — 8 transfer tasks × A/C/D.

| Cond | Passed | Fast-path hits | Input matches | Calls/task | Tokens/task |
|---|---|---|---|---|---|
| A (rerun) | 5/8 | 0 | 0 | 2.000 | 307.9 |
| C ungated (old) | 3/8 | 4 | 0/4 | 1.5 | 543.9 |
| C gated (new) | **5/8** | **0** | 0 | 2.000 | 645.9 |
| D ungated (old) | 4/8 | 4 | 0/4 | 1.5 | 355.6 |
| D gated (new) | **6/8** | **0** | 0 | 2.000 | 441.0 |

## Reading

- **Correctness bug fixed.** Zero misfires (was 4), zero A-relative
  regressions in C (was TRN-03/TRN-04 broken). The gate does exactly
  what it claims.
- **D flips TRN-08 with zero fast-path hits** — the concise text
  block alone replicates the B-family flip a third time (B, B-rerun,
  gated-D). Memory-caused, not luck.
- **But #2/#3 still FAIL — and now conclusively.** Gated C/D match
  A's calls (2.000) and exceed A's tokens (645.9/441.0 vs 307.9).
  The reason is structural: held-out inputs are novel BY DESIGN, so
  an input-gated fast-path fires ~never (0/32 checks here), while
  the assisted calls still pay full injection. A fast-path can only
  save calls on repeated inputs — i.e. on exposure-style streams,
  not on held-out transfer. The gate doc's "only remaining route"
  is now closed for transfer tasks: flipping #2/#3 needs repeated
  inputs (a streaming/time-series design), not a better reuse rule.

Cost: 48 calls, **$0.01191**. New-experiment spend this segment:
$0.0012 (recheck) + $0.0167 (B-rerun) + $0.0129 (diet) + $0.0119
(gated) ≈ **$0.043**. Session total ≈ $0.27 of the $50 cap.
