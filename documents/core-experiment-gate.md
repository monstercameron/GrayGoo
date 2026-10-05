# Core-experiment gate evaluation (2026-10-05)

**Question.** Has the core experiment succeeded — i.e. may the
`todos.md` "Only after the core experiment succeeds" stretch section
(shadow, canary, schema evolution, macro synthesis, recursive
inference, policy self-improvement, meta-learning, distributed
rehearsal) open?

**Answer: NO — the gate stays closed.** Re-evaluated below against
`documents/learning-assessment.md` (§b verdicts) plus every scrap of
new evidence since: TTVM measurement, lesson-key pilot-2,
matched-rerun, adversarial re-run 4, rollback mechanism proof,
Baseline A/BCD re-proofs.

## Criterion-by-criterion

| # | Criterion | Old | New | Evidence |
|---|---|---|---|---|
| 1 | Held-out success stable/up | PASS (fragile) | PASS (replicated 1-task edge) | B/D 6/8 vs A 5/8; TRN-08 flip replicates 2/2 B-runs and survives the 256-token A re-check (`documents/b-rerun.md`). Still one task — needs more transfer tasks to fully settle. |
| 2 | Calls/task decreases | FAIL | FAIL (unchanged) | Flat 2.057 by protocol design; no protocol change since. |
| 3 | Tokens/task decreases | FAIL | FAIL (diet tried) | Diet-B (top-1/120ch): 342.7, still +29% over bar, and regressed ADV-05 (`documents/b-diet.md`). Starving retrieval cannot reach 264.7; needs input-matched fast-path. |
| 4 | Reuse increases | UNMEASURABLE | UNMEASURABLE (darker) | Still no time series — and matched-rerun adds negative evidence: fast-path reuse 0/4 on input-matched cases (`documents/matched-rerun.md`). Reuse exists; reuse *growth* is unproven and the reuse that exists doesn't transfer. |
| 5 | Negative transfer below threshold | PASS | PASS (unchanged) | 0 A-relative regressions; re-proof confirms fail sets bit-stable. |
| 6 | Sublinear growth | UNMEASURABLE | UNMEASURABLE (unchanged) | One growth interval; store-everything. No consolidation-on experiment run. |
| 7 | Zero corruption | UNMEASURABLE | UNMEASURABLE (nearer) | Adversarial re-run 4: 4 SAFE / 0 VULN / 3 INCONCLUSIVE with a now-honest memory bomb — but v1 bar is 1000 adversarial executions and fs/net/evaluator remain INCONCLUSIVE (no OS enforcement). Mechanism evidence up, bar still far. |
| 8 | Rollback works | UNMEASURABLE | **PASS (mechanism)** | `set_current_version` now proven: back-only, atomic, record-preserving, refusal modes, ledger event → `postmortem_trigger`, outage-audible (`tests/test_promotion.py`, 4 rollback tests). Caveat: tempfile drills, not production break-then-restore; production drills still future work. |

Score: **3 PASS (1 fragile, 1 mechanism-only) / 2 FAIL / 3 UNMEASURABLE.**

## What changed since the assessment

- **TTVM is no longer TBD.** `documents/ttvm.md`: 8/10 verified,
  median 655 ms, per-stage breakdown (model 53%, worker 47%).
  Assessment §(a)-TTVM "TBD (n=0)" is superseded.
- **Baselines re-proven.** `tests/verify_baseline_a.py`: 68/68 claims
  match. New `tests/verify_baselines_bcd.py`: all B/C/D headline
  claims match, incl. 18/18 Phase-1 exposure output-token identity
  with A. The numbers the verdicts rest on are solid.
- **Assessment §c flip conditions still stand** for #2/#3/#4/#6/#7;
  #8's flip condition is now half-met (offline drills at 100%; needs
  production break-restore drills for full credit).

## Gate decision

The gate requires the learning criteria to move, and they have not:
calls/task flat, tokens/task up, reuse growth unproven with fresh
negative evidence, growth curve unknown. Mechanism work (rollback,
sandbox hardening, TTVM, provenance, CI truth) all landed, but
mechanism is not learning. **Stretch section stays closed.**

Next experiments that could open it, cheapest first (all within the
remaining ~$49 budget):

1. ~~TRN-08 256-token re-check~~ DONE (`documents/b-rerun.md`): A
   still fails TRN-08 at 256 (check-1 `[]` is competence, not
   budget), and a full B rerun replicates the flip exactly (30/35,
   identical fail sets, 35/35 stable verdicts/tokens). Criterion #1
   firms from "fragile" to replicated-single-task PASS. Cost $0.018.
2. ~~Retrieval-diet rerun~~ DONE (`documents/b-diet.md`): 342.7
   still over bar with an ADV-05 regression — diet alone cannot flip
   #3. Only remaining route: input-matched fast-path (fewer calls
   net of injection).
3. Input-matched reuse fix + matched-rerun re-run — prerequisite for
   any #4 movement.
4. Multi-timepoint reuse/growth series with consolidation on —
   measures #4/#6 instead of asserting them.
