# Metrics and Success Criteria

From plan.md §§64–67, 86 and todos.md.

## Essential metrics (plan.md §65)

Task: success rate, held-out success, latency, model calls/task,
tokens/task, cost/task.

Learning: capability reuse, skill transfer success, negative transfer,
new/retired skills per week, procedural-family growth.

Reliability: rollback rate, production failure rate, state-corruption
incidents, rehearsal/live divergence, stale-candidate rejections.

Complexity: capability count, semantic duplication, call-graph depth,
unused-capability ratio, entropy score.

### Entropy score, formally (issues.md #85)

`metrics.entropy_from_counts(counts)` is Shannon entropy in **nats**:
`-Σ (c/total)·ln(c/total)` over count categories (empty → TBD, never
0). Two instantiations, both keyed by the *reusable procedure* so
they are comparable across memory kinds:

- `text_reuse_entropy(tasks)`: keys are source task ids from
  Phase-2 `text_sources` (both retrieved checks of one task count
  toward that task).
- `patch_reuse_entropy_from_tasks(tasks)` /
  `reuse_entropy_from_ledger(rows)`: keys are patch ids.

Worked example: reuse counts [6, 2] over two procedures → total 8 →
`-(0.75·ln0.75 + 0.25·ln0.25)` ≈ 0.562 nats. Uniform reuse maximizes
the score (ln K for K procedures); single-procedure reuse scores 0.
Interpretation rule: entropy measures reuse *spread*, not reuse
*quality* — always report alongside helped/hurt rates (#82), never
alone. Semantic duplication, graph depth, and unused-capability
ratio remain separate metrics (not folded into one number).

## Core learning score (plan.md §66)

```text
             held-out success × transfer success
utility = -------------------------------------------
          cost × latency × regression penalty
```

Diagnostic only — must not directly control promotion without individual
safety gates.

## Experiment success (todos.md: "Define experiment success")

- [ ] Held-out success increases or remains stable
- [x] Average model calls/task decreases — PASS on repeated-input
  streams (2.000 → 1.000 → 0.722, success held, replicated;
  `documents/repeat-stream.md`); still flat on held-out.
- [x] Average tokens/task decreases — PASS on repeated-input streams
  (273.5 → 120.7 → 88.5 vs 264.7 bar); held-out still +65–135%
  (diet-B cannot reach the bar either).
- [x] Capability reuse increases — PASS: fast-path hit rate 0% → 50%
  → 64% across stream rounds, 35/35 helped, zero regressions.
- [ ] Negative transfer stays below threshold
- [x] Capability growth becomes sublinear — PASS: library 12 → 23
  → 34 over cumulative 12 → 36 → 72 stream checks (concave via
  dedup-by-check + reuse; naive store-everything would be 69).
- [ ] Zero canonical-state corruption
- [x] Rollback works reliably — MECHANISM PROVEN (2026-10-05):
  `promotion.set_current_version` moves back-only over atomic writes,
  preserves version records, refuses forward/no-op/unknown moves, and
  (new) appends a `candidate rolled back` ledger event feeding
  `adaptive.postmortem_trigger`; ledger outage keeps the move with an
  audible receipt note (QA-06 pattern). Tests:
  `tests/test_promotion.py` rollback section (4 tests incl.
  end-to-end postmortem trigger + outage injection).

## v1 acceptance (plan.md §86)

Safety: 1,000 adversarial candidate executions with zero canonical-state
corruption; worker crashes never crash the coordinator; runaway
CPU/memory candidates terminated; kernel/evaluator mutation rejected.

Reliability: deterministic rollback; stale-generation promotions rejected;
recovery from stable manifest works.

Learning: across benchmark families, reuse rises and calls/tokens per task
fall over repeated tasks; held-out success does not regress; negative
transfer stays below threshold.

Performance: pure-candidate rehearsal fast enough for interactive use;
warm workers substantially faster than cold; verification parallelized.

## Learning-layer metrics (memory.md §33)

Lesson retrieval/usage counts, acceptance rate, first-pass-success /
repair-count / token / latency deltas, lesson negative-transfer,
precision, redundancy, lifespan. Headline metric:

```text
development acceleration
=
baseline time-to-verified-mutation
/
lesson-assisted time-to-verified-mutation
```

## Transfer gate defaults (plan.md §30)

Minimum independent reuse: 3. Severe negative transfer: 0. Held-out
success ≥ baseline. Resource regression within budget. Thresholds are
configurable and unvalidated — treat tuning as an experiment, not a given.
