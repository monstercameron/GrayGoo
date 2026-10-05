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
| 2 | Calls/task decreases | FAIL | PASS (repeated streams) / FAIL (held-out) | Stream: 2.000 → 1.000 → 0.722, success held, replicated (`documents/repeat-stream.md`). Held-out: still flat — fast-path fires 0/32 on novel inputs. |
| 3 | Tokens/task decreases | FAIL | PASS (repeated streams) / FAIL (held-out) | Stream: 273.5 → 120.7 → 88.5 vs 264.7 bar (`documents/repeat-stream.md`). Held-out: memory still strictly costs tokens (diet-B 342.7 + ADV-05 regression). |
| 4 | Reuse increases | UNMEASURABLE | PASS (repeated streams) | Hit rate 0% → 50% → 64% across rounds, 35/35 helped (`documents/repeat-stream.md`). Held-out reuse growth still unproven (0/32 fires). |
| 5 | Negative transfer below threshold | PASS | PASS (unchanged) | 0 A-relative regressions; re-proof confirms fail sets bit-stable. |
| 6 | Sublinear growth | UNMEASURABLE | PASS (dedup policy) | Lib 12 → 23 → 34 over cumulative 12 → 36 → 72 checks (concave; naive would be 69). Mechanism = dedup-by-check + reuse, not consolidation (`documents/repeat-stream.md`). |
| 7 | Zero corruption | UNMEASURABLE | UNMEASURABLE (nearer) | Adversarial re-run 4: 4 SAFE / 0 VULN / 3 INCONCLUSIVE with a now-honest memory bomb — but v1 bar is 1000 adversarial executions and fs/net/evaluator remain INCONCLUSIVE (no OS enforcement). Mechanism evidence up, bar still far. |
| 8 | Rollback works | UNMEASURABLE | **PASS (mechanism)** | `set_current_version` now proven: back-only, atomic, record-preserving, refusal modes, ledger event → `postmortem_trigger`, outage-audible (`tests/test_promotion.py`, 4 rollback tests). Caveat: tempfile drills, not production break-then-restore; production drills still future work. |

Score: **7 scoped-PASS / 1 UNMEASURABLE (#7)** — but #2/#3/#4/#6
pass ONLY on repeated-input streams while failing (or moot) on
held-out, so the gate (premised on held-out transfer) stays closed.
See decision below.

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

Update after the repeat-stream experiment: the learning criteria HAVE
moved — on repeated-input streams. Calls/task 2.0 → 0.72, tokens/task
273 → 88, reuse 0% → 64%, growth concave, all replicated, zero
regressions. That is genuine reuse learning with real mechanisms
(input-gated fast-path, seed+outcome confidence, dedup-by-check).

**Stretch section stays closed anyway.** The gate's premise is
held-out TRANSFER — doing NEW tasks better — and there the score is
unchanged: one replicated single-task edge (TRN-08), flat calls,
memory strictly costs tokens, fast-path fires 0/32. Caching repeated
work is not transfer. What would open it: a second transfer edge
(more transfer tasks), or a reuse mechanism that fires on novel
inputs without misfiring (the gated fast-path proves the safety
half; the recall half on novel inputs is the open problem).

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
3. ~~Input-matched reuse fix + matched-rerun re-run~~ DONE
   (`documents/matched-rerun-gated.md`): gate blocks all misfires (C
   back to 5/8, D flips TRN-08 with zero hits) — but fires 0/32 on
   novel inputs, so the fast-path route to #2/#3 is structurally
   closed on held-out tasks. Needs repeated-input streams instead.
4. ~~Multi-timepoint reuse/growth series~~ DONE
   (`documents/repeat-stream.md`): reuse 0%→50%→64%, growth concave
   via dedup — AND consolidation-on now measured too (churn stream:
   12 retired, lib 12→11 under churn, 11/11 hits help). #6 holds via
   both mechanisms.
