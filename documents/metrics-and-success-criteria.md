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
- [ ] Average model calls/task decreases
- [ ] Average tokens/task decreases
- [ ] Capability reuse increases
- [ ] Negative transfer stays below threshold
- [ ] Capability growth becomes sublinear
- [ ] Zero canonical-state corruption
- [ ] Rollback works reliably

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
