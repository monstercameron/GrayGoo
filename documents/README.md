# Project Documents

Derived planning and research notes for the Self-Evolving Lisp Runtime.
These are working documents, not the specification.

| Document | Contents | Source |
|---|---|---|
| [thesis.md](thesis.md) | Research hypothesis, north-star principle, what counts as learning | plan.md §§1–2, 4, 67, 93 |
| [architecture-overview.md](architecture-overview.md) | Pipeline, trust domains, risk levels, lifecycle, modules | plan.md §§5–7, 10, 26–28, 73–74 |
| [benchmark-plan.md](benchmark-plan.md) | Benchmark families, baselines, key experiments, MVP demo | plan.md §§80–83, todos.md |
| [metrics-and-success-criteria.md](metrics-and-success-criteria.md) | Metrics, learning score, v1 acceptance, experiment success | plan.md §§64–67, 86, todos.md |
| [roadmap.md](roadmap.md) | Phases, MVP scope, first-30 tasks, current status | plan.md §§78, 91–92, todos.md |
| [open-questions.md](open-questions.md) | Unresolved decisions and research questions | plan.md §89, analysis |
| [learning-memory.md](learning-memory.md) | Advisory lesson memory: artifacts, mining loop, lifecycle, key experiment | memory.md |

Source of truth order:

1. `plan.md` — the specification
2. `memory.md` — the learning-layer design
3. `todos.md` — the execution-ordered task list
4. `documents/` — this folder: digests for planning, research, and onboarding

When a spec changes, update the affected digest. Do not let digests drift;
where they disagree with a spec, the spec wins.
