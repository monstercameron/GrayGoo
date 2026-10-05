# Thesis

## Research hypothesis (plan.md §2)

> A persistent Lisp runtime with executable procedural memory, isolated
> speculative execution, transfer-based promotion, and high-speed LLM
> inference can improve effective application competence over time while
> decreasing model inference required per successful task.

## North-star principle (plan.md §93)

The optimization target is not "how much code can the model generate" but:

> How much model reasoning can the system permanently replace with safe,
> reusable computation?

Expected life-cycle of a mature deployment:

```text
early life:    LLM reasoning dominates
middle life:   LLM synthesis + learned capabilities cooperate
mature domain: capabilities dominate, LLM handles novelty only
```

## What counts as learning

- **Learning requires transfer (§4.4).** A function that fixes one task is a
  patch. It becomes a learned capability only after reuse on related tasks.
- **Fewer, broader abstractions (§2).** A healthy system learns general
  procedural families, not one function per problem. Rising capability count
  alone is memorization, not learning.
- **Primary learning criterion (§67):** held-out competence increases AND
  model work per task decreases AND capability-library growth slows.
- **Model output is never trusted because the model says so (§85.10).**
  Promotion requires independent empirical evidence (tests, hidden eval,
  transfer).

## Desired long-term trends (plan.md §2)

```text
capability reuse             ↑
held-out task success        ↑
transfer success             ↑
LLM calls/task               ↓
tokens/task                  ↓
time-to-solution             ↓
time-to-verified-mutation    ↓
regression rate              ↓
capability duplication       ↓
```

## Core loop

```text
intent → capability lookup → live introspection → candidate synthesis
→ isolated rehearsal → verification → promotion → reuse
```

All generated changes are evaluated outside the active application trust
boundary before becoming canonical (§1, §4.1–4.2).
