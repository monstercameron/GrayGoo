# Advisory Learning Memory

Digest of `memory.md`: a developer-experience memory layered beside
executable capability memory. Capabilities answer "what to run"; lessons
steer "how to reason". A heuristic must never become executable authority.

## What it learns

The mutation loop learns what code works. The learning layer learns what
development strategies work, what failures recur, what fixes succeed, what
approaches waste tokens/time, what abstractions transfer, what tests are
predictive, and what model instructions improve first-pass success.

## Lesson classes (memory.md §1)

- **Failure lessons** — e.g. cursor-pagination replacements must test
  repeated-cursor termination.
- **Repair lessons** — e.g. CSV newline bugs live in field escaping, not
  row serialization.
- **Design lessons** — e.g. prefer composition over new monoliths.
- **Runtime-specific lessons** — e.g. SBCL inlining vs. dispatch cells.
- **Model-behavior lessons** — e.g. Qwen over-refactors past green;
  constrain scope and green-stop.

Store positive and negative lessons (§8); keep facts separate from
heuristics (§20) — facts can be trusted once verified, heuristics need
ongoing evidence.

## Data model (memory.md §3)

Lesson ID, class, statement, applicability (WHEN it applies, WHEN it does
not, WHY), supporting evidence, counterexamples, confidence, measured
impact (first-pass-success / token deltas), creation + last-validated
generation, usage and outcome counts, TTL/status.

## Mining loop (memory.md §§4–6)

Lessons are derived, not dictated:

```text
event ledger → cluster failures/repairs → recurring pattern
→ Qwen proposes concise lesson → replay with lesson injected (A/B)
→ compare against control → promote / reject
```

Replay is mandatory: e.g. replay 50 historical parser mutations with and
without the lesson; retain only on consistent improvement. Never let
"Qwen failed → Qwen explained → explanation becomes memory" accumulate
confident nonsense.

## Retrieval (memory.md §§12–15)

Context compiler injects 0–5 lessons per synthesis call. Score by semantic
similarity × failure-class match × family/runtime match × confidence ×
measured benefit × freshness × negative-transfer history. Retrieve across
layers (L0 universal → L1 Lisp/SBCL → L2 subsystem → L3 family → L4 task),
not just nearest semantic match. More than 5 relevant → synthesize a
temporary summary instead of prompt bureaucracy.

## Lifecycle (memory.md §§16–19)

```text
OBSERVATION → CANDIDATE → REPLAY VALIDATED → ACTIVE → GENERALIZED → PRINCIPLE
```

Counterexamples accumulate → REFINE / DEPRECATE. Confidence decays on
runtime/model upgrades until revalidated. Periodic consolidation merges
duplicates into principles and compiles stable lessons into task-family
playbooks — which can themselves become executable Lisp workflows
(`experience → lesson → playbook → executable workflow`, memory.md §22).

## Failure taxonomy (memory.md §9)

syntax, compile, type/contract, wrong-output, edge-case, state-corruption,
effect-violation, performance, timeout, memory, stale-generation,
over-refactor, negative-transfer, test-overfit, tool-misuse,
context-missing. Lessons attach to classes, not incidents.

## Adaptive development (memory.md §§23–30)

The layer also learns: prompt strategies, context composition (which
elements correlate with success), repair routing (deterministic fix vs
model repair vs escalate), test ordering (fastest/highest-yield first),
risk classification (predicted vs actual), capability applicability
boundaries (retrieved → used → worked/hurt). Every production rollback
triggers a postmortem: extract lesson candidates → replay → strengthen
evaluator.

## Key experiment (memory.md §34)

A no-lesson vs B raw transcripts vs C distilled lessons vs D lessons +
executable memory. Metrics: first-pass success, repair loops, tokens, LLM
calls, time-to-verified-mutation, held-out success. Expectation: D > C > A,
with raw transcripts possibly worse than distilled lessons.

## North-star (memory.md §36)

> Does each month of mutation history make the next mutation require fewer
> attempts, less context, and less inference while preserving or improving
> correctness?

Capability learning reduces how much work the model must perform; lesson
learning makes the remaining work more effective.
