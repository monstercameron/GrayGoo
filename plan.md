# Self-Evolving Lisp Runtime
## Project Specification

**Status:** Draft v1.0  
**Primary implementation language:** Common Lisp  
**Reference runtime:** SBCL  
**Primary inference provider:** Cerebras  
**Primary model:** Qwen 3.8 27B  
**Core design goal:** Build a continuously running Lisp application that can synthesize, test, learn, replace, and consolidate its own capabilities without allowing speculative code to corrupt canonical state or mutate the system’s trust boundary.

---

# 1. Executive Summary

The system is a persistent, self-extending Common Lisp runtime in which an LLM acts as an embedded software engineer.

Unlike conventional coding agents that repeatedly interact with:

```text
repository
→ files
→ editor
→ compiler
→ shell
→ process restart
→ logs
```

this system operates primarily against a live Lisp environment:

```text
intent
→ capability lookup
→ live introspection
→ candidate synthesis
→ isolated rehearsal
→ verification
→ promotion
→ reuse
```

The objective is not merely to let an LLM rewrite code.

The objective is to create a system that **converts expensive model reasoning into tested, reusable executable capabilities**, causing future tasks in the same domain to require progressively fewer model calls, fewer tokens, and less elapsed time.

The system MUST distinguish between:

1. ephemeral experiments;
2. instance-specific repairs;
3. reusable executable skills;
4. generalized procedural families;
5. stable application capabilities;
6. meta-level agent policy changes.

All generated changes MUST be evaluated outside the active application trust boundary before they become canonical.

---

# 2. Primary Research Hypothesis

The project tests the following hypothesis:

> A persistent Lisp runtime with executable procedural memory, isolated speculative execution, transfer-based promotion, and high-speed LLM inference can improve effective application competence over time while decreasing model inference required per successful task.

The desired long-term trend is:

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

The system MUST NOT equate increasing capability count with learning.

A healthy system should eventually learn **fewer, broader abstractions** rather than accumulating a one-function-per-problem memory.

---

# 3. Non-Goals

The initial system is NOT intended to:

- allow unrestricted LLM mutation of its own security system;
- allow generated code to execute directly in the production image;
- replace all source-controlled development;
- infer correctness from LLM judgment alone;
- automatically promote schema migrations without stronger validation;
- maintain an indefinitely growing transcript as memory;
- permanently retain every generated function;
- optimize only for benchmark pass rate;
- grant generated code direct production credentials;
- allow an LLM to alter its evaluator or hidden tests;
- assume that a successful one-off repair is a reusable skill.

---

# 4. Design Principles

The following principles are mandatory.

## 4.1 Speculate freely, commit conservatively

Generated code may experiment aggressively inside disposable environments.

Only the promotion subsystem may alter canonical executable state.

---

## 4.2 Canonical state is never directly exposed to experiments

Experimental code MUST execute against:

- isolated process memory;
- forked or transactional data;
- virtualized external effects;
- restricted filesystem access;
- restricted networking;
- synthetic or shadow credentials.

---

## 4.3 The trust root is not self-modifiable

The running agent MUST NOT modify:

- promotion authority;
- evaluator;
- permission broker;
- resource governor;
- worker isolation mechanism;
- canonical event ledger;
- credential broker;
- hidden benchmark store.

Changes to these components require an external release process.

---

## 4.4 Learning requires transfer

A function that fixes one task is a patch.

It becomes a learned capability only after demonstrating reuse on related tasks.

---

## 4.5 Prefer deterministic computation to model reasoning

If Lisp can answer a question directly, the LLM MUST NOT be invoked.

Examples:

```lisp
(callers-of ...)
(callees-of ...)
(find-capability ...)
(test-capability ...)
(profile-capability ...)
(contract-of ...)
(history-of ...)
```

---

## 4.6 Minimize model context

The agent receives a compiled, task-relevant world model rather than:

- the full source tree;
- the entire transcript;
- entire logs;
- all capabilities;
- historical irrelevant failures.

---

## 4.7 Every mutation is versioned and reversible

Generated code MUST NOT overwrite the only copy of an implementation.

---

# 5. High-Level Architecture

```text
                          EXTERNAL WORLD
                               │
                               ▼
                     ┌───────────────────┐
                     │   GOAL INTAKE     │
                     └─────────┬─────────┘
                               │
                               ▼
                     ┌───────────────────┐
                     │ INTENT NORMALIZER │
                     └─────────┬─────────┘
                               │
                               ▼
                     ┌───────────────────┐
                     │ CAPABILITY SEARCH │
                     └─────────┬─────────┘
                               │
                ┌──────────────┴─────────────┐
                │                            │
         capability exists             capability missing
                │                            │
                ▼                            ▼
         deterministic path        CONTEXT COMPILER
                                             │
                                             ▼
                                      CEREBRAS / QWEN
                                             │
                                      candidate Lisp AST
                                             │
                                             ▼
                                   MUTATION CLASSIFIER
                                             │
                                             ▼
                                  REHEARSAL ORCHESTRATOR
                                             │
                         ┌───────────────────┼───────────────────┐
                         ▼                   ▼                   ▼
                     worker A            worker B            worker C
                     regression          fuzzing             differential
                         │                   │                   │
                         └───────────────────┼───────────────────┘
                                             ▼
                                      TRUSTED EVALUATOR
                                             │
                                  ┌──────────┴───────────┐
                                  │                      │
                                FAIL                   PASS
                                  │                      │
                             discard/repair         registry
                                                         │
                                                   transfer gate
                                                         │
                                                         ▼
                                                PROMOTION AUTHORITY
                                                         │
                                                         ▼
                                                 VERSIONED DISPATCH
                                                         │
                                                         ▼
                                                    LIVE RUNTIME
```

---

# 6. Runtime Trust Domains

The system MUST be separated into explicit trust domains.

## Domain A — Trusted Kernel

Small, externally maintained, non-self-modifiable.

Responsibilities:

- versioned capability dispatch;
- worker lifecycle;
- generation tracking;
- permission enforcement;
- canonical event storage;
- promotion authority;
- evaluator communication;
- credential brokering;
- rollback.

Generated code cannot alter this domain.

---

## Domain B — Active Application

Contains the currently promoted mutable application behavior.

Includes:

- application capabilities;
- business logic;
- learned procedural abstractions;
- adapters;
- high-level workflows.

This domain may evolve.

---

## Domain C — Speculative Workers

Disposable SBCL processes.

They:

- load an exact known generation;
- execute candidate code;
- run tests;
- receive virtualized effects;
- may crash freely;
- may be terminated by the orchestrator.

Nothing in this domain is canonical.

---

## Domain D — Evaluator

Runs outside the active Lisp organism.

Contains:

- hidden tests;
- property generators;
- regression datasets;
- mutation analysis;
- promotion thresholds;
- transfer evaluation;
- performance thresholds.

The self-mutating application has no write access to Domain D.

---

# 7. Mutation Risk Levels

Every proposed change MUST receive a deterministic risk classification.

## R0 — Pure function

Examples:

- parsing;
- transformation;
- calculation;
- ranking.

Requirements:

- isolated compile;
- unit tests;
- property tests;
- performance checks.

---

## R1 — Local mutable state

Examples:

- cache update;
- local session mutation.

Additional requirements:

- state-diff validation;
- replay testing.

---

## R2 — Shared canonical state

Examples:

- user records;
- application state;
- queues.

Additional requirements:

- forked state;
- transaction simulation;
- invariant checking.

---

## R3 — Schema or API contract change

Examples:

- structure layout;
- serialization change;
- capability interface.

Additional requirements:

- migration;
- backwards compatibility;
- contract replay;
- previous-version coexistence.

---

## R4 — External effects

Examples:

- network write;
- email;
- payment;
- filesystem modification.

Additional requirements:

- effect virtualization;
- approval policy;
- shadow or dry-run evaluation.

---

## R5 — Agent policy mutation

Examples:

- retrieval strategy;
- context compiler;
- mutation policy;
- prompting strategy.

Requires:

- offline benchmark;
- control group;
- paired evaluation;
- hidden test suite;
- statistically meaningful result.

---

## R6 — Trusted kernel/evaluator

The agent MUST NOT mutate these components.

---

# 8. Capability Model

A capability is the fundamental learned executable unit.

Example conceptual declaration:

```lisp
(defcapability fetch-customer-history
  (:version 18)

  (:intent
    "Retrieve complete customer history while respecting
     provider pagination and retry requirements.")

  (:inputs
    ((customer-id customer-id)))

  (:returns customer-history)

  (:requires
    (valid-customer-id-p customer-id))

  (:ensures
    (complete-history-p result)
    (ordered-history-p result))

  (:effects
    (:network-read customer-api))

  (:must-not
    mutate-customer
    expose-credentials)

  (:performance
    (:p95-ms 750))

  (:failure-modes
    expired-auth-token
    pagination-cycle
    rate-limit)

  (:implementation
    ...))
```

---

# 9. Capability Metadata

Every version MUST retain:

```text
capability-id
version
parent-version
creation timestamp
creator
model
model parameters
prompt fingerprint
source generation
risk classification
intent
inputs
outputs
preconditions
postconditions
effects
dependencies
state schema compatibility
tests
hidden-eval result
performance metrics
transfer metrics
usage count
success count
failure count
rollback count
latency distribution
resource distribution
promotion status
expiration policy
deprecation state
```

---

# 10. Capability Lifecycle

```text
PROPOSED
   ↓
EPHEMERAL
   ↓
PATCH
   ↓
SKILL
   ↓
PROCEDURAL FAMILY
   ↓
STABLE
   ↓
DEPRECATED
   ↓
RETIRED
```

## Proposed

Raw candidate generated by model.

No reuse.

---

## Ephemeral

Passed basic rehearsal.

May be used inside current task.

Short TTL.

---

## Patch

Validated against current failure.

May persist temporarily.

Still considered task-specific.

---

## Skill

Has been reused successfully on independent related tasks.

---

## Procedural Family

Represents a generalized solution pattern with:

- semantic intent;
- applicability criteria;
- abstract procedure;
- one or more specialized implementations.

---

## Stable

Strong transfer evidence and sustained operational reliability.

---

# 11. Dual Procedural Memory

The system MUST NOT store learned behavior only as executable code.

Each reusable skill consists of:

```text
semantic description
+
applicability conditions
+
abstract procedure
+
contracts
+
known failure modes
+
executable specializations
+
empirical evidence
```

Example:

```text
Skill family:
Resilient Cursor Pagination

Applicable when:
- REST API
- cursor-based pagination
- GET-like idempotent operation

Procedure:
1. request page
2. accumulate result
3. inspect cursor
4. detect repeated cursor
5. retry bounded 429/503
6. terminate on null cursor

Implementations:
- Stripe API specialization
- Salesforce API specialization
- internal CRM specialization
```

---

# 12. Capability Retrieval

Before invoking the LLM, the system searches for existing capabilities.

Search signals:

- semantic similarity;
- matching input type;
- output type;
- effect requirements;
- intent;
- known task family;
- dependency graph;
- historical successful use;
- current runtime compatibility.

Retrieval output SHOULD be small.

Typical context:

```text
goal
3–8 relevant capabilities
relevant contracts
recent applicable failures
available effects
current schema generation
```

---

# 13. Capability Composition

The system SHOULD attempt composition before synthesis.

Example:

```text
Goal:
"Summarize failed invoices."

Available:

LIST-INVOICES
FILTER-FAILED
SUMMARIZE-INVOICES
```

Prefer:

```lisp
(compose
  #'list-invoices
  #'filter-failed
  #'summarize-invoices)
```

over generating another monolithic function.

---

# 14. Context Compiler

The context compiler creates a minimal model-facing state representation.

Example:

```text
GOAL
Fix CSV export for embedded newlines.

RELEVANT CAPABILITY
WRITE-CSV@12

CONTRACT
Output must parse to same row/column values.

CURRENT FAILURE
Embedded LF produces invalid row boundary.

CALLERS
EXPORT-CUSTOMERS

ALLOWED EFFECTS
filesystem-write:/exports

CONSTRAINT
Do not alter public WRITE-CSV interface.

TASK
Return one candidate replacement.
```

The context compiler SHOULD target the smallest context compatible with success.

---

# 15. LLM Interaction Model

Primary model:

```text
Qwen 3.8 27B
via Cerebras
```

The model SHOULD primarily emit:

```text
S-expressions
structured candidate definitions
structured hypotheses
structured repair requests
```

Avoid prose-heavy outputs on the hot path.

---

# 16. Model Output Grammar

Candidate example:

```lisp
(candidate
  (:target write-csv)
  (:parent 12)
  (:reason embedded-newline-handling)

  (:claims
    (preserves-interface t)
    (preserves-roundtrip t))

  (:definition
    (lambda (rows stream)
      ...)))
```

Invalid syntax MUST be rejected before worker execution.

---

# 17. Model Autonomy Gears

## Gear 0 — no model

Use known capability or deterministic composition.

---

## Gear 1 — one-shot synthesis

One model call.

Use for:

- small missing pure functions;
- obvious adapters;
- bounded repair.

---

## Gear 2 — one repair iteration

```text
candidate
→ test
→ concise failure
→ repair
```

Maximum default: one repair.

---

## Gear 3 — exploratory synthesis

Used for ambiguous problems.

May inspect runtime metadata and test alternatives.

---

## Gear 4 — recursive investigation

Only for genuinely difficult tasks.

Must have:

- recursion depth limit;
- token budget;
- call budget;
- wall-clock budget;
- isolated child contexts.

---

# 18. Green-Stop

Once the success contract is satisfied, the runtime MUST stop the agent.

The model does not get to continue because it sees an optional refactor.

Completion is determined by the harness.

---

# 19. Rehearsal Workers

Workers are isolated SBCL processes.

Each worker MUST be associated with an immutable generation fingerprint.

Example:

```text
application-generation: 48
schema-generation: 12
capability-set-hash: 851be...
runtime: SBCL-X.Y.Z
dependency-lock-hash: f91...
kernel-protocol: 4
```

A candidate tested against one generation cannot be promoted into another without rebasing and rerunning evaluation.

---

# 20. Prewarmed Worker Pool

Maintain a configurable pool:

```text
minimum: 4
target: 8–16
maximum: 32+
```

depending on system resources.

Workers SHOULD start from snapshots or preloaded images where practical.

Worker roles may include:

```text
compile worker
regression worker
property worker
fuzz worker
performance worker
state-diff worker
differential worker
```

---

# 21. Rehearsal Phases

A candidate passes through:

## Phase A — structural validation

- parse;
- syntax;
- forbidden forms;
- static effect scan;
- contract presence.

---

## Phase B — compile

Compile candidate in worker.

Reject warnings classified as fatal.

---

## Phase C — direct tests

Run candidate-specific and regression tests.

---

## Phase D — property tests

Check declared invariants.

---

## Phase E — differential testing

Compare candidate with previous stable implementation.

Detect unintended behavior drift.

---

## Phase F — adversarial inputs

Generate:

- boundary cases;
- malformed values;
- empty values;
- large values;
- unexpected ordering;
- random structurally valid inputs.

---

## Phase G — performance

Measure:

```text
wall-clock
CPU
allocation
GC
stack depth
effect count
external request count
```

---

## Phase H — state audit

Compare before/after speculative state.

---

## Phase I — hidden evaluation

Run evaluator-only cases outside the organism.

---

# 22. Effect System

Generated code MUST declare effects.

Example categories:

```text
pure
state-read
state-write
filesystem-read
filesystem-write
network-read
network-write
queue-read
queue-write
email
payment
process
clock
randomness
ffi
```

The runtime maps these to capability permissions.

---

# 23. Effect Virtualization

During rehearsal:

```text
database         → transactional/forked DB
filesystem       → overlay filesystem
network reads    → recorded/stubbed/shadow environment
network writes   → intercepted journal
email            → sink
payments         → simulator
queue            → isolated queue
clock            → virtual clock
random           → deterministic seeded generator
```

Candidate execution returns:

```text
result
+
effect journal
+
state delta
```

Canonical effects are never committed merely because code execution succeeded.

---

# 24. State Model

Canonical domain state SHOULD use:

- transactional databases;
- event-sourced boundaries where valuable;
- explicit schema versions.

A candidate working on state generation N produces a speculative branch.

Example:

```text
canonical
E1 E2 E3 E4

candidate branch
E1 E2 E3 E4
          \
           X1 X2 X3
```

Reject:

```text
discard X*
```

Accept:

```text
apply approved canonical transition
```

---

# 25. Schema Evolution

Schema mutations MUST be first-class operations.

A schema candidate MUST include:

```text
old schema version
new schema version
forward migration
compatibility statement
rollback strategy
affected capabilities
test fixtures
```

A function rollback MUST NOT be treated as sufficient rollback for migrated data.

---

# 26. Versioned Dispatch

Mutable capabilities MUST NOT rely on naive symbol redefinition.

Use explicit dynamic dispatch.

Conceptual model:

```text
CAPABILITY FOO
     │
     ▼
dispatch-cell
     │
     ├── FOO@17
     ├── FOO@18 ← current
     └── FOO@19
```

Call:

```lisp
(invoke-capability 'foo args...)
```

---

# 27. Epoch-Based Promotion

Each request receives a dispatch epoch.

Example:

```text
epoch 104:
FOO = 17
BAR = 9

epoch 105:
FOO = 18
BAR = 9
```

Requests that started under epoch 104 continue using compatible epoch 104 behavior.

New requests receive epoch 105.

Old versions remain loaded until active references drain.

---

# 28. Promotion Authority

Promotion is controlled by trusted code.

Promotion inputs:

```text
candidate ID
source generation
risk class
test evidence
hidden-eval evidence
performance evidence
transfer evidence
state compatibility
```

Promotion authority MUST reject stale candidates.

---

# 29. Transfer Gate

A patch is NOT automatically a skill.

After production or replay use, the system evaluates reuse.

Example:

```text
original task A → fixed by X

related unseen:
B → benefit
C → benefit
D → no regression
E → benefit
```

Only then does X become a skill.

---

# 30. Skill Transfer Evaluation

Track:

```text
reuse count
success delta
latency delta
token delta
failure delta
unseen-task delta
negative-transfer count
```

Skill promotion requires minimum transfer evidence.

Initial suggested thresholds:

```text
minimum independent reuse: 3
negative transfer: 0 severe regressions
held-out success: >= baseline
resource regression: within configured budget
```

Thresholds remain configurable.

---

# 31. Skill Expiration

Default principle:

> Forget unless usefulness is demonstrated.

Suggested TTL:

```text
ephemeral          task lifetime
patch              1–7 days
unproven skill     14–30 days
validated skill    renewable
stable capability  indefinite
```

TTL refresh requires real usage or successful transfer validation.

---

# 32. Capability Consolidation

A periodic maintenance process identifies:

```text
duplicate capabilities
overlapping intents
unused functions
obsolete specializations
excess dependency chains
low-value wrappers
```

Consolidation candidates may:

```text
merge
generalize
alias
deprecate
retire
```

They pass through the normal rehearsal process.

---

# 33. Capability Entropy

Define a metric approximating unnecessary complexity.

Possible factors:

```text
number of capabilities
semantic overlap
unused capability ratio
average call-depth
dependency fanout
average specialization count
duplicate contract ratio
```

Goal:

```text
competence ↑
while
capability entropy grows sublinearly
```

---

# 34. Conditions and Restarts

Common Lisp conditions/restarts SHOULD be used as structured agent feedback.

Instead of:

```text
ERROR: API failed
```

emit:

```lisp
(condition
  rate-limited
  (:retry-after 3)
  (:restarts
    retry-after
    reduce-batch
    use-cache
    abort))
```

Then the model may choose:

```lisp
(restart 'reduce-batch :size 25)
```

This is preferred over asking the model to regenerate an entire solution.

---

# 35. Failure Summaries

Model-facing failures SHOULD be aggressively compressed.

Example:

```text
FAILED PROPERTY
roundtrip(csv(rows)) == rows

COUNTEREXAMPLE
[["a", "x\ny"]]

EXPECTED
2 columns / 1 row

ACTUAL
row split after x

CALLSITE
escape-field
```

Full logs remain available on demand.

---

# 36. Evaluator Design

The evaluator runs outside the mutable runtime.

It owns:

```text
hidden test corpus
fresh property generation
metamorphic tests
benchmark transforms
replay trajectories
promotion thresholds
control-group results
```

Generated code cannot inspect or modify the evaluator.

---

# 37. Anti-Gaming Strategy

Static hidden tests are insufficient.

Evaluation MUST include freshly generated equivalence-preserving transformations.

Examples:

```text
rename identifiers
permute irrelevant ordering
vary semantically equivalent input forms
rescale values
inject irrelevant fields
reorder independent events
change formatting
generate new boundary examples
```

Goal:

detect candidates that optimize for test structure rather than intended semantics.

---

# 38. Control Groups

Every self-improvement experiment SHOULD compare:

### A — learned system

Current system with memory and promoted capabilities.

### B — memory-disabled baseline

Same current model and base runtime without learned capabilities.

### C — previous stable generation

Previous production system.

### D — semantic-memory-only variant

Optional.

Uses procedural descriptions but not executable learned code.

This isolates the value of:

```text
memory
vs
code
vs
model improvement
vs
runtime changes
```

---

# 39. Meta-Improvement Loop

Application capability evolution and agent-policy evolution MUST be separate.

Meta-improvement process:

```text
event log
→ cluster failures
→ formulate one hypothesis
→ produce one policy mutation
→ offline replay
→ paired benchmark
→ hidden evaluation
→ promote/reject
```

Only one conceptual policy variable SHOULD change per meta experiment.

---

# 40. Meta-Mutation Examples

Allowed:

```text
retrieval ranking weight
context selection strategy
failure summarization policy
repair escalation threshold
worker scheduling strategy
prompt template
```

Not allowed in-process:

```text
permission system
promotion authority
hidden evaluator
credential broker
kernel mutation rules
```

---

# 41. Event Ledger

Maintain an append-only canonical event ledger containing:

```text
task started
task completed
model called
candidate generated
candidate compiled
candidate failed
candidate passed
candidate promoted
candidate rolled back
capability invoked
capability failed
transfer succeeded
transfer failed
schema migrated
policy experiment started
policy promoted
```

Events should be immutable.

Derived views may be rebuilt.

---

# 42. World Model

The runtime derives projections from the event ledger.

Examples:

```text
current capabilities
capability reliability
recent failures
skill usefulness
task clusters
dependency graph
state generation
active epoch
mutation history
```

The LLM receives these projections, not raw historical logs.

---

# 43. Persistence Model

Persist:

```text
source-of-truth capability definitions
version metadata
contracts
intent metadata
schema definitions
migration history
event ledger
evaluation evidence
skill families
promotion history
```

The live Lisp image MUST be reconstructible.

The production image itself MUST NOT be the only source of truth.

---

# 44. Recovery

Recovery flow:

```text
start trusted kernel
→ load stable generation manifest
→ restore canonical state
→ replay required event state
→ load stable capability versions
→ verify hashes
→ start application
```

The recovery mechanism should not depend on mutable generated runtime state.

---

# 45. Checkpointing

Maintain:

```text
stable generation checkpoints
candidate generation checkpoints
schema checkpoints
policy checkpoints
```

A checkpoint contains identifiers/hashes, not necessarily giant memory dumps.

---

# 46. Rollback

Support:

## Capability rollback

Move dispatch pointer from:

```text
FOO@18 → FOO@17
```

---

## Generation rollback

Restore full stable capability manifest.

---

## Schema rollback

Only where safe and explicitly supported.

May require forward remediation instead of inverse migration.

---

## Policy rollback

Restore prior policy generation.

---

# 47. Cerebras Integration

Implement a dedicated inference adapter.

Interface:

```lisp
(generate
  :model ...
  :context ...
  :grammar ...
  :max-tokens ...
  :temperature ...)
```

Responsibilities:

- connection pooling;
- request timeout;
- token accounting;
- retries;
- rate limiting;
- structured response validation;
- provider telemetry.

---

# 48. Model Usage Strategy

Prefer frequent short calls.

Typical synthesis target:

```text
input:
~500–2,500 tokens

output:
~100–1,000 tokens
```

Avoid giant contexts except for Gear 4 tasks.

---

# 49. Model Caching

Cache model outputs only where safe.

Possible cache keys:

```text
normalized goal
context hash
capability generation
schema generation
model version
prompt version
```

Never blindly reuse candidates against a changed generation.

---

# 50. Time-to-Verified-Mutation

Primary latency KPI:

```text
candidate request
→ synthesis
→ compile
→ verification
→ evaluator
→ promotion decision
```

Measure individually:

```text
context build
model latency
compile latency
test latency
evaluation latency
promotion latency
```

---

# 51. Parallel Validation

Independent checks SHOULD run concurrently.

Example:

```text
                       candidate
                           │
          ┌────────────────┼─────────────────┐
          ▼                ▼                 ▼
     regression         fuzzing           perf
          │                │                 │
          └────────────────┼─────────────────┘
                           ▼
                        verdict
```

---

# 52. Worker Scheduler

Scheduler input:

```text
candidate risk
available workers
expected test duration
current load
priority
```

Scheduler SHOULD:

- cancel remaining work after decisive failure;
- prioritize cheap high-signal tests;
- reserve resources for production;
- avoid runaway mutation storms.

---

# 53. Mutation Budgets

Every task receives:

```text
max model calls
max output tokens
max candidate count
max worker CPU
max worker memory
max wall time
max repair depth
```

Budgets vary by autonomy gear.

---

# 54. Mutation Storm Protection

The system MUST detect:

```text
rapid repeated failures
same capability repeatedly rewritten
oscillation between versions
exploding candidate count
recursive mutation chains
```

On detection:

```text
freeze target
rollback stable version
escalate for diagnosis
```

---

# 55. Oscillation Detection

Example:

```text
F17 → F18 → F19 → F18-like → F19-like
```

Detect semantically similar back-and-forth mutations.

Response:

- freeze automatic promotion;
- cluster failure modes;
- require a higher-level abstraction change.

---

# 56. Security Model

Generated code gets no ambient authority.

It accesses resources only through injected capabilities.

Example:

```lisp
(fetch-url network-capability url)
```

rather than unrestricted direct socket access.

---

# 57. Credential Broker

Generated code never receives raw permanent credentials.

The broker issues:

```text
scoped
short-lived
operation-limited
environment-limited
```

credentials where external tests require them.

---

# 58. Filesystem Security

Speculative worker default:

```text
read-only runtime image
temporary writable overlay
no host home directory
no SSH keys
no production secrets
```

---

# 59. Network Security

Default:

```text
network denied
```

Rehearsal may selectively enable:

```text
mock endpoints
recorded responses
staging endpoints
approved read-only hosts
```

---

# 60. Resource Limits

Every worker MUST enforce:

```text
CPU time
wall time
memory
open files
process count
network connections
output size
```

---

# 61. Macro Policy

Macros have larger blast radius.

Risk hierarchy:

```text
pure function          normal
method                 normal+
shared class           high
macro                  very high
compiler macro         exceptional
reader macro           exceptional
kernel macro           prohibited
```

Macro promotion requires expanded-code inspection and regression coverage.

---

# 62. Dependency Policy

Generated code SHOULD reuse known stable dependencies.

Adding a new external library is a separate mutation class.

Requirements:

- provenance;
- version pinning;
- license policy;
- vulnerability scan;
- reproducibility.

---

# 63. Reproducibility

Each candidate MUST be reproducible from:

```text
runtime version
dependency lock
generation ID
candidate source
model identity
prompt fingerprint
test seed
test data version
```

---

# 64. Observability

Expose dashboards for:

```text
current generation
active capabilities
candidate throughput
promotion rate
rollback rate
model calls
tokens
latency
worker utilization
task success
transfer success
negative transfer
capability entropy
schema version
mutation freezes
```

---

# 65. Essential Metrics

## Task metrics

```text
success rate
held-out success
latency
model calls/task
tokens/task
cost/task
```

## Learning metrics

```text
capability reuse
skill transfer success
negative transfer
new skills/week
retired skills/week
procedural-family growth
```

## Reliability metrics

```text
rollback rate
production failure rate
state corruption incidents
rehearsal/live divergence
stale candidate rejection
```

## Complexity metrics

```text
capability count
semantic duplication
call graph depth
unused capability ratio
entropy score
```

---

# 66. Core Learning Score

Do not optimize one scalar blindly, but a useful research score is:

```text
             held-out success × transfer success
utility = -------------------------------------------
          cost × latency × regression penalty
```

This metric MUST NOT directly control promotion without individual safety gates.

---

# 67. Primary Learning Criterion

The strongest evidence that the system is learning is:

```text
held-out competence increases
AND
model work per task decreases
AND
capability-library growth slows
```

If capability count rises linearly with task count, the system is likely memorizing rather than abstracting.

---

# 68. Task Execution Algorithm

Conceptual:

```lisp
(defun satisfy-goal (goal)
  (let ((existing (find-solution goal)))
    (if existing
        (execute-plan existing goal)

        (let* ((context
                 (compile-context goal))
               (candidate
                 (generate-candidate context))
               (risk
                 (classify-risk candidate))
               (result
                 (rehearse candidate risk)))

          (cond
            ((green-p result)
             (register-candidate candidate result)
             (execute-ephemeral candidate goal))

            ((and
               (repairable-p result)
               (repair-budget-available-p))
             (repair-and-rehearse candidate result goal))

            (t
             (escalate-failure goal result)))))))
```

---

# 69. Candidate Rehearsal Algorithm

```text
validate structure
↓
assign generation fingerprint
↓
allocate worker(s)
↓
inject candidate
↓
compile
↓
run cheap checks
↓
if fail → stop
↓
parallel deeper evaluation
↓
trusted evaluator
↓
aggregate evidence
↓
verdict
```

---

# 70. Promotion Algorithm

```text
candidate green
↓
generation still current?
    no → rebase
    yes
↓
risk-specific gates satisfied?
↓
state compatibility satisfied?
↓
hidden evaluator passed?
↓
promotion scope:
    ephemeral / patch / skill / stable
↓
create new immutable capability version
↓
publish new dispatch epoch
↓
observe
```

---

# 71. Shadow Deployment

Higher-risk candidates SHOULD support shadow mode.

```text
production request
        │
        ├── stable implementation → real output
        │
        └── candidate             → shadow output
```

Compare:

```text
result divergence
latency
effects
errors
resource consumption
```

Candidate receives no canonical side effects.

---

# 72. Canary Deployment

After shadow success:

```text
1%
→ 5%
→ 25%
→ 100%
```

Promotion authority may automatically rollback on threshold breaches.

---

# 73. Project Modules

Suggested packages:

```text
evo.kernel
evo.dispatch
evo.capability
evo.contract
evo.intent
evo.registry
evo.events
evo.world
evo.context
evo.model
evo.cerebras
evo.worker
evo.rehearsal
evo.effects
evo.state
evo.schema
evo.eval-client
evo.promotion
evo.transfer
evo.memory
evo.consolidation
evo.policy
evo.metrics
evo.security
evo.recovery
```

---

# 74. Suggested Repository Layout

```text
/
├── src/
│   ├── kernel/
│   ├── capability/
│   ├── model/
│   ├── worker/
│   ├── evaluator-client/
│   ├── rehearsal/
│   ├── state/
│   ├── memory/
│   ├── promotion/
│   └── metrics/
│
├── evaluator/
│   ├── hidden-tests/
│   ├── property-generators/
│   ├── benchmarks/
│   └── service/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── adversarial/
│   └── recovery/
│
├── benchmarks/
│   ├── task-families/
│   ├── replay/
│   └── transfer/
│
├── config/
├── scripts/
├── docs/
└── experiments/
```

---

# 75. Storage

Initial recommended technologies:

```text
PostgreSQL:
events
capability metadata
evaluation evidence
task histories

Object storage/filesystem:
candidate source
test artifacts
worker logs
large snapshots

In-memory:
active capability registry
dispatch epochs
hot metrics
retrieval cache
```

The initial implementation MAY use SQLite for development.

---

# 76. Event Schema

Example:

```text
event_id
timestamp
event_type
generation
task_id
candidate_id
capability_id
capability_version
payload
hash
previous_hash
```

Optionally hash-chain critical events.

---

# 77. Model Call Record

Store:

```text
provider
model
request timestamp
latency
input tokens
output tokens
context hash
prompt version
generation
candidate produced
result
cost
```

Do not necessarily retain sensitive raw prompt content indefinitely.

---

# 78. Development Phases

## Phase 0 — Runtime spike

Goal:

Prove live Lisp synthesis works.

Build:

- SBCL process;
- Cerebras adapter;
- structured candidate generation;
- eval in disposable child process.

Acceptance:

```text
model can synthesize a valid function
child process compiles it
test executes
result returns
```

---

## Phase 1 — Capability registry

Build:

- immutable versions;
- metadata;
- contracts;
- explicit dispatch.

Acceptance:

```text
two versions coexist
dispatch changes safely
rollback changes pointer
```

---

## Phase 2 — Rehearsal manager

Build:

- worker pool;
- generation fingerprints;
- compile/test pipeline;
- resource limits.

Acceptance:

```text
bad candidate cannot affect active runtime
stale candidate is rejected
worker crash does not affect coordinator
```

---

## Phase 3 — Trusted evaluator

Build:

- separate process/service;
- hidden cases;
- property tests;
- regression corpus.

Acceptance:

```text
agent cannot inspect hidden suite
promotion requires evaluator verdict
```

---

## Phase 4 — Effect isolation

Build:

- effect API;
- mock network;
- overlay filesystem;
- transactional state.

Acceptance:

```text
failed candidate leaves canonical state unchanged
```

---

## Phase 5 — Procedural memory

Build:

- semantic skills;
- executable versions;
- applicability metadata;
- retrieval.

Acceptance:

```text
previously learned skill solves a later task
without full resynthesis
```

---

## Phase 6 — Transfer promotion

Build:

- patch → skill gate;
- replay tasks;
- held-out related tasks;
- TTL.

Acceptance:

```text
single-instance repair does not automatically
become permanent
```

---

## Phase 7 — Consolidation

Build:

- duplicate detection;
- usage analysis;
- merge proposals;
- retirement.

Acceptance:

```text
system can reduce redundant capabilities
without decreasing benchmark performance
```

---

## Phase 8 — Policy evolution

Build:

- failure clustering;
- one-variable meta experiments;
- paired benchmarks;
- control groups.

Acceptance:

```text
agent policy can improve through externally
verified mutation without altering trust kernel
```

---

## Phase 9 — Shadow production

Build:

- candidate shadow execution;
- divergence metrics;
- canary promotion.

Acceptance:

```text
candidate can run against real request distributions
without affecting outputs or state
```

---

# 79. MVP Scope

The MVP SHOULD deliberately exclude:

```text
schema self-migration
real external writes
payment/email effects
macro self-generation
recursive agents
policy self-modification
```

MVP includes:

```text
pure/local-state capabilities
Cerebras generation
isolated SBCL workers
versioned capability registry
contracts
unit/property tests
rehearsal
promotion
rollback
procedural-memory reuse
basic transfer measurement
```

---

# 80. MVP Demo

A compelling first demo:

## Domain

Data transformation / API-like synthetic tasks.

## Sequence

Task 1:

```text
Normalize records with weird phone formats.
```

System:

- fails;
- synthesizes capability;
- rehearses;
- promotes patch.

Task 2:

```text
Normalize another dataset from same family.
```

System:

- retrieves existing skill;
- adapts or directly reuses;
- uses fewer tokens.

Task 3:

```text
Normalize a related but different format.
```

System:

- recognizes procedural family;
- creates generalized abstraction.

Then show:

```text
Task 1 model calls: 3
Task 2 model calls: 1
Task 3 model calls: 1

Task 20 model calls: 0
```

That demonstrates actual procedural learning better than flashy self-rewriting.

---

# 81. Benchmark Suite

Create several task families.

## Family A — parsing

```text
dates
CSV
nested records
logs
```

## Family B — API workflows

```text
pagination
retry
normalization
caching
```

## Family C — planning

```text
composition of known tools
conditional workflow
```

## Family D — state transformation

```text
inventory
accounts
simple workflows
```

## Family E — fault recovery

```text
timeouts
bad inputs
partial failures
```

Each family should contain:

```text
training/exposure tasks
unseen transfer tasks
adversarial transforms
long-term replay tasks
```

---

# 82. Baselines

Compare against:

## Baseline A

Qwen with no persistent learning.

## Baseline B

Qwen + textual memory.

## Baseline C

Qwen + conventional coding tools.

## Baseline D

Qwen + executable capability memory.

## Baseline E

Full system with transfer-tested dual memory.

---

# 83. Key Experiments

## Experiment 1

Does executable memory reduce inference?

Measure:

```text
tokens/task
calls/task
latency/task
```

---

## Experiment 2

Does dual semantic + executable memory transfer better than executable-only memory?

---

## Experiment 3

Does skill consolidation improve retrieval and reduce negative transfer?

---

## Experiment 4

Does the rehearsal architecture prevent state corruption under intentionally malicious candidate code?

---

## Experiment 5

Does high-speed inference materially improve time-to-verified-mutation, or does validation become dominant?

---

## Experiment 6

Can policy self-improvement outperform the frozen base policy on held-out benchmark families?

---

# 84. Failure Modes to Expect

## Capability overfitting

Generated function solves one case but harms others.

Mitigation:

transfer gate.

---

## Skill-library explosion

Too many narrow functions.

Mitigation:

TTL + consolidation + procedural families.

---

## Evaluator gaming

Candidate optimizes hidden benchmark patterns.

Mitigation:

dynamic metamorphic tests.

---

## State corruption

Candidate writes canonical state.

Mitigation:

effect isolation.

---

## Runtime divergence

Candidate passed against stale generation.

Mitigation:

generation fingerprint.

---

## Semantic hot-swap inconsistency

Existing requests observe mixed versions.

Mitigation:

epochs.

---

## Self-reinforcing bad abstraction

Bad skill gets reused because it is already popular.

Mitigation:

continuous outcome metrics and periodic baseline comparison.

---

## Model over-agency

Agent continues altering already-correct state.

Mitigation:

green-stop.

---

## Repair loops

Model alternates between two broken approaches.

Mitigation:

oscillation detection + escalation.

---

## Context poisoning

Old incorrect conclusions influence future tasks.

Mitigation:

derived world model + evidence-backed state.

---

# 85. Hard Invariants

The runtime MUST satisfy:

```text
1. No speculative candidate can mutate canonical state directly.

2. No candidate can alter its evaluator.

3. No candidate can change promotion authority.

4. Every promoted capability has an immutable prior version.

5. Every promotion is attributable to evidence.

6. Every active generation can be reconstructed.

7. Every external side effect is mediated by a capability.

8. Every schema change is versioned separately from code.

9. A task-level repair is not automatically classified as learning.

10. Model output is never considered trustworthy solely because
    the same model says it is correct.
```

---

# 86. Acceptance Criteria for v1

The system is v1-ready when all of the following are demonstrated.

## Safety

- 1,000 adversarial candidate executions produce zero canonical-state corruption.
- Worker crashes do not crash the coordinator.
- Memory/CPU runaway candidates are terminated.
- Kernel/evaluator mutation attempts are rejected.

## Reliability

- Capability rollback succeeds deterministically.
- Stale generation promotions are rejected.
- Production can recover from stable manifest.

## Learning

Across benchmark families:

- capability reuse rises over repeated tasks;
- average model calls/task falls;
- average tokens/task falls;
- held-out task success does not regress;
- negative transfer remains below defined threshold.

## Performance

- pure candidate rehearsal is fast enough for interactive use;
- warm worker startup is substantially faster than cold startup;
- verification pipeline is parallelized.

---

# 87. Stretch Goals

After v1:

```text
automatic schema evolution
macro synthesis
distributed rehearsal cluster
multiple cooperating models
model-routing by difficulty
formal methods integration
SMT-based invariant checking
proof-producing capabilities
WASM-isolated candidate execution
cross-project learned skill transfer
long-running autonomous application domains
```

---

# 88. Potential Multi-Model Future

Eventually:

```text
fast Qwen
→ routine synthesis / repair

larger reasoning model
→ difficult architecture / abstraction

specialized judge
→ semantic comparison

symbolic verifier
→ formal property checking
```

But v1 SHOULD intentionally use one primary model to make experiment interpretation cleaner.

---

# 89. Open Research Questions

1. What percentage of successful generated patches become genuinely reusable skills?

2. Do semantic procedural descriptions improve transfer over executable code alone?

3. At what library size does retrieval become a bottleneck?

4. How frequently should consolidation occur?

5. Can a learned skill correctly estimate its own applicability?

6. Does Common Lisp's condition/restart system materially reduce repair tokens?

7. How much does worker snapshotting improve mutation throughput?

8. What types of tasks produce the strongest reusable procedural abstractions?

9. Can model inference eventually become a minority of system execution cost?

10. Does meta-level policy self-improvement remain stable over long horizons?

---

# 90. Recommended Initial Technology Choices

```text
Language:
Common Lisp

Implementation:
SBCL

Build/system:
ASDF

Interactive development:
SLY/SLIME + Swank

LLM:
Qwen 3.8 27B

Inference:
Cerebras

Database:
PostgreSQL

Development DB:
SQLite acceptable

Isolation:
separate SBCL processes
Linux namespaces/container sandbox

Metrics:
OpenTelemetry-compatible event export

Tests:
FiveAM or Parachute
+
custom property-testing layer

Serialization:
S-expressions internally
JSON only at external service boundaries
```

---

# 91. First 30 Implementation Tasks

1. Create repository and ASDF systems.
2. Implement kernel process.
3. Implement capability identifier structure.
4. Implement immutable capability versions.
5. Implement dispatch cells.
6. Implement epoch tracking.
7. Implement event ledger.
8. Implement Cerebras API client.
9. Implement structured candidate grammar.
10. Implement Lisp AST validation.
11. Implement worker launcher.
12. Implement worker protocol.
13. Implement worker timeout.
14. Implement worker memory/CPU limits.
15. Implement generation fingerprints.
16. Implement compile rehearsal.
17. Implement basic unit-test rehearsal.
18. Implement property-check interface.
19. Implement trusted evaluator process.
20. Implement promotion authority.
21. Implement rollback.
22. Implement intent metadata.
23. Implement capability retrieval.
24. Implement context compiler.
25. Implement Gear 0/1 execution.
26. Implement green-stop.
27. Implement patch TTL.
28. Implement transfer counter.
29. Build benchmark family A.
30. Produce first end-to-end self-learning demo.

---

# 92. First End-to-End Target

The first meaningful milestone is:

```text
User goal
↓
no suitable capability exists
↓
Qwen generates candidate
↓
worker compiles it
↓
tests fail
↓
Qwen receives minimal counterexample
↓
Qwen repairs it
↓
candidate passes
↓
candidate becomes temporary patch
↓
later related task appears
↓
patch is reused
↓
transfer succeeds
↓
patch becomes skill
↓
future equivalent task executes without Qwen
```

Once this works reliably, the core research thesis is operational.

---

# 93. North-Star Principle

The project's central optimization target is not:

```text
How much code can the model generate?
```

It is:

```text
How much model reasoning can the system permanently
replace with safe, reusable computation?
```

The ideal mature system should increasingly behave like this:

```text
early life:
LLM reasoning dominates

middle life:
LLM synthesis + learned capabilities cooperate

mature domain:
capabilities dominate
LLM handles novelty only
```

That is the core definition of success.

---

# 94. Final Architecture Statement

The system should be understood as:

> A persistent Common Lisp runtime that treats LLM inference as an expensive synthesis resource, converts successful reasoning into versioned procedural abstractions, validates all mutations in isolated execution environments, promotes only empirically useful capabilities, continuously measures transfer and negative transfer, and preserves a non-self-modifiable external trust root governing state, permissions, evaluation, and promotion.

The intended emergent behavior is not unconstrained self-rewriting.

It is **controlled executable learning**.

The runtime becomes progressively more capable while requiring progressively less inference to handle previously encountered classes of problems.

That is the project.