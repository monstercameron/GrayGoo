# Repeat-stream experiment: reuse learning over time (2026-10-05)

The experiment the gate doc called for. Held-out transfer is novel by
design (the gated fast-path fires 0/32 there), so reuse-over-time
(#4), growth shape (#6), and calls/tokens per task (#2/#3) can only
move on a stream with repeated inputs. This runs one: Family-A
exposure tasks in expanding cumulative rounds (6 → 12 → 18 tasks)
through the input-gated patch fast-path.

Driver: `experiments/repeat_stream.py` (offline-tested in
`tests/test_repeat_stream.py`). Round 1 solves from scratch and
stores verified check exemplars (dedup by task+check; seed confidence
helped=1 for passing its own check); later rounds repeat earlier
tasks. Every fast-path hit records its outcome (helped/hurt), so a
wrong recorded output self-revokes. Assisted misses solve from
scratch (empty injection) — misses cost exactly Baseline-A-like
calls. Standard conditions: stripped scoring, temp 0.0, reasoning
off, 128 tokens.

Artifacts: `artifacts/repeat-stream/` (final run),
`artifacts/repeat-stream-rerun1/` (bit-identical replication),
`artifacts/repeat-stream-prefix/` (pre-fix run: ranking bug + zeroed
usage — superseded, kept for the record).

## Result: all four time-series criteria flip (repeated-stream scope)

| Round | Tasks | Passed | Fails | Hits / helped | Lib size | Calls/task | Tokens/task |
|---|---|---|---|---|---|---|---|
| 1 | 6 | 6/6 | — | 0 / 0 | 12 | 2.000 | 273.5 |
| 2 | 12 | 11/12 | EXP-08 | 12 / 12 | 23 | 1.000 | 120.7 |
| 3 | 18 | 16/18 | EXP-08, EXP-16 | 23 / 23 | 34 | 0.722 | 88.5 |

- **Fails are exactly the known-hard tasks** (EXP-08/EXP-16 fail in
  A/B/C/D too). Zero repeat regressions: every task that passed in
  an earlier round passes again; 35/35 hits helped across rounds 2–3.
- **#2 calls/task: 2.000 → 1.000 → 0.722**, success held → PASS
  (within-setting comparison; the 2.057 bar is the full-35 mean,
  round-1 stream baseline is 2.000 — same conclusion either way).
- **#3 tokens/task: 273.5 → 120.7 → 88.5** vs the 264.7 bar → PASS.
- **#4 reuse increases: hit rate 0% → 50% → 64% of checks**, rising
  across runs → PASS (first real reuse time series).
- **#6 sublinear growth: lib 12 → 23 → 34 over cumulative 12 → 36 →
  72 checks** (slopes 0.92 → 0.61 per check, concave) → PASS, with
  the mechanism named: dedup-by-check + reuse. Without dedup the
  naive store count would be 12+23+34 = 69 (linear); the policy is
  the curve.
- **Replication**: the final run reproduces rerun1 bit-identically
  (same pass/fails/hits, same costs to the dollar-6th-decimal).
  Temp-0.0 is deterministic here, as in the B rerun.

## Two bugs found and fixed along the way

1. `repeat_stream.py` read `summary["totals"]`; `runner.run` returns
   `"usage"` — round-1 usage printed 0.000 (prefix run).
2. Retrieval ranking buried same-task patches: category ties break
   newest-first, so `find_fast_path`'s top-2 rarely held the repeat
   (prefix round 2 fired 4 hits instead of ~12).
   `find_fast_path` now considers same-task patches FIRST
   (input+confidence gates unchanged — strictly higher precision).
   Pinned by 2 new tests (6 total in `test_patch_memory.py`).

## Scope honesty (read before citing)

These flips hold **on repeated-input streams**, where reuse is
possible by construction. They do NOT transfer to held-out tasks:
there the gated fast-path fires 0/32 and #2/#3 still FAIL. Caching
repeated work is real learning-system behavior (and the self-revoking
confidence + dedup policy are genuine mechanisms), but it is not
transfer. The core-experiment gate — premised on held-out transfer —
stays closed; see `documents/core-experiment-gate.md`.

Cost: prefix run 62 assisted calls ≈ $0.0087 (usage zeroed by the
key bug; estimated at the measured $0.0001405/call) + 2 × $0.005198
(37 calls each) ≈ **$0.019**. New-experiment spend this segment ≈
**$0.062**; session total ≈ **$0.29 of the $50 cap**.
