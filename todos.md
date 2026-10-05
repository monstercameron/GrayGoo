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

- [ ] **Create the external evaluator**
  - [x] Run evaluator in a separate process
  - [x] Keep hidden tests inaccessible to the agent
  - [x] Add fresh randomized cases
  - [x] Add metamorphic/equivalent-input tests
  - [x] Add adversarial edge cases
  - [ ] Make evaluator verdict mandatory for promotion

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

- [ ] **Implement capability retrieval**
  - [ ] Search by semantic intent
  - [x] Search by input/output type
  - [x] Search by effect requirements
  - [x] Rank by successful historical reuse
  - [x] Retrieve 3–8 relevant capabilities max
  - [x] Attempt composition before synthesis

- [ ] **Create the first benchmark family**
  - [x] Start with parsing/data transformation
  - [x] Build 20–50 related tasks
  - [x] Hold back unseen transfer tasks
  - [x] Add adversarial variants
  - [x] Add equivalent transformations
  - [ ] Establish baseline success with no learning

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

- [ ] **Add semantic procedural memory**
  - [ ] Store intent
  - [ ] Store applicability conditions
  - [ ] Store abstract procedure
  - [ ] Store known failure modes
  - [ ] Link executable implementations
  - [ ] Compare semantic+code memory vs code-only memory

- [ ] **Run baseline B: textual memory only**
  - [ ] Re-run benchmark family
  - [ ] Compare against no-memory baseline
  - [ ] Compare against executable-memory variant

- [ ] **Run baseline C: executable memory**
  - [ ] Re-run benchmark
  - [ ] Track reuse
  - [ ] Track negative transfer
  - [ ] Track capability count
  - [ ] Track library growth rate

- [ ] **Run baseline D: dual memory**
  - [ ] Semantic skill + executable specialization
  - [ ] Compare held-out success
  - [ ] Compare transfer success
  - [ ] Compare tokens/task
  - [ ] Compare capability entropy

- [ ] **Add capability consolidation**
  - [ ] Detect duplicates
  - [ ] Detect overlapping intents
  - [ ] Detect unused skills
  - [ ] Propose merges/generalizations
  - [ ] Rehearse consolidation
  - [ ] Retire redundant capabilities

- [ ] **Add skill forgetting**
  - [ ] Expire unused patches automatically
  - [ ] Require evidence to renew skills
  - [ ] Measure whether forgetting improves retrieval quality

- [ ] **Capture mutation trajectories (learning L1)**
  - [ ] Store complete trajectories (goal → candidates → failures → repairs → success)
  - [ ] Normalize failure classes (syntax, compile, contract, wrong-output, edge-case, state-corruption, effect-violation, performance, timeout, memory, stale-generation, over-refactor, negative-transfer, test-overfit, tool-misuse, context-missing)
  - [ ] Store repair outcomes per trajectory
  - [ ] Track successful vs failed development strategies

- [ ] **Seed manual lessons (learning L2)**
  - [ ] Define lesson schema (id, class, statement, applicability, evidence, counterexamples, confidence, impact, generation, TTL)
  - [ ] Separate lesson classes: failure, repair, design, runtime-specific, model-behavior
  - [ ] Separate facts from heuristics
  - [ ] Manually seed 10–20 lessons
  - [ ] Retrieve 0–5 lessons per synthesis call via context compiler
  - [ ] Measure effect on first-pass success and tokens

- [ ] **Mine lessons automatically (learning L3)**
  - [ ] Cluster recurring failures from event ledger
  - [ ] Cluster successful repairs
  - [ ] Ask Qwen for concise candidate lesson per pattern
  - [ ] Attach source evidence to every candidate
  - [ ] Deduplicate candidate lessons

- [ ] **Validate lessons by replay (learning L4)**
  - [ ] Replay historical tasks: original context vs context + lesson
  - [ ] Measure first-pass success, repair count, tokens, wall time, regressions
  - [ ] Reject harmful or neutral lessons
  - [ ] Promote useful lessons to registry
  - [ ] Decay confidence on runtime/model upgrades; revalidate or downgrade
  - [ ] Track counterexamples per lesson; refine overgeneralized lessons

- [ ] **Consolidate lessons into principles and playbooks (learning L5)**
  - [ ] Merge overlapping lessons
  - [ ] Generalize survivors into principles; retire narrow duplicates
  - [ ] Compile high-confidence lessons into task-family playbooks
  - [ ] Promote stable playbooks to executable Lisp workflows

- [ ] **Close the adaptive-development loop (learning L6)**
  - [ ] Learn context composition (which elements correlate with success)
  - [ ] Learn test ordering (fastest/highest-yield first)
  - [ ] Learn repair routing (deterministic fix vs model repair vs escalate)
  - [ ] Learn risk classification from predicted-vs-actual outcomes
  - [ ] Learn capability applicability boundaries from retrieval outcomes
  - [ ] Trigger lesson postmortem on every production rollback

- [ ] **Run the lesson key experiment**
  - [ ] Condition A: no lesson memory
  - [ ] Condition B: raw previous transcripts
  - [ ] Condition C: retrieved distilled lessons
  - [ ] Condition D: lessons + executable capability memory
  - [ ] Compare first-pass success, repair loops, tokens, calls, time-to-verified-mutation, held-out success
  - [ ] Confirm D > C > A and distilled beats raw transcripts

- [ ] **Add effect isolation**
  - [ ] Introduce effect declarations
  - [ ] Virtualize filesystem writes
  - [ ] Virtualize network calls
  - [ ] Add transactional state sandbox
  - [ ] Ensure failed experiments cannot mutate canonical state

- [ ] **Adversarially attack the rehearsal boundary**
  - [ ] Infinite loop candidate
  - [ ] Memory bomb
  - [ ] Process spawn attempt
  - [ ] Filesystem escape attempt
  - [ ] Network escape attempt
  - [ ] Kernel mutation attempt
  - [ ] Evaluator inspection attempt
  - [ ] Confirm all fail safely

- [ ] **Measure true learning**
  - [ ] Compare current system against memory-disabled current model
  - [ ] Compare against previous stable generation
  - [ ] Measure held-out task success
  - [ ] Measure reuse rate
  - [ ] Measure negative transfer
  - [ ] Measure tokens/task trend
  - [ ] Measure calls/task trend
  - [ ] Measure time-to-verified-mutation
  - [ ] Measure capability entropy

- [ ] **Define experiment success**
  - [ ] Held-out success increases or remains stable
  - [ ] Average model calls/task decreases
  - [ ] Average tokens/task decreases
  - [ ] Capability reuse increases
  - [ ] Negative transfer stays below threshold
  - [ ] Capability growth becomes sublinear
  - [ ] Zero canonical-state corruption
  - [ ] Rollback works reliably

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