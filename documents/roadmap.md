# Roadmap

From plan.md §§78–79, 91–92 and todos.md.

## Development phases (plan.md §78)

| Phase | Focus | Acceptance |
|---|---|---|
| 0 | Runtime spike: SBCL + Cerebras + child-process eval | Model synthesizes a function, child compiles and runs it |
| 1 | Capability registry | Versions coexist, dispatch swaps safely, rollback works |
| 2 | Rehearsal manager | Bad candidates can't touch runtime; stale rejected; crashes contained |
| 3 | Trusted evaluator | Agent can't inspect hidden suite; promotion needs verdict |
| 4 | Effect isolation | Failed candidates leave canonical state unchanged |
| 5 | Procedural memory | Learned skill solves a later task without resynthesis |
| 6 | Transfer promotion | One-off repairs don't auto-persist; TTL enforced |
| 7 | Consolidation | Redundant capabilities retired without benchmark loss |
| 8 | Policy evolution | Verified policy mutation without touching trust kernel |
| 9 | Shadow production | Real-traffic shadow runs with no output/state impact |

## Learning-layer phases (memory.md §35)

Bolt on only after event logging and basic mutation work:

| Phase | Focus | Output |
|---|---|---|
| L1 | Capture | Trajectories, failure taxonomy, repair outcomes |
| L2 | Manual lessons | Schema, 10–20 seeded lessons, retrieval, measured effect |
| L3 | Automatic mining | Clustered failures → evidenced candidate lessons |
| L4 | Replay validation | A/B replay; harmful rejected, useful promoted |
| L5 | Consolidation | Merged principles, task-family playbooks |
| L6 | Adaptive development | Learned context, test order, repair routing, escalation |

## MVP scope (plan.md §79)

In: pure/local-state capabilities, Cerebras generation, isolated SBCL
workers, versioned registry, contracts, unit/property tests, rehearsal,
promotion, rollback, procedural-memory reuse, basic transfer measurement.

Out: schema self-migration, real external writes, payment/email effects,
macro self-generation, recursive agents, policy self-modification.

## Minimum convincing experiment (todos.md)

```text
SBCL + Cerebras/Qwen + isolated workers + versioned capabilities
+ benchmark family + patch persistence + transfer testing + no-memory baseline
```

First end-to-end target (§92): novel goal → no capability → Qwen candidate
→ worker compile → test fail → minimal counterexample → repair → pass →
temporary patch → later related task reuses it → transfer succeeds → patch
becomes skill → equivalent tasks run with zero model calls.

## Current status

- Done: Cerebras API access verified live via Python + OpenAI SDK
  (`cerebras_client.py`, model `qwen-3.8-27b`, `.venv` w/ `openai`,
  `python-dotenv`). Covers latency + token accounting.
- Next candidates: structured S-expression output mode + malformed-output
  rejection (rest of the Cerebras todos), then benchmark Family A, since
  baselines unblock all learning measurement.

## Post-v1 (gated on core experiment succeeding)

Shadow/canary, schema evolution, macro synthesis, recursive inference,
policy self-improvement, A/B meta-learning, distributed rehearsal.
