Yes. I’d make the learning layer a **separate advisory memory system**, not something that directly mutates code. Its job is to capture what the mutation loop learns about *how to develop effectively* and feed only the most relevant lessons back into future synthesis.

Think of it as **developer experience memory** layered beside executable capability memory.

## Learning Layer Goal

The mutation system already learns:

```text
what code works
```

The new layer should learn:

```text
what development strategies work
what failures recur
what fixes tend to succeed
what approaches waste tokens/time
what abstractions transfer
what tests are predictive
what model instructions improve first-pass success
```

So instead of repeatedly rediscovering:

> “Whenever this API returns nested cursors, the model tends to forget cycle detection.”

the system stores that as a reusable lesson.

---

# 1. Learning Artifact Types

I’d separate lessons into five classes.

### A. Failure lessons

Example:

```text
When replacing a capability that consumes cursor pagination,
always test repeated-cursor termination.
```

Derived from repeated failures.

### B. Repair lessons

Example:

```text
CSV newline bugs are usually fixed at field escaping,
not row serialization.
```

These accelerate debugging.

### C. Design lessons

Example:

```text
Prefer composition of existing capabilities over generating
new monolithic workflows.
```

Broader development heuristics.

### D. Runtime-specific lessons

Example:

```text
Do not assume redefining an SBCL function updates previously
inlined callers; route mutable calls through dispatch cells.
```

These encode Lisp/runtime semantics the model has already encountered.

### E. Model-behavior lessons

Example:

```text
Qwen frequently over-refactors after achieving a passing state.
Use explicit "modify only X" instruction and green-stop.
```

This is effectively learning how to use the model itself better.

---

# 2. Keep Lessons Separate from Capabilities

Architecture:

```text
                    EXPERIENCE
                        │
           ┌────────────┴────────────┐
           ▼                         ▼
   Capability Memory            Learning Memory
     "what to run"              "how to reason"
           │                         │
           └────────────┬────────────┘
                        ▼
                 CONTEXT COMPILER
                        │
                        ▼
                       LLM
```

Capabilities solve work.

Lessons steer synthesis.

That separation matters because a heuristic should not accidentally become executable authority.

---

# 3. Lesson Data Model

Each lesson should look approximately like:

```lisp
(deflesson cursor-cycle-check
  (:type :failure-pattern)

  (:statement
   "Pagination implementations should detect repeated cursors.")

  (:applies-when
   (:effects :network-read)
   (:concepts cursor pagination))

  (:evidence
   ((failure task-182)
    (failure task-211)
    (success task-239)))

  (:confidence 0.91)

  (:impact
   (:first-pass-success +0.14)
   (:tokens -430))

  (:created-generation 52)

  (:last-validated-generation 68)

  (:status :active))
```

Important fields:

- lesson ID
- lesson class
- statement
- applicability conditions
- supporting evidence
- counterexamples
- confidence
- measured impact
- creation generation
- last validation
- usage count
- success/failure after retrieval
- TTL/status

---

# 4. Lessons Should Be Derived, Not Just Written by the LLM

A dangerous implementation is:

```text
Qwen fails
→ Qwen explains what it learned
→ explanation becomes permanent memory
```

That will accumulate confident nonsense.

Instead:

```text
events
 ↓
pattern detector
 ↓
candidate lesson
 ↓
replay / validation
 ↓
lesson registry
```

Candidate lessons can be proposed by Qwen, but they need evidence.

---

# 5. Lesson Mining Loop

Run periodically over mutation history.

```text
event ledger
   ↓
cluster related failures
   ↓
cluster successful repairs
   ↓
find recurring causal pattern
   ↓
Qwen proposes concise lesson
   ↓
replay previous tasks with lesson injected
   ↓
compare against control
   ↓
promote / reject
```

This is essentially meta-learning without changing model weights.

---

# 6. Replay Is Crucial

Suppose the system proposes:

```text
"Always use property testing for parsers."
```

Test it.

Replay 50 historical parser mutations:

```text
A = original context
B = original context + lesson
```

Measure:

```text
first-pass success
repair count
tokens
wall time
regressions
```

Only retain the lesson if B consistently improves outcomes.

---

# 7. Lessons Need Applicability Boundaries

The worst learning-memory failure is overgeneralization.

Bad lesson:

```text
Always use immutable data structures.
```

Better:

```text
For speculative parsing transformations where rollback matters,
prefer immutable intermediate structures unless allocation cost
violates the task budget.
```

Every lesson should encode:

```text
WHEN it applies
WHEN it does not
WHY it exists
```

---

# 8. Positive and Negative Lessons

Store both.

Positive:

```text
For pure transformations, differential testing against the
previous implementation is cheap and highly predictive.
```

Negative:

```text
Do not solve malformed CSV by altering row splitting before
checking field escaping.
```

Negative lessons are particularly useful for cutting dead-end exploration.

---

# 9. Maintain a Failure Taxonomy

Create normalized failure classes:

```text
syntax
compile
type/contract
wrong-output
edge-case
state-corruption
effect-violation
performance
timeout
memory
stale-generation
over-refactor
negative-transfer
test-overfit
tool-misuse
context-missing
```

Then lessons can attach to failure classes instead of individual incidents.

---

# 10. Store Mutation Trajectories

Don't just store final success.

Capture:

```text
goal
candidate 1
failure 1
repair 1
failure 2
candidate 3
success
```

Then mine:

```text
which intermediate choices were useful?
which were wasted?
```

This creates a training-like dataset for future inference.

---

# 11. Distill Trajectories Into Compact Lessons

A 6,000-token mutation transcript might produce:

```text
Lesson:
Before rewriting the serializer, inspect escaping behavior.
```

This is exactly the kind of compression that can accelerate future work dramatically.

---

# 12. Context Compiler Integration

The context compiler should retrieve at most a handful of lessons.

Example model context:

```text
GOAL
Fix JSON streaming parser.

RELEVANT CAPABILITY
PARSE-STREAM@8

RELEVANT LESSONS

L17:
Streaming parsers frequently fail at chunk boundaries.
Always include split-token test cases.

L42:
Do not change public parser interfaces during local repairs.

L61:
For pure parser fixes, run old/new differential tests first.

FAILURE
Unicode escape split across two chunks.
```

That's far better than injecting a generic 3,000-token "development guide."

---

# 13. Lesson Retrieval Scoring

Rank lessons by:

```text
semantic similarity
failure-class match
capability-family match
language/runtime match
recent success
confidence
measured impact
negative-transfer history
```

Possible score:

```text
score =
 relevance
 × confidence
 × measured-benefit
 × freshness
```

---

# 14. Limit Lesson Count Aggressively

I'd start with:

```text
0–5 lessons per synthesis call
```

Too many lessons becomes prompt bureaucracy.

If more than 5 look relevant, synthesize a temporary higher-level summary.

---

# 15. Layered Lessons

Have levels:

```text
L0 — universal development principles
L1 — Common Lisp/SBCL lessons
L2 — subsystem lessons
L3 — capability-family lessons
L4 — task-specific hints
```

Example:

```text
L0:
Don't mutate unrelated working behavior.

L1:
Mutable SBCL capability calls must cross dispatch boundaries.

L2:
Network adapters must declare retry semantics.

L3:
Cursor pagination needs cycle detection.

L4:
Provider X's cursor may repeat once after rate limiting.
```

Retrieve from several layers, not just nearest semantic match.

---

# 16. Confidence Must Decay

Lessons shouldn't become scripture.

Example:

```text
confidence = 0.93
```

After runtime/model upgrades, confidence can decay:

```text
Qwen version changed
SBCL version changed
architecture changed
```

Then lesson gets revalidated or downgraded.

---

# 17. Track Counterexamples

Every lesson should explicitly collect failures.

Example:

```text
lesson:
"Use composition before synthesis."

counterexample:
Composition caused 4× latency in extremely hot path.
```

Eventually the lesson might evolve into:

```text
Prefer composition unless the capability sits on a
latency-critical hot path.
```

This is how the memory itself becomes more nuanced.

---

# 18. Merge Lessons Into Principles

You'll get duplicates:

```text
Don't over-refactor after success.
Stop once tests pass.
Avoid unrelated changes after green state.
```

Periodic consolidation should turn them into:

```text
PRINCIPLE P12:
Once the task contract is satisfied, preserve unrelated behavior
and terminate mutation.
```

Then retire the narrower duplicates.

---

# 19. Lesson Lifecycle

```text
OBSERVATION
   ↓
CANDIDATE LESSON
   ↓
REPLAY VALIDATED
   ↓
ACTIVE
   ↓
GENERALIZED
   ↓
PRINCIPLE
```

or:

```text
ACTIVE
 ↓
counterexamples accumulate
 ↓
REFINED / DEPRECATED
```

---

# 20. Separate Facts From Heuristics

This is important.

Fact:

```text
SBCL may inline a function call.
```

Heuristic:

```text
Prefer dispatch cells for mutable capabilities.
```

The system should know which is which.

Facts can be strongly trusted if verified.

Heuristics need ongoing empirical performance evidence.

---

# 21. Add a "Mutation Playbook"

The highest-confidence lessons should compile into task-family playbooks.

Example:

```text
PLAYBOOK: Parser Repair

1. Reproduce smallest failing input.
2. Classify failure:
   lexical / structural / chunk-boundary / encoding.
3. Inspect nearest parser stage.
4. Prefer local repair.
5. Run regression corpus.
6. Run generated boundary cases.
7. Differential-test old/new.
8. Green-stop.
```

Then Qwen doesn't have to reconstruct the debugging strategy every time.

---

# 22. Playbooks Can Become Lisp Workflows

Eventually a stable lesson sequence can itself become executable orchestration:

```lisp
(run-repair-playbook
  :family :parser
  :target 'parse-stream
  :failure failure)
```

Notice the progression:

```text
experience
→ lesson
→ playbook
→ executable workflow
```

That's a second route by which reasoning crystallizes into computation.

---

# 23. Learn Prompt Strategy Too

Track which instruction patterns improve Qwen behavior.

Examples:

```text
"Return one replacement function only."

"Preserve all unrelated behavior."

"Use existing capability X rather than rebuilding it."

"Do not modify public interface."
```

Treat these as experimentally testable prompt lessons.

---

# 24. Learn Context Composition

Record which context elements correlate with success.

Example:

```text
including full source       no improvement
including contract          +19%
including nearest failure   +27%
including three callers     +3%
including 10 lessons        -8%
```

The context compiler can then itself become learned.

This may be one of the biggest performance wins.

---

# 25. Learn Repair Routing

Over time the system can learn:

```text
syntax failure
→ deterministic parser repair

compiler condition
→ show minimal compiler diagnostic

edge-case regression
→ Qwen repair

architecture failure
→ escalate to deeper reasoning
```

Not every failure deserves another full model invocation.

---

# 26. Learn Which Tests Are Valuable

Track:

```text
which test caught the eventual production issue?
```

Then increase its importance.

Tests with almost no unique signal can be deprioritized.

Over time rehearsal ordering becomes:

```text
fastest/highest-yield tests first
```

That reduces time-to-verified-mutation.

---

# 27. Learn Candidate Risk

Compare predicted mutation risk with actual outcomes.

Then improve risk classification.

Example:

```text
functions touching shared cache were classified R1
but caused repeated state contamination
→ family reclassified R2
```

Risk classification itself becomes experience-informed.

---

# 28. Learn Capability Applicability

Every time retrieval chooses a capability:

```text
retrieved
used?
worked?
hurt?
```

Update its applicability boundary.

A skill may evolve from:

```text
handles CSV
```

to:

```text
handles RFC4180 CSV with embedded quotes/newlines
but not streaming chunk input
```

That is substantive learning.

---

# 29. Learn From Rejected Candidates

Rejected code is valuable.

Example:

```text
candidate:
regex parser

rejected because:
catastrophic backtracking on 20 KB input
```

Store:

```text
Avoid regex-based parsing for this grammar family when
unbounded repetitions are present.
```

Otherwise the model may reinvent the same bad approach ten times.

---

# 30. Learn From Rollbacks

Production rollback should be extremely high-value evidence.

Any capability rollback triggers:

```text
postmortem
→ extract lesson candidates
→ replay
→ strengthen evaluator
```

Ideally:

```text
one production regression
→ entire class becomes harder to repeat
```

---

# 31. Learning Layer Architecture

```text
                         EVENT LEDGER
                              │
                              ▼
                      EXPERIENCE MINER
                              │
             ┌────────────────┼────────────────┐
             ▼                ▼                ▼
         failures          successes       trajectories
             │                │                │
             └────────────────┼────────────────┘
                              ▼
                       LESSON SYNTHESIZER
                              │
                              ▼
                        candidate lesson
                              │
                              ▼
                         REPLAY EVALUATOR
                              │
                    ┌─────────┴─────────┐
                    │                   │
                  reject             promote
                                        │
                                        ▼
                              LESSON REGISTRY
                                        │
                           ┌────────────┴──────────┐
                           ▼                       ▼
                     retrieval               consolidation
                           │
                           ▼
                    CONTEXT COMPILER
                           │
                           ▼
                          QWEN
```

---

# 32. Learning Layer Packages

I'd add:

```text
evo.lesson
evo.lesson.mine
evo.lesson.registry
evo.lesson.retrieve
evo.lesson.replay
evo.lesson.consolidate
evo.lesson.playbook
evo.lesson.metrics
```

---

# 33. New Metrics

Track:

```text
lesson retrieval count
lesson usage count
lesson acceptance rate
first-pass-success delta
repair-count delta
token delta
latency delta
lesson negative-transfer
lesson precision
lesson redundancy
lesson lifespan
```

And especially:

```text
development acceleration
=
baseline time-to-verified-mutation
/
lesson-assisted time-to-verified-mutation
```

If it's 2.0, the learning layer doubled development speed.

---

# 34. The Key Experiment

Run every task under one of:

```text
A — no lesson memory
B — raw previous transcripts
C — retrieved distilled lessons
D — retrieved lessons + executable capability memory
```

Measure:

```text
first-pass success
repair loops
tokens
LLM calls
time-to-verified-mutation
held-out success
```

My expectation is:

```text
D > C > A
```

and raw transcripts may surprisingly perform worse than distilled lessons.

---

# 35. Implementation Order

I'd bolt this onto the main project only after event logging and basic mutation work.

### Phase L1 — capture

- [ ] Store complete mutation trajectories
- [ ] Normalize failure classes
- [ ] Store repair outcomes
- [ ] Track successful/failed strategies

### Phase L2 — manual lessons

- [ ] Create lesson schema
- [ ] Manually seed 10–20 lessons
- [ ] Retrieve relevant lessons into context
- [ ] Measure effect

### Phase L3 — automatic mining

- [ ] Cluster recurring failures
- [ ] Ask Qwen for candidate lesson
- [ ] Attach source evidence
- [ ] Deduplicate candidate lessons

### Phase L4 — replay validation

- [ ] Historical A/B replay
- [ ] Compute impact
- [ ] Reject harmful lessons
- [ ] Promote useful lessons

### Phase L5 — consolidation

- [ ] Merge overlapping lessons
- [ ] Generate principles
- [ ] Build playbooks

### Phase L6 — adaptive development

- [ ] Learn context selection
- [ ] Learn test ordering
- [ ] Learn repair strategy
- [ ] Learn escalation policy

---

# 36. The Broader Loop

With this layer, the whole system becomes:

```text
                TASK
                  │
                  ▼
       retrieve capabilities
                  +
          retrieve lessons
                  │
                  ▼
                QWEN
                  │
                  ▼
              mutation
                  │
                  ▼
              rehearsal
                  │
       ┌──────────┴───────────┐
       ▼                      ▼
     success                failure
       │                      │
       └──────────┬───────────┘
                  ▼
             experience
                  │
            ┌─────┴──────┐
            ▼            ▼
       capability      lesson
         learning       learning
            │            │
            └─────┬──────┘
                  ▼
           future context
```

That gives you two complementary learning loops:

> **Capability learning reduces how much work the model must perform.**

> **Lesson learning makes the remaining model work more effective.**

Together, those are much stronger than simply letting the runtime accumulate generated functions.

The north-star for this side layer should be:

> **Does each month of mutation history make the next mutation require fewer attempts, less context, and less inference while preserving or improving correctness?**

If yes, then the system isn't only learning the application domain—it is learning **how to develop itself more efficiently**.