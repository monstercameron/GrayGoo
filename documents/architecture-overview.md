# Architecture Overview

Condensed from plan.md §§5–7, 10, 19–28. See the spec for full detail.

## Pipeline

```text
GOAL → INTENT NORMALIZER → CAPABILITY SEARCH ─┬─ capability exists → deterministic path
                                              └─ capability missing → CONTEXT COMPILER
                                                  → CEREBRAS/QWEN → candidate Lisp AST
                                                  → MUTATION CLASSIFIER → REHEARSAL ORCHESTRATOR
                                                  → parallel workers (regression / fuzzing / differential / …)
                                                  → TRUSTED EVALUATOR → PASS → registry → transfer gate
                                                  → PROMOTION AUTHORITY → VERSIONED DISPATCH → LIVE RUNTIME
```

## Trust domains (plan.md §6)

| Domain | Name | Contents | Mutable by agent? |
|---|---|---|---|
| A | Trusted Kernel | Dispatch, workers, permissions, ledger, promotion, credentials, rollback | No — external release only |
| B | Active Application | Promoted capabilities, business logic, learned abstractions | Yes — via promotion |
| C | Speculative Workers | Disposable SBCL processes running candidates | N/A — nothing canonical |
| D | Evaluator | Hidden tests, property generators, thresholds, transfer eval | No — no write access |

The agent must not modify: promotion authority, evaluator, permission broker,
resource governor, worker isolation, event ledger, credential broker, hidden
benchmark store (§4.3).

## Mutation risk levels (plan.md §7)

| Level | Scope | Examples | Key extra gates |
|---|---|---|---|
| R0 | Pure function | Parsing, transform, ranking | Unit + property + perf |
| R1 | Local mutable state | Cache, session | State-diff, replay |
| R2 | Shared canonical state | Records, queues | Forked state, invariants |
| R3 | Schema/API contract | Layout, serialization | Migration, compat, coexistence |
| R4 | External effects | Network, email, payment, fs | Virtualization, dry-run, approval |
| R5 | Agent policy | Retrieval, prompting, context | Offline benchmark, control group, hidden suite |
| R6 | Kernel/evaluator | — | Prohibited in-process |

## Capability lifecycle (plan.md §§10, 31)

```text
PROPOSED → EPHEMERAL (task TTL) → PATCH (1–7d TTL) → SKILL (transfer-proven)
→ PROCEDURAL FAMILY (generalized) → STABLE (indefinite) → DEPRECATED → RETIRED
```

Forgetting is default: unproven capabilities expire without real usage or
transfer evidence.

## Key mechanisms

- **Generation fingerprints (§19):** every worker pins app/schema/capability/
  runtime hashes; stale candidates must rebase and re-run.
- **Epoch-based dispatch (§§26–27):** requests pin a dispatch epoch; old
  versions drain before unloading; rollback moves a pointer.
- **Autonomy gears (§17):** G0 no-model → G1 one-shot → G2 one repair →
  G3 exploratory → G4 recursive (budgeted). Prefer the lowest gear.
- **Green-stop (§18):** the harness, not the model, declares success and halts.
- **Effect virtualization (§23):** rehearsal gets forked DB, overlay fs,
  stubbed network, sinked email, simulated payments, virtual clock.
- **Conditions/restarts (§34):** structured CL feedback (e.g. `rate-limited`
  with `retry-after` / `reduce-batch` restarts) instead of full regeneration.

## Advisory learning memory (memory.md)

A second learning loop beside capability memory: lessons learn *how to
develop*, capabilities learn *what to run*.

```text
EVENT LEDGER → EXPERIENCE MINER → LESSON SYNTHESIZER → candidate lesson
→ REPLAY EVALUATOR → reject / promote → LESSON REGISTRY → retrieval
→ CONTEXT COMPILER → QWEN
```

New packages: `evo.lesson lesson.mine lesson.registry lesson.retrieve
lesson.replay lesson.consolidate lesson.playbook lesson.metrics`. Full
digest in [learning-memory.md](learning-memory.md).

## Module map (plan.md §§73–74)

Packages: `evo.kernel dispatch capability contract intent registry events
world context model cerebras worker rehearsal effects state schema
eval-client promotion transfer memory consolidation policy metrics security
recovery`. Evaluator lives outside the runtime (`evaluator/`), benchmarks
under `benchmarks/`, storage is Postgres (+SQLite for dev).
