Here’s the issue list, prioritized.

1. **Unsafe reader eval in speculative workers**
   - Worker `read` path does not bind `*read-eval*` to `nil`.
   - A candidate can potentially use `#.` reader evaluation before normal validation.
   - Fix first.

2. **Worker isolation is process-only, not authority-safe**
   - Fresh SBCL processes still inherit OS-level privileges.
   - Generated code may access filesystem, network, subprocesses, environment variables, or exit the process.
   - Needs real sandboxing/allowlisted effects.

3. **“Prewarmed worker pool” is not actually persistent**
   - Current pool still launches fresh SBCL processes per job.
   - This will dominate latency and undermine the high-speed REPL thesis.
   - Needs long-lived worker processes with bounded recycle/reset.

4. **Generation fingerprint is too weak**
   - Current fingerprint does not fully identify the loaded runtime/source tree.
   - Source can change without changing the fingerprint.
   - Include source-tree hash, dependencies, capability manifest, schema generation, protocol version, etc.

5. **`rehearse` orchestration is still a stub**
   - Core end-to-end mutation validation pipeline is not implemented.
   - This is the main functional gap.

6. **Risk classification is effectively unimplemented**
   - Current classifier returns the same high-risk classification broadly.
   - Needs real static/semantic classification across R0–R6.

7. **Trusted evaluator is still a stub**
   - Promotion is not yet gated by a separate authoritative evaluator.
   - Hidden tests, metamorphic tests, transfer gates, and evaluator isolation are missing.

8. **Promotion authority is still a stub**
   - There is no complete evidence-based promotion path yet.
   - Candidate → evidence → eligibility → version registration → epoch publish needs wiring.

9. **Context compiler is mostly packaging, not compilation**
   - It does not yet perform strong retrieval, ranking, token budgeting, relevance filtering, or compression.
   - Current context logic is too shallow for the intended architecture.

10. **Capability retrieval is unimplemented/incomplete**
    - `find-skills` / retrieval path currently does little or nothing useful.
    - Existing capabilities cannot yet reliably substitute for model calls.

11. **Learning loop is not operational**
    - Patch reuse, transfer validation, procedural families, lesson mining, and negative-transfer handling are not end-to-end.

12. **Event ledger is in-memory**
    - Experimental history disappears on crash/restart.
    - For a research system, mutation history needs durable append-only persistence immediately.

13. **TODO/status tracking overstates completion**
    - `todos.md` marks subsystems complete when many are interface-level or stub-level.
    - This creates false confidence and bad agent coordination.

14. **Roadmap/status documents disagree**
    - README/roadmap/todos/source are not aligned.
    - Need one generated or test-backed source of truth.

15. **Capability identity is too tightly coupled to Lisp symbols/packages**
    - Persistent IDs based on symbols can drift across package moves/loads.
    - Prefer stable string/UUID identity plus symbolic display/binding name.

16. **Dispatch globals need concurrency protection**
    - Epochs, pins, version tables, and promotion state can race under concurrent mutation.
    - Serialize control-plane updates or add explicit synchronization.

17. **Epoch semantics need broader integration testing**
    - The dispatch code looks good, but mixed-version workflows need explicit tests.
    - Especially nested capability calls across request epochs.

18. **Schema/version state rollback is not yet solved**
    - Code rollback is easier than data/schema rollback.
    - Schema evolution should remain disabled until versioned migrations and compatibility checks exist.

19. **Effect isolation is not implemented**
    - Filesystem, DB, queue, email, payments, network writes, clock, randomness need mediated effect objects.
    - Without that, failed candidates can still have real-world consequences.

20. **No strong distinction yet between compile safety and semantic safety**
    - “Compiles + tests pass” is not enough.
    - Need hidden cases, differential checks, invariants, resource limits, and effect validation.

21. **No adversarial evaluator-gaming defense yet**
    - Static tests can be overfit.
    - Need fresh generated cases and metamorphic transformations.

22. **Model can still become too involved in its own evaluation**
    - Same-model implementation/test/explanation loops risk self-confirmation.
    - Deterministic and external oracles should dominate acceptance.

23. **No durable experiment baseline pipeline**
    - Need repeatable A/B runs:
      - no memory
      - textual lessons
      - executable memory
      - dual memory

24. **No clear benchmark freeze**
    - The project still risks evolving faster than the experiment.
    - Freeze one benchmark family and run it consistently before expanding scope.

25. **No production-grade secret handling**
    - `.env` tracking/history needs careful handling.
    - Ensure credentials never enter generated worker context or Git history.

26. **No candidate grammar enforcement beyond raw Lisp forms**
    - Long-term, model outputs should use constrained candidate S-expressions rather than unrestricted top-level forms.
    - This reduces attack surface and parsing ambiguity.

27. **No robust worker reset protocol**
    - Persistent workers will need namespace/state reset or deterministic recycle thresholds.
    - Otherwise contamination accumulates.

28. **No worker health model**
    - Need health checks, poison detection, recycle-on-anomaly, and worker-generation ownership.

29. **No mutation storm / oscillation controls wired in**
    - Repeated rewrite loops can burn tokens and destabilize behavior.
    - Add target freeze, cycle detection, and escalation.

30. **No capability consolidation yet**
    - Without deduplication/TTL/generalization, the library will become noisy and retrieval quality will degrade.

31. **No lesson validation loop yet**
    - “Lessons learned” need replay/A-B validation before they steer future model behavior.

32. **No confidence decay / stale-knowledge handling**
    - Lessons and capabilities can become invalid as model/runtime/dependencies change.

33. **No test-yield learning**
    - The system does not yet learn which tests catch meaningful regressions fastest.
    - This matters for time-to-verified-mutation.

34. **No real time-to-verified-mutation dashboard**
    - You need breakdowns for:
      - context build
      - model latency
      - worker startup
      - compile
      - tests
      - evaluator
      - promotion

35. **Python/Lisp boundary may become accidental architectural debt**
    - Python is fine for bootstrap, but if too much orchestration stays there, GrayGoo becomes a Python agent system with Lisp attached rather than a Lisp-native live system.

36. **No explicit “one vertical slice” integration test**
    - You need a single acceptance test proving:
      - task
      - Qwen generation
      - candidate parse
      - isolated execution
      - failure
      - repair
      - pass
      - temporary promotion
      - reuse

37. **No shadow execution path**
    - Higher-risk candidates should be able to run against real request distributions without affecting canonical outputs/effects.

38. **No canary promotion path**
    - Future live promotion needs staged exposure and automatic rollback thresholds.

39. **No capability applicability calibration**
    - Skills may be reused outside their valid domain.
    - Retrieval needs positive and negative applicability evidence.

40. **Scope creep risk**
    - The architecture is already rich enough.
    - The main project risk now is continuing to add subsystems before proving the core learning experiment.

If you want, I can turn these into a **GitHub issue backlog with severity, labels, dependencies, and acceptance criteria** next.