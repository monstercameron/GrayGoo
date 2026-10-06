Here’s the experiment as an execution-oriented TODO list, ordered to get to a real result as fast as possible.

- [x] **Bootstrap the runtime**
  - [x] Create Common Lisp repo + ASDF systems
  - [x] Standardize on SBCL
  - [x] Add SLY/Swank development setup
  - [x] Define packages for kernel, capabilities, workers, evaluation, memory, promotion, metrics

- [x] **Implement Cerebras/Qwen integration**
  - [x] Add Cerebras client
  - [x] Configure Qwen 3.8 27B
  - [x] Track latency, input/output tokens, cost, request IDs
  - [x] Add structured S-expression output mode
  - [x] Reject malformed model output before execution

- [x] **Build the capability model**
  - [x] Define `capability-id`
  - [x] Define immutable capability versions
  - [x] Add intent, contracts, inputs, outputs, effects, dependencies
  - [x] Add parent-version lineage
  - [x] Add TTL and promotion status
  - [x] Store source + metadata persistently

- [x] **Implement safe dispatch**
  - [x] Add explicit `invoke-capability`
  - [x] Implement versioned dispatch cells
  - [x] Implement request epochs
  - [x] Keep old versions alive while active epochs drain
  - [x] Implement instant rollback

- [x] **Create the rehearsal system**
  - [x] Launch isolated SBCL worker processes
  - [x] Add worker generation fingerprints
  - [x] Prewarm a worker pool
  - [x] Add wall-clock timeout
  - [x] Add CPU/memory limits
  - [x] Capture stdout, conditions, backtraces, return values
  - [x] Kill and recycle broken workers

- [x] **Build the mutation pipeline**
  - [x] Parse candidate Lisp
  - [x] Classify mutation risk R0–R6
  - [x] Compile inside rehearsal worker
  - [x] Run direct tests
  - [x] Run regression tests
  - [x] Run property tests
  - [x] Run old-vs-new differential tests
  - [x] Run performance checks
  - [x] Produce one compact verdict

- [x] **Create the external evaluator**
  - [x] Run evaluator in a separate process
  - [x] Keep hidden tests inaccessible to the agent
  - [x] Add fresh randomized cases
  - [x] Add metamorphic/equivalent-input tests
  - [x] Add adversarial edge cases
  - [x] Make evaluator verdict mandatory for promotion
  - [x] Execute candidates on fresh inputs in the promotion path (service-side fresh eval exists; promotion must run candidates on fresh cases and submit outputs)

- [x] **Implement green-stop**
  - [x] Define task success contracts
  - [x] Stop model interaction immediately when contract is satisfied
  - [x] Prevent optional refactors after success
  - [x] Track unnecessary post-success model actions as failures

- [x] **Add repair mode**
  - [x] Return minimal failing counterexample to Qwen
  - [x] Allow one repair attempt by default
  - [x] Rehearse repair from scratch
  - [x] Escalate only after bounded repair failure
  - [x] Detect repair oscillation

- [x] **Build event logging**
  - [x] Log every model call
  - [x] Log every candidate
  - [x] Log compile/test results
  - [x] Log promotion/rejection
  - [x] Log capability invocation/failure
  - [x] Log rollback
  - [x] Log task outcomes
  - [x] Make the event ledger append-only

- [x] **Build the context compiler**
  - [x] Generate minimal task context
  - [x] Include only relevant capabilities
  - [x] Include applicable contracts
  - [x] Include recent relevant failures
  - [x] Include allowed effects
  - [x] Enforce a context token budget
  - [x] Avoid raw full-history prompting

- [x] **Implement capability retrieval**
  - [x] Search by semantic intent
  - [x] Search by input/output type
  - [x] Search by effect requirements
  - [x] Rank by successful historical reuse
  - [x] Retrieve 3–8 relevant capabilities max
  - [x] Attempt composition before synthesis

- [x] **Create the first benchmark family**
  - [x] Start with parsing/data transformation
  - [x] Build 20–50 related tasks
  - [x] Hold back unseen transfer tasks
  - [x] Add adversarial variants
  - [x] Add equivalent transformations
  - [x] Establish baseline success with no learning

- [x] **Run baseline A: no persistent memory**
  - [x] Qwen solves every task from scratch
  - [x] Measure calls/task
  - [x] Measure tokens/task
  - [x] Measure latency/task
  - [x] Measure success rate
  - [x] Measure cost/task

- [x] **Implement executable patch memory**
  - [x] Persist successful candidate as patch
  - [x] Give patches short TTLs
  - [x] Retrieve patches on later related tasks
  - [x] Record whether reuse helped or hurt

- [x] **Implement transfer-based promotion**
  - [x] Require reuse on independent tasks
  - [x] Track success delta
  - [x] Track token savings
  - [x] Track latency savings
  - [x] Track negative transfer
  - [x] Promote patch → skill only after transfer evidence

- [x] **Add semantic procedural memory**
  - [x] Store intent
  - [x] Store applicability conditions
  - [x] Store abstract procedure
  - [x] Store known failure modes
  - [x] Link executable implementations
  - [x] Compare semantic+code memory vs code-only memory

- [x] **Run baseline B: textual memory only**
  - [x] Re-run benchmark family
  - [x] Compare against no-memory baseline
  - [x] Compare against executable-memory variant

- [x] **Run baseline C: executable memory**
  - [x] Re-run benchmark
  - [x] Track reuse
  - [x] Track negative transfer
  - [x] Track capability count
  - [x] Track library growth rate

- [x] **Run baseline D: dual memory**
  - [x] Semantic skill + executable specialization
  - [x] Compare held-out success
  - [x] Compare transfer success
  - [x] Compare tokens/task
  - [x] Compare capability entropy

- [x] **Add capability consolidation**
  - [x] Detect duplicates
  - [x] Detect overlapping intents
  - [x] Detect unused skills
  - [x] Propose merges/generalizations
  - [x] Rehearse consolidation
  - [x] Retire redundant capabilities

- [x] **Add skill forgetting**
  - [x] Expire unused patches automatically
  - [x] Require evidence to renew skills
  - [x] Measure whether forgetting improves retrieval quality

- [x] **Capture mutation trajectories (learning L1)**
  - [x] Store complete trajectories (goal → candidates → failures → repairs → success)
  - [x] Normalize failure classes (syntax, compile, contract, wrong-output, edge-case, state-corruption, effect-violation, performance, timeout, memory, stale-generation, over-refactor, negative-transfer, test-overfit, tool-misuse, context-missing)
  - [x] Store repair outcomes per trajectory
  - [x] Track successful vs failed development strategies

- [x] **Seed manual lessons (learning L2)**
  - [x] Define lesson schema (id, class, statement, applicability, evidence, counterexamples, confidence, impact, generation, TTL)
  - [x] Separate lesson classes: failure, repair, design, runtime-specific, model-behavior
  - [x] Separate facts from heuristics
  - [x] Manually seed 10–20 lessons
  - [x] Retrieve 0–5 lessons per synthesis call via context compiler
  - [x] Measure effect on first-pass success and tokens

- [x] **Mine lessons automatically (learning L3)**
  - [x] Cluster recurring failures from event ledger
  - [x] Cluster successful repairs
  - [x] Ask Qwen for concise candidate lesson per pattern
  - [x] Attach source evidence to every candidate
  - [x] Deduplicate candidate lessons

- [x] **Validate lessons by replay (learning L4)**
  - [x] Replay historical tasks: original context vs context + lesson
  - [x] Measure first-pass success, repair count, tokens, wall time, regressions
  - [x] Reject harmful or neutral lessons
  - [x] Promote useful lessons to registry
  - [x] Decay confidence on runtime/model upgrades; revalidate or downgrade
  - [x] Track counterexamples per lesson; refine overgeneralized lessons

- [x] **Consolidate lessons into principles and playbooks (learning L5)**
  - [x] Merge overlapping lessons
  - [x] Generalize survivors into principles; retire narrow duplicates
  - [x] Compile high-confidence lessons into task-family playbooks
  - [x] Promote stable playbooks to executable Lisp workflows

- [x] **Close the adaptive-development loop (learning L6)**
  - [x] Learn context composition (which elements correlate with success)
  - [x] Learn test ordering (fastest/highest-yield first)
  - [x] Learn repair routing (deterministic fix vs model repair vs escalate)
  - [x] Learn risk classification from predicted-vs-actual outcomes
  - [x] Learn capability applicability boundaries from retrieval outcomes
  - [x] Trigger lesson postmortem on every production rollback

- [ ] **Run the lesson key experiment**
  - [x] Condition A: no lesson memory
  - [x] Condition B: raw previous transcripts
  - [x] Condition C: retrieved distilled lessons
  - [x] Condition D: lessons + executable capability memory
  - [x] Compare first-pass success, repair loops, tokens, calls, time-to-verified-mutation, held-out success
  - [ ] Confirm D > C > A and distilled beats raw transcripts

- [x] **Add effect isolation**
  - [x] Introduce effect declarations
  - [x] Virtualize filesystem writes
  - [x] Virtualize network calls
  - [x] Add transactional state sandbox
  - [x] Ensure failed experiments cannot mutate canonical state

- [ ] **Adversarially attack the rehearsal boundary**
  - [x] Infinite loop candidate
  - [x] Memory bomb
  - [x] Process spawn attempt
  - [ ] Filesystem escape attempt
  - [x] Network escape attempt
  - [x] Kernel mutation attempt
  - [ ] Evaluator inspection attempt
  - [ ] Confirm all fail safely

- [x] **Measure true learning**
  - [x] Compare current system against memory-disabled current model
  - [x] Compare against previous stable generation
  - [x] Measure held-out task success
  - [x] Measure reuse rate
  - [x] Measure negative transfer
  - [x] Measure tokens/task trend
  - [x] Measure calls/task trend
  - [x] Measure time-to-verified-mutation
  - [x] Measure capability entropy

- [ ] **Define experiment success**
  - [x] Held-out success increases or remains stable
  - [x] Average model calls/task decreases (repeated streams 2.0→0.72; held-out still flat)
  - [x] Average tokens/task decreases (repeated streams 273→88; held-out still +65–135%)
  - [x] Capability reuse increases (hit rate 0%→50%→64% on repeated streams)
  - [x] Negative transfer stays below threshold
  - [x] Capability growth becomes sublinear (concave via dedup-by-check on streams)
  - [ ] Zero canonical-state corruption
  - [x] Rollback works reliably

- [ ] **Harden worker sandbox (adversarial report 2026-10-05)**
  - [x] Fix risk classifier: route dispatch/entrypoint redefinition to R6
  - [x] Strip process-spawn primitives from worker image or gate R4-process rehearsal on approval
  - [ ] Jail worker filesystem (OverlayFS path rewriting plus deny ACLs outside jail)
  - [ ] Separate hidden evaluator corpus from rehearsal host (verdicts only over protocol)
  - [ ] Control worker network egress (remove socket primitives or firewall worker identity)
  - [ ] Re-run adversarial suite until all attacks fail safely

- [x] **Fix QA findings (qa-report 2026-10-05)**
  - [x] QA-01: coerce non-string recorded output in runner exact compare
  - [x] QA-02: runner exits nonzero when filter selects zero tasks
  - [x] QA-03: pipeline fails on unknown stage names
  - [x] QA-04: normalize worker return_value comparison in pipeline checks
  - [x] QA-05: collision-proof promotion version filenames
  - [x] QA-06: promotion ledger outage returns reject dict (fail closed)
  - [x] QA-07: risk scan value-position atoms, not just head position
  - [x] QA-08: flag mutation targets naming core dynamic forms
  - [x] QA-09: reject promotion on corrupt versions/epochs state
  - [x] QA-10: fail (or skip explicitly) stages running zero checks
  - [x] QA-11: depth guard in risk walker
  - [x] QA-12: type-check check values in runner validate_tasks

- [ ] **Focus: executable reuse thesis (session goal 2026-10-05)**
  - [x] Define REUSE/COMPOSE/ADAPT/NOVEL outcome taxonomy (`outcomes.py`)
  - [x] Build executable capability registry with applicability gate (`execaps.py`, 5 verified seeds)
  - [x] Build composition executor: plans reusing >=2 capabilities, zero model calls
  - [x] Add reuse-designed held-out tasks (`benchmarks/family-r/`: 4 reuse + 1 trap + 2 compose)
  - [x] Build canonical A/B/C/D driver (`benchmarks/run_abcd.py`)
  - [x] Verify offline Family-R: D 8/8 at 3 calls vs A 8/8 at 15 calls; C 6/8 honest misfire pinned
  - [x] Compression warning live: 7 tasks / 7 artifacts = 1.0, warning firing (see `documents/abcd.md`)
  - [x] Run live Family-R comparison (28 calls, $0.007): D 7/7 dominates A/B 6/7 at 13x fewer calls
  - [x] Full hierarchy in C/D: reuse → compose → adapt (retrieve-closest + minimal context) → novel
  - [x] Evidence ledgers (positive/negative per artifact) + applicability metrics (precision/FP/FN)
  - [x] Deterministic composition search: SEQ/MAP discovery, typed glue, output-demand + trial + vacuity gates
  - [x] API-workflow benchmark (`benchmarks/family-w/`: 8 primitives + 6 transfer incl. 3-composition)
  - [x] Verify offline Family-W: D 6/6 (2 REUSE + 2 COMPOSE + ADAPT + NOVEL) vs A 6/6 all NOVEL
  - [x] Run live Family-W comparison (32 calls, $0.005): D 6/6 matches B at 1/3 calls, beats A 4/6
  - [x] Multi-order runs + learning curves (§5, §7) + latency/TTVR metrics (§6)
  - [x] Transfer-based promotion rule on evidence ledgers (§9) + failure criteria (§15)
  - [ ] Generalize seeds so artifact count stays flat as reuse tasks grow (close compression warning)
  - [ ] Contract-coverage applicability: single-cap abstains when prompt demands more than its contract
  - [x] Synthesis learning: distill.py (Qwen synthesis → AST gate → rehearsal → verify → register → consolidate)
  - [x] Learned registry replaces hand seeds: run_abcd --capabilities, 13/13 distilled live ($0.044)
  - [x] Inflection-tolerant SIG matching shared by synthesis/reuse/compose gates + regression tests
  - [x] Compose gate fix: mean-overlap gate removed (float lottery), joint-coverage floor (≥3 words) added
  - [x] Learned transfer parity (SAME held-out tasks): stub W 6/6 + R 8/8; live W 6/6 + R 7/8 = hand
  - [x] Zero-LLM REUSE/COMPOSE verified per-record (8 outcomes, 0 calls) + learning metrics tracked
  - [ ] Compress across procedures: consolidation dropped 0, still one function per exposure task
  - [ ] Separate prompt-distinguished semantic siblings (A-EXP-05/08 null-vs-empty; hand seeds share the flaw)

- [ ] **Only after the core experiment succeeds**
  - [ ] Add shadow execution on real workloads
  - [ ] Add canary promotion
  - [ ] Add schema evolution
  - [ ] Add macro synthesis
  - [ ] Add recursive inference
  - [ ] Add policy self-improvement
  - [ ] Add one-mutation A/B meta-learning
  - [ ] Add distributed rehearsal workers

The **minimum convincing experiment** is much smaller than the whole project:

```text
SBCL
+ Cerebras/Qwen
+ isolated workers
+ versioned capabilities
+ benchmark family
+ patch persistence
+ transfer testing
+ no-memory baseline
```