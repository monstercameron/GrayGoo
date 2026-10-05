# Canonical A/B/C/D comparison — executable reuse thesis (2026-10-05)

Session-goal reframing of the core experiment. Prior baselines
(`documents/baselines-bcd.md`) injected memory into prompts in every
arm; the new canonical arms separate prompt retrieval from TRUE
zero-call execution:

| Arm | Memory | Per-check behavior |
|---|---|---|
| A (no memory) | none | model solves from scratch |
| B (prompt retrieval) | top-2 exposure exemplars injected | model solves, augmented |
| C (executable reuse) | 5 verified capabilities | applicable cap executes (0 calls), else model scratch |
| D (executable composition) | 5 capabilities + 2 compositions | composition first, then cap, then model scratch |

Outcomes (`outcomes.py`): REUSE (one cap, 0 calls), COMPOSE (plan,
0 calls), ADAPT (retrieved but model needed), NOVEL (from scratch).

## Why Family R

Family A transfer tasks need fresh procedures by design, so reuse
cannot fire on them (gated fast-path: 0/32). Family R
(`benchmarks/family-r/`, 7 held-out tasks) measures reuse on unseen
INPUTS: 4 same-procedure tasks, 1 applicability trap (the date
capability must abstain on an ISO-week line), 2 composite tasks.

## Offline result (stub, zero live calls)

`uv run python benchmarks/run_abcd.py --adapter stub`
(pinned by `tests/test_run_abcd.py`):

| Arm | Passed | Outcomes | Calls/task | Tokens/task* |
|---|---|---|---|---|
| A | 7/7 | 7 NOVEL | 1.857 | 180.4 |
| B | 7/7 | 7 ADAPT | 1.857 | 349.6 |
| C | 5/7 | 6 REUSE + 1 NOVEL | 0.143 | 12.6 |
| D | 7/7 | 4 REUSE + 2 COMPOSE + 1 NOVEL | 0.143 | 12.6 |

\* Stub tokens are chars/4 estimates; live runs re-measure real usage.

Read: B costs 2x tokens for zero call savings (retrieval without
execution). C reuses 4/4 and abstains correctly on the trap, but both
composites FAIL on the single-capability path (csv-parse returns
un-normalized dates; flatten returns JSON, not CSV) — the honest
misfire that motivates composition. D matches A's 7/7 at 1/13th the
calls by executing 6/7 tasks with zero inference.

## Live result (Cerebras/Qwen, 28 calls, $0.007, same day)

`uv run python benchmarks/run_abcd.py --adapter cerebras --arms abcd
--artifacts artifacts/abcd-live` (artifacts gitignored, per-task JSONs
under `artifacts/abcd-live/`):

| Arm | Passed | Outcomes | Calls/task | Tokens/task | Cost |
|---|---|---|---|---|---|
| A | 6/7 | 7 NOVEL | 1.857 | 291.3 | $0.0023 |
| B | 6/7 | 7 ADAPT | 1.857 | 609.9 | $0.0045 |
| C | 5/7 | 6 REUSE + 1 NOVEL | 0.143 | 19.7 | $0.0001 |
| D | 7/7 | 4 REUSE + 2 COMPOSE + 1 NOVEL | 0.143 | 19.7 | $0.0001 |

D beats A/B on success (7/7 vs 6/7) AND inference (13x fewer calls,
15–31x fewer tokens, 15–30x cheaper). Both live failures are
R-CMP-02 check 1 (null `{}`/`null` CSV serialization): the model
drops empty values from scratch AND with retrieval, while the
verified composition serializes them exactly — execution beats
reasoning on the edge case. The trap passed live in all arms (the
model solved the ISO-week line; C/D abstained and fell back).
C's 5/7 matches the offline prediction exactly (same two misfires).

## What this proves (and does not)

Proves: the reuse/composition mechanics work end-to-end (applicability
gate, composition-first ordering, outcome accounting); the harness can
separate C from D on held-out inputs; and live, D strictly dominates
A/B on both success and inference cost. Does NOT prove transfer on
fresh procedures (Family A transfer still stands: memory moves 1/16).
Seeds are hand-written verified procedures, so this measures reuse
mechanics, not synthesis quality — the next step is letting the
synthesis loop LEARN these capabilities from exposure successes
instead of hand-seeding them.

## Compression warning (live)

D solves 7 tasks with 7 artifacts (5 capabilities + 2 compositions):
tasks-per-capability 1.0, compression warning FIRING. Per the thesis,
this is memorization-shaped. Next: generalize seeds (e.g. one
table-shaping capability family) so the artifact count stays flat as
reuse tasks grow.
