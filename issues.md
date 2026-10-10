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

Yes. Fresh `main` has enough new code that I found **9 additional issues plus 2 important reopenings**. The repo has improved materially, but a few of the new protections have bypasses.

### New issues to add

**71. Risk classification is optional and missing classification fails open — CRITICAL**

`run_candidate(..., risk_fn=None)` marks risk as `"unclassified"` but records the risk stage as **pass** and proceeds with execution. `_risk_blocks()` only blocks R6/explicit `blocked`, so “we don't know the risk” is currently treated as safe enough to rehearse. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/pipeline.py)

**Fix:** absence of a classifier or any `unclassified` result must fail closed before execution.

---

**72. Entire mutation pipeline can return PASS without running a single test — CRITICAL**

If `tests` is `{}` or all stages are absent/`None`, `ran_any` remains false and the pipeline returns:

```text
candidate parsed; no checks requested
```

with `ok=True`. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/pipeline.py)

That means parsing alone can produce a green verdict if a caller misconfigures the pipeline.

**Fix:** zero executed verification stages must always be non-promotable. Require a minimum gate set derived from risk level.

---

**73. Python S-expression parser and the real Lisp reader do not have the same semantics — HIGH**

This is subtle and potentially nasty.

Your Python parser implements only a subset: parentheses, strings, `'` quote sugar, atoms, and treats comma as whitespace. It does **not** implement Common Lisp reader semantics for backquote, unquote, dispatch macros, escaped symbols, reader conditionals, pathname syntax, etc. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/s_expr.py)

But the worker later hands source to the actual SBCL reader. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/workers.py)

Therefore you effectively have:

```text
Python interpretation used for security/risk
             ≠
SBCL interpretation used for execution
```

That is a classic parser-differential attack surface.

**Fix:** don't risk-scan one representation and execute another. Parse the constrained grammar once, then **generate canonical Lisp from the validated AST** and execute that canonical form.

This is probably the most important architectural security change I'd make.

---

**74. Hard process-deny can be hidden inside quoted code passed to `eval` — HIGH**

`sandbox._payload_walk()` deliberately skips quoted subtrees. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/sandbox.py)

So something conceptually like:

```lisp
(eval '(sb-ext:run-program ...))
```

has an outer `eval` that risk classification recognizes as dynamic/R4, but the sandbox's “process effects are never rehearsed” scanner can skip the quoted `run-program`. `eval` itself is explicitly considered dynamic by the risk classifier. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/risk.py)

If that R4 mutation gets approval for rehearsal, you've bypassed the intended unconditional process deny.

The Lisp-level sandbox may still stop the obvious API call, but you've explicitly documented that it is bypassable.

**Fix:** for the hard-deny layer, dynamic evaluation (`eval`, `compile`, `load`, `funcall` over computed targets, etc.) should either:
- be unrehearsable, or
- require a much stronger execution boundary.

---

**75. Sandbox silently disappears if `sandbox.py` fails to import — CRITICAL**

Workers default to `sandbox=True`, which sounds fail-closed.

But:

```python
try:
    import sandbox as _sandbox
except ImportError:
    _sandbox = None
```

and then `_sandbox_prelude()` returns `""` if `_sandbox is None`. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/workers.py)

So an installation/package error silently transforms:

```text
sandbox=True
```

into:

```text
run completely unsandboxed
```

**Fix:** if sandboxing is requested and sandbox initialization is unavailable, worker startup must fail.

---

**76. Caller-provided prelude runs with enough authority to dismantle the sandbox — HIGH**

Current order is:

```text
sandbox containment prelude
→ caller-supplied prelude
→ candidate
```

because `effective_prelude` concatenates the sandbox guard followed by the supplied prelude. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/workers.py)

If `prelude` ever contains generated/task-controlled content, it gets an opportunity to restore functions, unlock packages, rebind globals, etc. before the candidate executes.

**Fix:** formally designate prelude as trusted-kernel input and enforce that boundary. Better:

```text
trusted setup
→ lockdown as final setup operation
→ candidate
```

so no mutable setup executes after lockdown.

---

**77. Performance benchmark mostly measures SBCL startup, not candidate performance — HIGH**

The performance stage uses `record["elapsed_ms"]`, which comes from `run_lisp()`'s wall-clock timer. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/pipeline.py)

But every `run_lisp()` currently launches a fresh SBCL, loads worker packages, loads rehearsal code, installs sandboxing, evaluates the prelude, and only then executes the candidate. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/workers.py)

So:

```text
reported candidate performance =
process spawn
+ SBCL startup
+ source loads
+ sandbox install
+ candidate
```

This can completely drown out a 50 μs versus 5 ms algorithmic regression.

**Fix:** transport both:

```text
worker_wall_ms
candidate_cpu_ms
candidate_wall_ms
alloc_bytes
GC time
```

and base capability performance gates on in-worker candidate measurements.

---

**78. Performance stage can pass despite having no performance threshold — MEDIUM**

If samples exist but `budget_ms` is missing, the performance stage explicitly records a note and returns success. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/pipeline.py)

That's useful for telemetry but should not satisfy a mandatory `performance-checks` gate.

**Fix:** distinguish:

```text
MEASURED
PASS
FAIL
```

A measurement without an acceptance threshold is not verification.

---

**79. State sandbox exposes an escape hatch that can desynchronize transaction bookkeeping — MEDIUM**

`StateSandbox.connection` publicly exposes the raw SQLite connection. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/effects.py)

The sandbox separately maintains:

```python
_savepoints
_sp_counter
```

as its authoritative nesting state.

External code can therefore do things like direct `COMMIT`, `ROLLBACK`, create savepoints, alter pragmas, etc., leaving GrayGoo's bookkeeping inconsistent with SQLite.

**Fix:** generated/speculative consumers should never receive the raw connection. Keep the escape hatch explicitly trusted-only, or remove it.

---

### Reopen / sharpen two existing issues

**#1 reader-eval is not fully fixed.**

The new code correctly binds:

```lisp
(*read-eval* nil)
```

while looping over forms. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/workers.py)

But the code reads **one form, evaluates it, then reads the next form**:

```text
READ form1
EVAL form1
READ form2
EVAL form2
```

Because `*read-eval*` is dynamically bound but mutable, form 1 can potentially alter the current binding before form 2 is read.

Conceptually:

```lisp
(setq *read-eval* t)
#.(dangerous-reader-time-code)
```

So the current comment claiming reader evaluation “never fires here” is stronger than the implementation guarantees. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/workers.py)

**Better fix:** parse all forms while `*read-eval*` is unchangeably controlled **before evaluating any of them**, or—better—execute only canonical forms generated from your Python validated AST.

That also solves #73.

---

**#46 host environment inheritance still appears unresolved.**

The current `subprocess.Popen()` still supplies no explicit sanitized `env=`. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/workers.py)

So the worker still inherits the coordinator environment.

Given that the Lisp-level sandbox is explicitly documented as bypassable, this remains a P0 until OS/process isolation lands.

---

## One design change fixes several of these at once

I would now strongly recommend changing the worker boundary from:

```text
LLM source string
↓
Python parses it
↓
Python security-scans it
↓
original source string
↓
SBCL READ again
↓
EVAL
```

to:

```text
LLM source
↓
restricted parser
↓
validated GrayGoo AST
↓
risk/effect classification
↓
canonical Lisp emitter
↓
ONE known canonical form
↓
COMPILE
↓
execute
```

That eliminates a whole class of:

- reader-macro tricks,
- parser differentials,
- multiple-form `*read-eval*` attacks,
- hidden syntax,
- ambiguity between what the security layer approved and what SBCL executed.

The latest code is moving in a good direction—the risk classifier in particular is now substantially more serious—but **the parser/executor split is becoming the next major attack surface**. [GitHub](https://raw.githubusercontent.com/monstercameron/GrayGoo/main/risk.py)

So I’d add **#71–79**, reopen **#1**, and keep **#46** P0.


Yes. Based on the **experiment docs specifically**, I’d add these issues to the backlog. These are mostly scientific-validity and measurement gaps rather than implementation bugs.

71. **No frozen benchmark version / benchmark manifest**
   - If Family A tasks change while GrayGoo evolves, results across runs stop being comparable.
   - Add a versioned benchmark manifest with task hashes, split IDs, generator version, and seed.

72. **Exposure / transfer / held-out split needs leakage protection**
   - The docs define these splits, but the system needs explicit guarantees that task generation, lesson mining, and replay cannot leak held-out examples into training memory.
   - Hidden test/task IDs should never enter mutable memory.

73. **Independent reuse is underspecified**
   - “3 independent reuses” needs a formal definition.
   - Three syntactic variants of the same task should not count as independent transfer.

74. **Negative transfer needs an explicit severity taxonomy**
   - The docs track “severe negative transfer,” but severity needs objective criteria.
   - Example: accuracy regression, latency regression, token regression, state-safety regression should be separated.

75. **Promotion thresholds are currently arbitrary**
   - `>=3 reuses`, zero severe regressions, etc. are reasonable starting points but unvalidated.
   - Add a threshold-calibration experiment instead of treating them as fixed truth.

76. **No statistical significance / uncertainty plan**
   - Results like “tokens/task fell 12%” are meaningless without variance and repeated runs.
   - Track confidence intervals or bootstrap intervals for key deltas.

77. **No preregistered experiment stopping criteria**
   - Without fixed stopping rules, it is easy to keep iterating until the desired result appears.
   - Define number of runs/tasks/seeds before evaluating success.

78. **Baseline C is underspecified**
   - “Qwen + conventional coding tools” needs a precise toolset, prompt, context budget, and repair budget.
   - Otherwise GrayGoo can accidentally compare against a weak strawman baseline.

79. **Baseline token budgets must be normalized**
   - A learned-memory system should not get a larger inference budget than the no-memory baseline.
   - Define equal or explicitly reported resource budgets across arms.

80. **Model/provider version drift can invalidate longitudinal comparisons**
   - Cerebras/Qwen behavior may change over time.
   - Record exact provider model ID, API version, sampling parameters, and date for every run.

81. **No benchmark contamination check for pretrained model knowledge**
   - Some benchmark tasks may already be trivial for Qwen due to pretraining.
   - Include procedurally generated or synthetic task families where contamination is unlikely.

82. **Capability reuse can improve latency while hurting generalization**
   - The experiment needs to separately measure:
     - raw reuse rate
     - useful reuse rate
     - harmful reuse rate
   - High reuse alone is not evidence of learning.

83. **Zero-model-call success can be gamed by memorization**
   - Reaching zero calls on repeated tasks is not impressive if the system just stores exact task solutions.
   - Zero-call success should be measured primarily on unseen members of a task family.

84. **Capability-library growth needs a normalized metric**
   - “Growth slows” is currently qualitative.
   - Track capabilities per solved novel task and bytes/tokens of learned memory per task.

85. **Capability entropy is not formally defined**
   - The docs reference entropy/complexity, but the metric needs a stable definition before it can support conclusions.
   - Consider overlap, unused-capability ratio, graph depth, and semantic duplication as separate metrics first.

86. **Lesson-memory experiment has a confound between better context and actual learning**
   - Distilled lessons may simply provide better prompting, not durable learning.
   - Add a control where equivalent handcrafted guidance is supplied.

87. **Raw transcript baseline may be unfairly weak**
   - Dumping raw history verbatim can create an intentionally bad baseline.
   - Give transcript memory a reasonable retrieval/compression strategy.

88. **Replay validation can overfit the replay corpus**
   - Lessons validated repeatedly on the same historical trajectories may specialize to those trajectories.
   - Use a second held-out replay set for lesson validation.

89. **Lesson acceptance needs negative evidence**
   - A lesson should store not only supporting cases but counterexamples where it hurt.
   - Otherwise confidence only ratchets upward.

90. **No explicit measure of first-pass synthesis quality**
   - Track:
     - first candidate pass rate
     - repair count
     - total candidates/task
   - This is central to measuring whether the learning layer actually accelerates development.

91. **Time-to-verified-mutation needs decomposition**
   - Break out:
     - context retrieval
     - model inference
     - worker startup
     - compile
     - tests
     - evaluator
     - promotion
   - Otherwise you cannot tell where GrayGoo is improving or regressing.

92. **Cost/task is provider-price dependent**
   - Dollar cost can change independently of system quality.
   - Always report raw input/output tokens and model calls alongside USD cost.

93. **No explicit failure criterion for the core thesis**
   - Define what result would make you conclude executable memory is not useful.
   - Example: if held-out success does not improve and tokens/task do not fall after N tasks, hypothesis fails.

94. **No cross-family transfer test**
   - Current design focuses heavily on within-family transfer.
   - Add a later experiment for whether a procedural abstraction learned in one family helps another related family.

95. **No forgetting-vs-retention experiment**
   - TTL/retirement is part of the architecture, but there should be an explicit A/B test:
     - no forgetting
     - TTL forgetting
     - usage-weighted forgetting

96. **No consolidation ablation**
   - To prove consolidation matters, compare:
     - flat skill library
     - consolidated procedural families
   - Measure retrieval accuracy, latency, and negative transfer.

97. **No lesson retrieval precision metric**
   - Track whether retrieved lessons were actually useful for the mutation.
   - Otherwise more retrieved lessons may silently degrade performance.

98. **No causal attribution between capability memory and lesson memory**
   - Full GrayGoo combines both, so improvements can be hard to attribute.
   - Keep factorial experiments:
     - neither
     - capability only
     - lesson only
     - both

99. **Task-family difficulty should be calibrated**
   - If early tasks are harder than later tasks, decreasing tokens/task could be falsely interpreted as learning.
   - Randomize or stratify task difficulty across sequence position.

100. **Task ordering can bias the learning curve**
   - A fixed curriculum may make later tasks naturally easier.
   - Run multiple randomized task orders/seeds.

101. **No cold-start vs warm-start comparison**
   - Measure the same held-out tasks with:
     - empty memory
     - mature memory
   - This gives a clean estimate of accumulated system value.

102. **No catastrophic-memory test**
   - Inject one bad promoted skill/lesson and measure whether GrayGoo detects, contains, and recovers from it.
   - This is essential for long-running self-learning systems.

103. **No measurement of memory retrieval overhead**
   - As capability/lesson memory grows, retrieval itself may become expensive.
   - Track retrieval latency and context size against memory size.

104. **No benchmark for misleading near-match capabilities**
   - Add tasks where an existing capability looks semantically relevant but is subtly wrong.
   - This tests applicability boundaries and negative transfer.

105. **No explicit “learning efficiency” metric**
   - Add something like:
   ```text
   improvement on held-out tasks
   -----------------------------
   total mutation + inference cost
   ```
   - This captures how expensive the learning process itself is.

106. **No durability test across process restart**
   - Since persistence is central to the thesis, prove that learned capabilities and lessons survive restart and still reproduce the same behavior.

107. **No reproducibility target**
   - Define what another machine should be able to reproduce from a run artifact:
     - benchmark
     - model config
     - runtime version
     - capability state
     - random seeds
     - final metrics

108. **No experiment artifact bundle**
   - Every major run should produce a self-contained report:
     - config
     - benchmark version
     - commits
     - metrics
     - event log hash
     - plots
     - promoted skills
     - failures

109. **The core thesis lacks a single canonical headline experiment**
   - The docs contain many good experiments, but you need one primary result everyone can understand.
   - I’d define:
   > “Across 100 unseen related tasks, does mature GrayGoo beat the same Qwen model with memory disabled on held-out success while using materially fewer model calls and tokens?”

110. **Success criteria should include system simplicity**
   - A 20% token reduction is not impressive if it requires 10× more infrastructure and validation cost.
   - Report total compute and end-to-end latency, not only model inference savings.

The **highest-value ones to add immediately** are **#71–80, #83, #88, #93, #98–100, and #109**. Those determine whether the eventual result is scientifically convincing rather than merely a cool demo.

Prioritized issues to add
Status (2026-10-10): all 15 rows below were worked; the Status column says what was done and what was left out, with the tests that hold it. Issues #1-110 above are tracked in documents/issues-triage.md.
These are the issues I would give the coding agents. Some are confirmed implementation limitations; others are architectural risks requiring regression tests.
Priority	Issue	Evidence / required fix	Status
P0	Persistent SBCL accepts unrestricted evaluated forms	lispserver.py reads then evaluates forms. Ensure every runtime call passes the same strict allowlist used by the interactive REPL, with no path for generated code to redefine trusted functions.	DONE (2026-10-10). Every candidate passes `lispstyle.trust_problems` before anything runs: it may not redefine, remove or shadow a function it does not own, define anything beside its one function, call eval/compile/load/intern, use a package-qualified or escaped symbol, or `#.`. The warm process keeps a record of the trusted functions; a candidate that changes one is rejected, the function is put back and the process restarted (`tamper` event). 129 saved functions scanned: none trips a rule. NOT DONE: OS-level isolation of the warm process, still tracked as #46. Tests: `tests/test_trust_lint.py`, `tests/test_lispserver_state.py`, `tests/test_round13.py`.
P0	Application authentication is not production-safe	visualcheck.py explicitly reads plaintext credentials from generated user-state rows. Replace password storage with a trusted password-hashing/authentication adapter before treating mounted apps as secure.	DONE for what the harness controls. The web kit has `hash-password` and `password-matches-p` (salted SHA-256, 1,000 rounds); the web-app contract requires them and a seeded demo/demo account; the sign-in walk no longer needs readable passwords; a new free check, passwords-hashed, fails a build whose working password can be read in its users table. SKIPPED: a production key-derivation function (bcrypt, argon2) is out of reach for pure Lisp functions without a foreign library, so mounted apps remain demos. The saved pcrm app still stores plain text; the check now reports it and the next prompt is told to fix it. Tests: `tests/test_webkit_auth.py`, `tests/test_acceptance.py`, `tests/test_visual.py`.
P0	Feature completion is inferred from names/text	goalcheck.py uses regular expressions over function names, descriptions and definitions. A function named handle-login does not prove authentication works. Require behavioral acceptance tests for goal completion.	DONE. `acceptance.py` tries the finished app like a visitor (9 checks, no model call, about 0.4 s). A goal feature whose check fails is reported as not built even when the code names it. LIMIT: login, create and list have checks; update, delete, search, styling and validation are still judged from the code text (delete is never clicked on purpose). Tests: `tests/test_acceptance.py`, `tests/test_round13.py`.
P1	Full application integration remains unreliable	Previous todo and CRM builds accumulated many passing functions before completing the application. Add end-to-end acceptance scenarios and dependency-aware fault localization.	DONE. The visitor checks run after every build that touches an app, single-function builds included. What they find broken gets one round of fixes that is kept only if nothing that passed now fails. `interfaces.py` names the function and route at fault, and the planner of the next prompt is given those facts. On the saved pcrm app this finds three real faults for free: logout does not end the session, the edit form posts to a route that does not exist, and the plain-text password.
P1	Compiled tool contracts can drift across functions	CSS and page class mismatches required several rounds of fixes. Extend interface checks to state schemas, response formats, argument conventions and dependency contracts.	DONE. `interfaces.py` compares the saved functions: call arity, form and link targets against the router, state tables read but never written, helpers called but not saved, handlers nothing routes to. A call with the wrong number of arguments is refused before the candidate runs. SKIPPED for now: HTTP-method mismatches (a POST form to a GET-only route) and response-format checks; listed in todos.md. Tests: `tests/test_interfaces.py` (47).
P1	Persistent process state can contaminate later requests	Long-lived SBCL workers retain mutable state. Test resets, concurrent requests, reloads, partial failures and restarting from canonical definitions.	DONE. After each rehearsal the warm process restores the function under test from the saved definition, checks the trusted functions against its record, and restarts from the canonical definitions if one changed. Concurrent requests, reloads and a cached-response staleness case are tested against mounted apps. Tests: `tests/test_lispserver_state.py`, `tests/test_concurrency_evidence.py` (cases 11, 12).
P1	Compact text edits need stronger identity checks	compaction.py patches prior source text. Require source-version hashes, AST-level validation and rejection if the target changed since the model generated its edit.	DONE. An edit is applied only to the exact version the model was shown (`definition_sha`); each search text must match once; the result must parse as one function with the same name. Anything else is rejected and the model is asked for the whole function. Tests: `tests/test_compaction.py` (67).
P1	Visual repair can regress an otherwise successful build	The repo documents this happening. Keep candidate visuals isolated, rerun previous acceptance tests, and promote only if both behavior and visual quality pass.	DONE. A round of fixes starts from a registry snapshot. It is undone (`visual_rollback` event) when the app stops answering, when a visitor check that passed now fails, or when the next look at the screenshots shows more problems than before. Tests: `tests/test_round13.py` (GuardedFixTests).
P1	Cancellation needs transactional semantics	Cancelling must terminate pending work without losing previously verified tools or promoting half-validated changes. Test cancellation during every pipeline stage.	DONE. `tests/test_cancel_stages.py` cancels at 13 pipeline stages (19 tests) and holds each to the same rules: what was saved stays, nothing half-checked is saved, no model call starts afterwards. Four bugs found and fixed: a cancel during the smoke check or the last rehearsal ended as done, the reply of a JSON retry was still built, and the no-memory twin could not be cancelled.
P1	Model-call budgets can expand substantially in planning	Planned builds now permit up to 80 calls. Add per-goal spending, wall-time and call budgets with explicit escalation before expensive continuation.	DONE. One build stops at $0.40, 300 s or its call limit, whichever comes first, keeps what it saved and says which limit it reached. Sending the prompt again (Continue) is the explicit go-ahead to spend more. A build never gets more than what is left of the per-process spend cap. Tests: `tests/test_round13.py` (BudgetTests).
P2	Tool registry relevance is still heuristic	Focused registry compression prioritizes named/recent tools. Test whether older relevant capabilities are omitted and whether this leads to unnecessary regeneration.	DONE in part. A saved function that shares two or more content words with the call keeps its description at the first compaction level, so an older relevant function is no longer reduced to a bare signature. Regeneration is now measured (`economics.py`: 63% of the pcrm spend rebuilt functions that already existed). STILL HEURISTIC: word overlap, and levels 2 and 3 drop the descriptions again.
P2	Visual validation coverage remains shallow	The visitor walker follows a limited set of routes and forms. Add systematic CRUD flows, authentication failure cases, state transitions and mobile viewport checks.	DONE in part. Added: wrong-password and logout checks, a create flow with a reload, link checks, a no-server-error check over every request made, and one screenshot at phone width (390 x 844) when the goal asks for a small-screen layout. SKIPPED: update and delete flows (destructive, so never clicked).
P2	Missing lifecycle economics for complex applications	Track total learning cost, saved capability creation cost, repair cost, actual reuse savings and break-even time across projects.	DONE. `uv run python economics.py <project>` reports total spend, spend by kind of call, repair share, the share spent rebuilding existing functions, reuse and break-even. For pcrm: $1.57 over 19 builds, 370 calls, repairs 50% of spend. NOT DONE: showing it in the dashboard (todos.md). Tests: `tests/test_economics.py`.
P2	Concurrency needs evidence beyond throughput	Test parallel changes to shared capabilities, promotion races, registry writes, and simultaneous requests against mounted applications.	DONE. `tests/test_concurrency_evidence.py` (18 tests, real threads): registry writes, replacement while others read, promotion races, lanes, 40 simultaneous requests against a mounted app, the rate-limit gate. Six bugs found and fixed: two registries on one file lost adds, readers saw an empty registry during a write, concurrent starts opened several servers, the request log lost lines, a project touch undid a rename, and a busy port could be bound twice on Windows.
P2	Automated completion reports need independent verification	Distinguish a model's “done” response, passing function tests, successful integration, and independently verified user-goal completion.	DONE. The run summary has a `verification` block that keeps the levels apart: function tests, the app answering, visitor checks, functions fitting together, style classes, screenshots. A level that did not run says so, and the model's own done is never counted. The dashboard shows it as 'Checked by the harness itself' at the top of the end-of-run card.