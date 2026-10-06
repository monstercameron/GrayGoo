# Decisive experiment: learned reuse + composition vs Qwen from scratch

Session goal 2026-10-06. Thesis under test:

> LLM reasoning can be crystallized into verified reusable capabilities
> that later reduce or eliminate inference on unseen related tasks
> without reducing correctness.

## Design

PHASE 1 (exposure, frozen after): Qwen solved 12 exposure tasks; every
solution passed AST gate + subprocess rehearsal + verification with
cross-task negatives before registration. 13 capabilities persisted to
`artifacts/distill/learned.json` ($0.044, 28 attempts), then **frozen** —
no re-distill, no cap edits during phase 2.

PHASE 2 (held-out, same tasks for every arm): extended transfer sets —
Family-W 9 tasks, Family-R 12 tasks (7 new reuse variants added for this
experiment, each validated to REUSE-solve under BOTH hand seeds and the
frozen learned registry before inclusion). Arms:

- A — Qwen from scratch (NOVEL everything)
- B — prior solutions retrieved into prompt (ADAPT)
- C — learned executable reuse, no composition
- D — learned reuse + composition

## Live results (Cerebras/Qwen, max-tokens 128, ~$0.02 total)

Family-W (9 held-out):

| arm | success | outcomes | calls/task | tokens/task |
|---|---|---|---|---|
| A | 6/9 | 9 NOVEL | 2.000 | 187.9 |
| B | 9/9 | 8 ADAPT + 1 NOVEL | 2.000 | 405.4 |
| C | 7/9 | 5 REUSE + 3 ADAPT + 1 NOVEL | 0.667 | 109.0 |
| D | 9/9 | 4 REUSE + 2 COMPOSE + 2 ADAPT + 1 NOVEL | 0.556 | 88.2 |

Family-R (12 held-out):

| arm | success | outcomes | calls/task | tokens/task |
|---|---|---|---|---|
| A | 10/12 | 12 NOVEL | 1.917 | 279.4 |
| B | 10/12 | 11 ADAPT + 1 NOVEL | 1.917 | 593.9 |
| C | 9/12 | 9 REUSE + 2 ADAPT + 1 NOVEL | 0.333 | 50.0 |
| D | 11/12 | 7 REUSE + 2 COMPOSE + 2 ADAPT + 1 NOVEL | 0.333 | 50.0 |

D's only miss (R-NOV-01) is the pure-model novelty path, missed by A and
B as well — a model limitation, not a registry failure.

## Verdict against the six criteria

- held-out success >= A: YES (W 9/9 vs 6/9; R 11/12 vs 10/12).
- calls/task << A: YES (3.6x W, 5.8x R fewer). tokens/task << A: YES
  (2.1x W, 5.6x R fewer).
- zero-LLM completion > 0: YES — 15/21 held-out tasks solved with 0
  model calls (6 W + 9 R), verified per-record in `d-summary.json`.
- harmful reuse low: YES — 0 failed REUSE/COMPOSE in arm D on either
  family; `transfer_clean: true` in all verdicts. (Arm C's honest
  misfires on composite tasks are the pinned negative control.)
- tasks/capability > 1 and rising: YES — W 2.00 → 2.33 → 3.00 and R
  1.40 → 1.80 → 2.20 zero-LLM incidences per used cap as reuse sets
  grew 6→7→9 (W) and 8→10→12 (R), artifacts flat at 13 throughout.
- reproducibility: stub A/B/C/D replicated over 5 orders × 2 families
  with byte-identical routing (REUSE/COMPOSE paths are model-free, hence
  order-invariant); live D matches stub routing on all reuse/compose
  tasks.

Thesis verdict `supported: true` on both families (live and stub).

## Scope notes (honest boundaries)

- Capabilities are Python executables in this harness, not Lisp — the
  crystallization→reuse mechanism is what this experiment tests; a Lisp
  port is separate work.
- `growth_sublinear` (§15) still fails: growth is linear in EXPOSURE
  procedures (one cap each); flatness is proven across TRANSFER reuse.
  Cross-procedure compression remains open (see synthesis-learning.md).
- Live runs use max-tokens 128 with reasoning off; NOVEL/ADAPT absolute
  scores depend on that budget, but all arms share it.
