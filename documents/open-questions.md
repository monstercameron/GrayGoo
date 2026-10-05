# Open Questions

Decisions to resolve before or during implementation. Research questions
marked (R) come from plan.md §89.

## Resolved

- **Model identity:** `qwen-3.8-27b` confirmed real on Cerebras and verified
  live (OpenAI-compatible `https://api.cerebras.ai/v1`). Docs note: 27B
  dense multimodal, reasoning defaults to high (`reasoning_effort: none`
  disables), supports structured outputs, tool calling, prompt caching.

## Architecture decisions

- **Orchestration language split.** Spec assumes Common Lisp throughout;
  the API layer is currently Python. Decide: Python orchestration + SBCL
  workers, or Lisp-native with a Lisp HTTP client? Affects repo layout
  (§74) and Phase 0 shape.
- **Sandbox mechanism.** "Linux namespaces/container sandbox" is named but
  not specified; dev machine is Windows. Options: containers, gVisor,
  Firecracker, plain process isolation for MVP. Needed by Phase 2/4.
- **CL ambient-authority containment.** `eval`, `compile`, reader macros,
  MOP, `sb-ext:run-program`, FFI all bypass a declared effect system.
  Specify enforcement: locked packages, disabled reader/eval in workers,
  syscall filtering — static effect scanning alone is insufficient.
- **Dispatch enforcement.** Epoch dispatch works only if all calls route
  through `invoke-capability`. Rule needed against direct `funcall`,
  inlining, and compiler-macro bypass in generated code.
- **SBCL prewarm strategy.** No cheap snapshot story; measure cold vs.
  warm worker cost before committing to an 8–16 worker pool.
- **Rebase semantics.** "Rebase and re-run" across generations is
  undefined. MVP answer may be "discard and re-run" — say so explicitly.
- **Retrieval stack.** Semantic search needs an embedding model + index +
  thresholds; none are named. Retrieval quality gates Gear 0 hit rate.
- **State backend for MVP.** Spec assumes transactional/event-sourced
  state, but schema self-migration is out of MVP. Define what MVP state
  actually is (SQLite + what isolation?).
- **Baseline naming.** plan.md §82 (A–E) vs. todos.md (A–D) disagree.
  Adopt plan.md's scheme.

## Thresholds to validate experimentally

Transfer minimums (3 reuses, 0 severe regressions), TTLs, canary
percentages, autonomy-gear budgets. All defaults are unvalidated.

## Research questions (plan.md §89)

1. What fraction of successful patches become genuinely reusable skills?
2. Do semantic procedural descriptions improve transfer over code alone?
   (Experiment 2)
3. At what library size does retrieval bottleneck?
4. How often should consolidation run?
5. Can a skill estimate its own applicability?
6. Do CL conditions/restarts materially reduce repair tokens?
7. How much does worker snapshotting improve mutation throughput?
8. Which task types yield the strongest reusable abstractions?
9. Can inference become a minority of execution cost?
10. Is meta-level policy self-improvement stable over long horizons?

## Learning-layer questions (memory.md)

- Lesson retrieval budget: is 0–5 lessons per call the right cap, and when
  should overflow synthesize a summary vs. drop?
- Confidence decay rates on model/runtime upgrades: what schedule, and what
  revalidation evidence restores confidence?
- Replay cost: full historical A/B per candidate lesson may dominate
  compute — what sampling policy keeps validation affordable?
- Playbook-to-workflow promotion gate: what stability bar before a lesson
  sequence becomes executable orchestration?

## Missing artifacts

- Formal contract DSL semantics (the `defcapability` sketch is conceptual).
- Adversarial candidate corpus for the 1,000-execution acceptance bar.
- Benchmark Family A task list with exposure/transfer/adversarial splits.
