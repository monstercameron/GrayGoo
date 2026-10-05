Here’s the experiment as an execution-oriented TODO list, ordered to get to a real result as fast as possible.

- [ ] **Bootstrap the runtime**
  - [ ] Create Common Lisp repo + ASDF systems
  - [ ] Standardize on SBCL
  - [ ] Add SLY/Swank development setup
  - [ ] Define packages for kernel, capabilities, workers, evaluation, memory, promotion, metrics

- [ ] **Implement Cerebras/Qwen integration**
  - [ ] Add Cerebras client
  - [ ] Configure Qwen 3.8 27B
  - [ ] Track latency, input/output tokens, cost, request IDs
  - [ ] Add structured S-expression output mode
  - [ ] Reject malformed model output before execution

- [ ] **Build the capability model**
  - [ ] Define `capability-id`
  - [ ] Define immutable capability versions
  - [ ] Add intent, contracts, inputs, outputs, effects, dependencies
  - [ ] Add parent-version lineage
  - [ ] Add TTL and promotion status
  - [ ] Store source + metadata persistently

- [ ] **Implement safe dispatch**
  - [ ] Add explicit `invoke-capability`
  - [ ] Implement versioned dispatch cells
  - [ ] Implement request epochs
  - [ ] Keep old versions alive while active epochs drain
  - [ ] Implement instant rollback

- [ ] **Create the rehearsal system**
  - [ ] Launch isolated SBCL worker processes
  - [ ] Add worker generation fingerprints
  - [ ] Prewarm a worker pool
  - [ ] Add wall-clock timeout
  - [ ] Add CPU/memory limits
  - [ ] Capture stdout, conditions, backtraces, return values
  - [ ] Kill and recycle broken workers

- [ ] **Build the mutation pipeline**
  - [ ] Parse candidate Lisp
  - [ ] Classify mutation risk R0–R6
  - [ ] Compile inside rehearsal worker
  - [ ] Run direct tests
  - [ ] Run regression tests
  - [ ] Run property tests
  - [ ] Run old-vs-new differential tests
  - [ ] Run performance checks
  - [ ] Produce one compact verdict

- [ ] **Create the external evaluator**
  - [ ] Run evaluator in a separate process
  - [ ] Keep hidden tests inaccessible to the agent
  - [ ] Add fresh randomized cases
  - [ ] Add metamorphic/equivalent-input tests
  - [ ] Add adversarial edge cases
  - [ ] Make evaluator verdict mandatory for promotion

- [ ] **Implement green-stop**
  - [ ] Define task success contracts
  - [ ] Stop model interaction immediately when contract is satisfied
  - [ ] Prevent optional refactors after success
  - [ ] Track unnecessary post-success model actions as failures

- [ ] **Add repair mode**
  - [ ] Return minimal failing counterexample to Qwen
  - [ ] Allow one repair attempt by default
  - [ ] Rehearse repair from scratch
  - [ ] Escalate only after bounded repair failure
  - [ ] Detect repair oscillation

- [ ] **Build event logging**
  - [ ] Log every model call
  - [ ] Log every candidate
  - [ ] Log compile/test results
  - [ ] Log promotion/rejection
  - [ ] Log capability invocation/failure
  - [ ] Log rollback
  - [ ] Log task outcomes
  - [ ] Make the event ledger append-only

- [ ] **Build the context compiler**
  - [ ] Generate minimal task context
  - [ ] Include only relevant capabilities
  - [ ] Include applicable contracts
  - [ ] Include recent relevant failures
  - [ ] Include allowed effects
  - [ ] Enforce a context token budget
  - [ ] Avoid raw full-history prompting

- [ ] **Implement capability retrieval**
  - [ ] Search by semantic intent
  - [ ] Search by input/output type
  - [ ] Search by effect requirements
  - [ ] Rank by successful historical reuse
  - [ ] Retrieve 3–8 relevant capabilities max
  - [ ] Attempt composition before synthesis

- [ ] **Create the first benchmark family**
  - [ ] Start with parsing/data transformation
  - [ ] Build 20–50 related tasks
  - [ ] Hold back unseen transfer tasks
  - [ ] Add adversarial variants
  - [ ] Add equivalent transformations
  - [ ] Establish baseline success with no learning

- [ ] **Run baseline A: no persistent memory**
  - [ ] Qwen solves every task from scratch
  - [ ] Measure calls/task
  - [ ] Measure tokens/task
  - [ ] Measure latency/task
  - [ ] Measure success rate
  - [ ] Measure cost/task

- [ ] **Implement executable patch memory**
  - [ ] Persist successful candidate as patch
  - [ ] Give patches short TTLs
  - [ ] Retrieve patches on later related tasks
  - [ ] Record whether reuse helped or hurt

- [ ] **Implement transfer-based promotion**
  - [ ] Require reuse on independent tasks
  - [ ] Track success delta
  - [ ] Track token savings
  - [ ] Track latency savings
  - [ ] Track negative transfer
  - [ ] Promote patch → skill only after transfer evidence

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