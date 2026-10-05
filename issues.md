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

Yes. The latest code exposes several **new concrete issues** beyond the 40-item list. These are worth adding because they come from actual implementation details rather than architectural speculation.

### New issues to add

41. **Transfer gate does not enforce its documented success criteria — HIGH**
   - `transfer-green-p` currently checks only `reuse-count >= 3` and `severe-regressions == 0`.
   - It completely ignores `success-delta`, `token-delta`, and the documented requirement that held-out performance remain at or above baseline. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/memory/transfer.lisp)
   - A skill could therefore be promoted after three useless reuses.
   - **Fix:** require statistically meaningful task benefit and/or non-regression against the baseline before patch → skill promotion.

42. **Capability persistence filenames can collide across Lisp packages — HIGH**
   - Persistent filenames use only `(symbol-name id)`, e.g. `foo-v3.lisp`. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/capability/registry.lisp)
   - `PACKAGE-A:FOO` and `PACKAGE-B:FOO` therefore map to the same disk file.
   - This compounds the symbol-as-persistent-ID problem already identified.
   - **Fix:** use stable UUID/string IDs, or include an escaped package identity/content hash in the pathname.

43. **“Immutable” capability files can be silently overwritten — HIGH**
   - In-memory registration rejects duplicate versions, but `save-capability` opens existing files with `:if-exists :supersede`. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/capability/registry.lisp)
   - So `(FOO, 7)` is immutable in RAM but mutable on disk.
   - That breaks the audit/lineage guarantee.
   - **Fix:** write-once version files; fail if the version already exists unless byte-for-byte identical.

44. **Capability persistence writes are not atomic — HIGH**
   - Capability files are written directly to their final pathname. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/capability/registry.lisp)
   - A crash or process kill mid-write can leave a corrupted canonical version.
   - **Fix:** write temp file → flush/fsync → hash → atomic rename.

45. **Persisted capability artifacts have no integrity hash — HIGH**
   - The loader trusts whatever plist exists at the expected pathname.
   - There is no content hash/manifest linkage yet. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/capability/registry.lisp)
   - Manual corruption or external modification could silently become canonical after restart.
   - **Fix:** content-address capability versions or store expected SHA-256 hashes in a signed/immutable generation manifest.

46. **Worker subprocess inherits host environment/secrets — CRITICAL**
   - `subprocess.Popen` is not being launched with a sanitized environment in the visible worker implementation. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/workers.py)
   - That means an LLM-generated Lisp candidate may be able to read values such as `CEREBRAS_API_KEY`, PATH credentials, cloud tokens, etc. using OS/environment APIs.
   - This is worse than generic “no sandbox”: it's a direct secret-exfiltration channel.
   - **Fix:** construct an explicit minimal `env={...}` for workers. Never inherit the coordinator environment.

47. **The “restricted environment” is only a boolean flag — CRITICAL**
   - `with-restricted-environment` merely binds `*restricted*` to `T`; `deny-by-default` only matters if code voluntarily calls it. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/kernel/security.lisp)
   - Generated Lisp can call Common Lisp/SBCL filesystem, process, FFI, and other APIs directly.
   - It could also simply rebind/set the flag.
   - **Fix:** do not treat this as security. Enforcement must occur at the OS/process/capability boundary.

48. **Effect declarations are not checked against actual behavior — HIGH**
   - `check-declared-effects` only checks whether supplied keywords are members of a known list. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/kernel/effects.lisp)
   - A candidate can declare:
     ```lisp
     (:effects (:pure))
     ```
     and then access files or network.
   - **Fix:** declared effects are claims; compare them against observed/brokered effects and reject undeclared operations.

49. **`derive-version` cannot reliably clear inherited list fields — MEDIUM**
   - Several fields use `(or new-value old-value)`. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/capability/capability.lisp)
   - Therefore an explicit `NIL` cannot clear:
     - inputs
     - outputs
     - effects
     - dependencies
     - source
     - creator
     - model
   - Intent/contract correctly use `supplied-p`; the others don't.
   - **Fix:** use supplied-p flags consistently.

50. **TTL cannot be removed from a derived capability — MEDIUM**
   - `derive-version` treats `NIL` TTL as “inherit parent TTL.” [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/capability/capability.lisp)
   - So a temporary patch that later becomes stable cannot naturally change:
     ```text
     ttl = 7 days
     ```
     to:
     ```text
     ttl = NIL
     ```
   - **Fix:** add `ttl-given-p`, exactly like intent/contract.

51. **Risk values are not validated — MEDIUM**
   - `make-capability` accepts an arbitrary symbol for `risk`. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/capability/capability.lisp)
   - Nothing prevents:
     ```lisp
     :risk :banana
     ```
     or incorrectly downgraded classifications from reaching persistence.
   - **Fix:** define/validate the R0–R6 enum at capability construction and promotion boundaries.

52. **Lifecycle state and promotion status duplicate the same concept and can disagree — MEDIUM**
   - A capability stores both `state` and `promotion-status`, independently. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/capability/capability.lisp)
   - You can construct contradictory records such as:
     ```text
     state            = :stable
     promotion-status = :proposed
     ```
   - **Fix:** either remove one field or encode a strict invariant/transition state machine.

53. **Parent lineage is not validated — MEDIUM**
   - A derived or loaded capability can claim `parent-version = N` without verifying that N exists or belongs to the same legitimate lineage. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/capability/capability.lisp)
   - Cycles/holes could appear through persistence/import.
   - **Fix:** registry insertion should validate parent existence, monotonic versioning, and acyclic lineage.

54. **Version numbers can branch/collide under concurrent derivation — HIGH**
   - `derive-version` defaults to `parent.version + 1`. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/capability/capability.lisp)
   - Two agents deriving simultaneously from version 12 both create version 13.
   - One later fails registration or, worse, persistence races.
   - **Fix:** registry/promotion authority assigns versions atomically; candidate code should not choose final version numbers.

55. **Persistence path identity and in-memory identity use different equality semantics — MEDIUM**
   - Registry keys are symbol-based pairs while disk identity collapses symbols to lowercase symbol names for filenames. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/capability/registry.lisp)
   - This can create cases where RAM considers capabilities different but storage considers them identical.
   - **Fix:** one canonical stable ID representation everywhere.

56. **Worker protocol may be spoofable through alternate output streams — HIGH**
   - The coordinator identifies results using magic stdout markers. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/workers.py)
   - Normal `*standard-output*` is captured, which is good, but untrusted Lisp can potentially write through other inherited streams such as terminal/process descriptors and attempt to inject its own result markers.
   - **Fix:** use a dedicated IPC pipe/socket/file descriptor unavailable through normal candidate streams, or authenticate/frame responses.

57. **Worker result values are converted to strings, destroying typed semantics — MEDIUM**
   - `run-test-thunk` uses `prin1-to-string`, with depth/length truncation. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/worker/worker.lisp)
   - The Python coordinator therefore gets display text rather than a typed structured result.
   - Two unequal large structures can potentially serialize to the same truncated representation.
   - **Fix:** evaluation assertions should happen inside the worker; external envelopes should report structured test facts/hashes, not infer correctness from printed return values.

58. **Printed results are intentionally non-readable — MEDIUM**
   - `*print-readably*` is bound to `NIL`. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/worker/worker.lisp)
   - This is fine for diagnostics but unsafe if any future code treats `return_value` as reconstructible state.
   - **Fix:** make result transport explicitly diagnostic-only or introduce a safe typed serialization protocol.

59. **`serious-condition` handling may classify control conditions incorrectly — MEDIUM**
   - Worker execution broadly traps `serious-condition`. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/worker/worker.lisp)
   - Lisp conditions/restarts are intended to become a structured agent interface, but flattening them into `"error" + backtrace` loses restart semantics.
   - **Fix:** transport condition type, structured slots, and available restart names separately before formatting diagnostics.

60. **Current worker path does not actually compile candidates as claimed — HIGH**
   - The hot path uses `READ` + `EVAL` over arbitrary forms. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/workers.py)
   - Yet `todos.md` marks “compile inside rehearsal worker” complete. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/todos.md)
   - A `DEFUN` may compile depending on implementation behavior, but this is not an explicit compile gate.
   - **Fix:** candidate grammar → construct lambda/function → `COMPILE` → capture compiler warnings/notes → only then execute tests.

61. **Worker generation fingerprint ignores almost all source files — HIGH**
   - Confirmed again: fingerprint hashes only `graygoo.asd`, SBCL version, and caller epoch. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/workers.py)
   - This should remain on the list, but sharpen it: **the fingerprint can claim equality after the worker implementation itself changed.**
   - That invalidates stale-candidate guarantees.

62. **Transfer evidence has no task identity, so “independent reuse” cannot be proven — HIGH**
   - `transfer-evidence` stores a numeric reuse count, not the IDs/families of the tasks constituting that count. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/memory/transfer.lisp)
   - The same task could theoretically be counted three times.
   - **Fix:** evidence should contain immutable task/run IDs and independence criteria; compute counts from evidence instead of accepting a count.

63. **Transfer evidence is caller-asserted rather than derived — HIGH**
   - `make-transfer-evidence` accepts arbitrary deltas and regression counts. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/memory/transfer.lisp)
   - This lets mutable code manufacture:
     ```lisp
     :reuse-count 999
     :severe-regressions 0
     ```
   - **Fix:** only the trusted evaluator/ledger projection should construct promotion evidence.

64. **Learning metrics are mutable scalar overwrites, not observations — MEDIUM**
   - `record-metric` simply overwrites a hash-table value. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/metrics/metrics.lisp)
   - You lose distributions, timestamps, experiment identity, confidence intervals, and raw observations.
   - **Fix:** metrics should be derived from immutable events/runs, with gauges only as projections.

65. **The research utility score is dimensionally unstable/gameable — MEDIUM**
   - Current score divides held-out success × transfer by cost × latency × regression penalty. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/metrics/metrics.lisp)
   - Tiny denominators can explode the score; units and scale changes radically affect it.
   - **Fix:** use a dashboard of primary metrics for decisions; if you need a composite, normalize each term against a baseline and bound penalties.

66. **Model call records lack experiment/run/candidate identity — MEDIUM**
   - Current model record tracks provider/model/tokens/context hash/prompt version/result, but not task ID, candidate ID, run ID, generation, or cost. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/model/model.lisp)
   - This makes causal experiment reconstruction harder.
   - **Fix:** every inference record should have immutable lineage into task → run → candidate → verdict.

67. **World-model projections currently have no provenance/freshness contract — MEDIUM**
   - `project-world` defines keys but not event offset, generation, timestamp, or consistency boundary. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/src/model/world.lisp)
   - Once implemented asynchronously, the context compiler could mix stale projections with current dispatch state.
   - **Fix:** every projection should carry `derived-through-event`, state generation, and active dispatch epoch.

68. **Risk of test leakage through persisted mutation history — HIGH**
   - Once hidden-evaluator verdicts influence repairs/lessons, the system can gradually infer characteristics of the hidden suite even if individual hidden tests aren't shown.
   - The architecture already acknowledges evaluator gaming, but current event/memory design needs an explicit information-flow rule.
   - **Fix:** do not persist hidden-test-specific rationales into mutable learning memory; expose coarse verdict categories and periodically rotate held-out generators.

69. **ASDF declares zero external dependencies while runtime behavior is split across Python — MEDIUM**
   - `graygoo.asd` presents a coherent Lisp system with no dependencies, while crucial worker/model orchestration exists outside that system. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/graygoo.asd)
   - Reproducibility of “GrayGoo generation X” therefore requires Python/runtime/tooling versions too.
   - **Fix:** create a top-level reproducibility manifest covering Lisp + Python + SBCL + OS sandbox + model/provider configuration.

70. **The source-of-truth problem is now measurable, not cosmetic — HIGH**
   - `todos.md` says the mutation pipeline, rehearsal system, event logging, context compiler and transfer promotion are complete, while corresponding Lisp modules still explicitly call themselves bootstrap stubs or error unimplemented. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/todos.md)
   - Agents can now plan future work based on false premises.
   - **Fix:** checkbox completion should require executable acceptance tests; generate status from CI, not agent-authored TODO edits.

## The six I'd file first

If you're turning these into GitHub issues, I'd prioritize the new ones like this:

| Priority | Issue |
|---|---|
| **P0** | #46 Sanitize worker environment / prevent secret inheritance |
| **P0** | #47 Replace fake restricted environment with enforceable sandbox |
| **P0** | #41 Fix transfer gate so it actually measures transfer |
| **P1** | #43–45 Make capability persistence genuinely immutable + atomic + hashed |
| **P1** | #54 Make version allocation atomic |
| **P1** | #62–63 Make transfer evidence task-backed and evaluator-derived |
| **P1** | #70 Make TODO completion test-backed |
| **P2** | #49–52 Fix capability-version data-model semantics |

The most concerning new discovery is actually **the transfer path**. `todos.md` considers transfer-based promotion complete, but the current `transfer-green-p` is effectively:

```lisp
(reuse >= 3) AND (no severe regression)
```