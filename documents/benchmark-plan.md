# Benchmark Plan

From plan.md §§80–83 and todos.md.

## Benchmark families (plan.md §81)

| Family | Task types |
|---|---|
| A — parsing | Dates, CSV, nested records, logs |
| B — API workflows | Pagination, retry, normalization, caching |
| C — planning | Tool composition, conditional workflows |
| D — state transformation | Inventory, accounts, simple workflows |
| E — fault recovery | Timeouts, bad inputs, partial failures |

Each family contains: training/exposure tasks, unseen transfer tasks,
adversarial transforms, long-term replay tasks. Start with Family A
(parsing/data transformation), 20–50 related tasks (todos.md).

## Required splits

- **Exposure tasks:** the system may learn from these.
- **Held-out transfer tasks:** unseen tasks from the same family; the primary
  evidence of learning vs. memorization.
- **Adversarial variants:** boundary, malformed, empty, large, reordered
  inputs (plan.md §21F).
- **Equivalent transformations:** rename identifiers, permute ordering,
  rescale values, inject irrelevant fields — detects evaluator gaming
  (plan.md §37).

## Baselines

> Naming mismatch to reconcile: plan.md §82 defines five baselines (A–E)
> while todos.md defines four (A–D). Recommend adopting plan.md's scheme.

| Baseline (plan.md §82) | Configuration |
|---|---|
| A | Qwen, no persistent learning |
| B | Qwen + textual memory |
| C | Qwen + conventional coding tools |
| D | Qwen + executable capability memory |
| E | Full system with transfer-tested dual memory |

Control groups per experiment (§38): learned system vs. memory-disabled
baseline vs. previous stable generation vs. (optional) semantic-only variant.

## Key experiments (plan.md §83)

1. Does executable memory reduce inference (tokens/calls/latency per task)?
2. Does dual semantic+executable memory transfer better than executable-only?
3. Does consolidation improve retrieval and reduce negative transfer?
4. Does rehearsal prevent state corruption under intentionally malicious
   candidates?
5. Does high-speed inference materially improve time-to-verified-mutation,
   or does validation dominate?
6. Can policy self-improvement beat the frozen base policy on held-out
   families?

## Lesson key experiment (memory.md §34)

Conditions: A no-lesson memory, B raw previous transcripts, C retrieved
distilled lessons, D lessons + executable capability memory. Metrics:
first-pass success, repair loops, tokens, LLM calls,
time-to-verified-mutation, held-out success. Expectation: D > C > A, with
raw transcripts possibly worse than distilled lessons.

## MVP demo (plan.md §80)

Domain: data transformation / synthetic API tasks.

1. Task 1 (novel format): fail → synthesize → rehearse → promote patch.
2. Task 2 (same family): retrieve skill, adapt or reuse directly, fewer
   tokens.
3. Task 3 (related but different): recognize procedural family, generalize.

Target narrative: Task 1 takes N model calls, Task 2 takes ~1, Task 20
takes 0.
