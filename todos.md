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
  - [x] Decisive experiment: 7 held-out reuse tasks added (validated on hand + frozen learned registries)
  - [x] Multi-order replication: stub A/B/C/D × 5 orders × 2 families, identical routing
  - [x] Rising tasks/capability at flat 13 artifacts (W 2.0→3.0, R 1.4→2.2) via --ids subset curves
  - [x] Live A/B/C/D both families: D beats A on success at 1/4–1/6 the calls; 15 zero-LLM solves
  - [x] Pinned-test expectations updated to extended transfer sets (all verified, suite green)
  - [x] Precision: mined veto words + 3 adversarial traps (misfire→abstain→adapt, transfer unchanged)
  - [x] Scaling: evidence pre-filter + distractor proof (identical routing at 513 caps, linear ms)
  - [x] Compression: generalize_pair collapses csv siblings into 1 parameterized cap (14→13, trap graduates)
  - [ ] Automatic sibling detection + flag-spec proposal (generalize_pair still takes a caller spec)
  - [ ] N-way parameters (single alt value only today)

- [x] **Interactive agent dashboard (2026-10-06)**
  - [x] Prompt -> model-written Lisp tool + tests -> SBCL REPL -> saved registry (`agent_session.py`, `dashboard/agent.js`)
  - [x] Every prompt also answered with an empty registry (measured no-memory baseline)
  - [x] Zero-token exact-repeat cache, short quick-reuse prompt, allow-listed "call a saved tool" box
  - [x] Harness hardening from real live failures (auto-quoted list literals, paren completion, answer-call repair, actual-vs-expected repair feedback)
  - [x] Held-out prompts scored by an independent Python oracle (10 prompts)
  - [x] Recorded live evidence shipped in `dashboard/evidence/`; capped repeat runner `evidence_run.py`
  - [x] Thesis tab explaining the idea, the tool and its limits
  - [x] Independent reference oracle (FNV-1a 32/64, CRC32, Adler-32, MD5, SHA-1/256): guessed hash vectors are overruled by Python, with provenance (`oracle.py`)
  - [x] Frozen oracle: expectations that passed or came from a reference cannot change; contradictory tests are rejected (SPEC_INCONSISTENT)
  - [x] Failure classes (COMPILER_ERROR, RUNTIME_ERROR, IMPLEMENTATION_WRONG, TEST_WRONG, AMBIGUOUS, REPEATED_CANDIDATE) and oracle confidence; mutation stops when the oracle is suspect (property-test rescue)
  - [x] Recurring Lisp slips persisted as lessons and injected into later prompts; syntax-only repair for compile errors; identical failed candidates skipped, not re-run
  - [x] Repair-efficiency metrics (repeated errors, wasted tokens) and a per-failure postmortem JSON in `artifacts/agent/postmortems/`
  - [ ] Reference oracles beyond hashes (parsers, date math, number formatting) so more exact vectors can be machine-checked
  - [ ] Promote validated sub-facts (e.g. FNV offset basis and prime) into reusable knowledge instead of rediscovering them per run
  - [ ] Semantic task check: a "hash-password" request that yields a fast non-cryptographic hash should be flagged as unsafe for password storage
  - [x] Typed Lisp REPL box: strict allow-list evaluator (saved tools, math, lists, lambda, let, if); I/O, eval, funcall, quoted-symbol function tricks and reader syntax are rejected before running
  - [x] 20 held-out prompts with independent Python oracles (10 harder); `evidence_run.py --temperature 0.7 --held 20` measures variance
  - [ ] Run the 20-prompt, sampled-temperature evidence live (needs a live-spend approval) and ship it in `dashboard/evidence/`
  - [ ] OS-level worker sandbox (job object / jail) so the typed REPL could be widened beyond the allow-list
  - [ ] Larger held-out set and confidence intervals (current: 10 prompts, a few runs)
  - [ ] Persistent REPL image instead of one SBCL process per eval
  - [x] Model-slip guards from the live blog runs (2026-10-09): data lists with string heads auto-quoted, dropped paren in nested test data restored by arity, surplus `)` trimmed, unusable tests pruned, missing-quote JSON repaired, JSON retries at rising temperature, unreadable reply costs one attempt not the session
  - [x] `TEST_CALL_INVALID` failure class: a broken test call is repaired without rewriting the definition
  - [x] House style for built tools (`lispstyle.py`): pure single-purpose functions, auto docstring, hard rules against global state, printing, destructive ops on arguments and case-insensitive secret comparison
  - [x] Projects (`projects.py`): separate programs with their own tool registry and prompt log; create, rename, switch, delete (to trash) in the dashboard; follow-up prompts refine a project
  - [x] Regression guard: replacing a saved tool is refused when a tool that calls it would fail its tests
  - [x] Mounting (`mount.py`): the harness serves a project's pure `(handle-request request state)` on a local port and persists the returned state in SQLite; request log and report; Run server control in the dashboard
  - [ ] Live-verify a web app built end to end against the mount contract (needs a live-spend approval)
  - [ ] Verified password hashing for mounted apps (a Lisp SHA-256 checked by the reference oracle, or a harness-supplied primitive)
  - [x] Keep one SBCL process alive per mounted app (`lispserver.py`): about 0.1 ms per call instead of about 130 ms; fresh-process fallback
  - [x] Command-line apps: `(handle-command args state now)` contract, runner (`mount.py --shell`, dashboard command box), state helpers seeded
  - [x] Function metadata for the graph (`toolmeta.py`): effects, inherited effects, latency kinds, measured CPU time
  - [x] Live build graph as the page centre: fixed canvas, layered layout, effect icons and legend, graph beside the live log in one viewport
  - [x] Learning ledger: in-run mistakes first, per-project relevance, warning effectiveness, unhandled-error list (`logreview.py --learning`)
  - [x] Leftover functions of abandoned plans are retired (kept on disk) before an app build
  - [x] One-command start (`start.py`, `start.cmd`); mounted apps are remembered across restarts
  - [x] Shorter sectioned system prompt; focused registry and brief lessons on non-planning calls
  - [x] Plan steps that do not call each other are built concurrently in lanes; a step waits only for the planned functions it calls (`tests/test_parallel_thinking.py`)
  - [x] Shared rate-limit gate (`modelgate.py`): a 429 halves the number of simultaneous model calls and pauses new ones for the Retry-After time; the limit grows back after a quiet spell
  - [x] Thinking calls are back on for the plan of a large goal, plan extension, step splitting and a step's last rewrite, with an 8000-token budget and a no-thinking fallback when the reply is empty or cut off
  - [x] Run meter in the graph header: wall clock of the build and a gauge of the model's average tokens per second
  - [x] One warm SBCL process per build session takes newly saved functions incrementally (`LispServer.extend`)
  - [x] A project is shown and built with the Live model only: the scripted demo model is refused outside Scratchpad, so its example functions can no longer be filed under a real project (`tests/test_projects.py`)
  - [x] A project's prompt log shows its own prompts for the chosen model; the recorded evidence run is Scratchpad's opening view only
  - [x] Project chips count the project's own functions (no kit helpers, no retired leftovers)
  - [x] Lanes and thinking ran live (6 pcrm builds, all succeeded, no 429s): thought-out plans answered in 1-3.4 s, lanes peaked at 5 calls in flight
  - [x] A test call of the form `(let ((r (tool ...))) body)` with a miscounted paren run is rebalanced from the tool's arity (10 failed verdicts in the first live run; replayed: 0)
  - [x] A broken test call is repaired as a test call (code kept); thinking is spent only when the code itself is wrong, with a 5000-token budget
  - [x] The plain rewrite prompt no longer asks the model to reason before answering (that wording drew empty replies); an empty reply is asked again unchanged
  - [x] A planned step named after a saved tool is told it CHANGES that tool; a change the model declines is reported (`step_kept`), not counted as done
  - [x] Screenshot check: a finished web app is walked like a visitor (`visualcheck.py`), rendered in headless Edge (`screenshot.py`), and the pictures go to the model with the goal; one round of fixes, then a second look (`tests/test_visual.py`, `tests/test_screenshot.py`)
  - [x] Live-verify the screenshot check: that the API accepts the pictures as sent, what a look costs, and how often its verdict is right (needs a paid run)
  - [x] The screenshot check ran live (6 looks over 4 builds): the API takes the pictures as sent, a look costs about $0.004-0.006 and 2-6 s, and it found real faults (stray n characters, raw CSS as text, unstyled pages)
  - [x] Cancel a running build: `Session.cancel`, `POST /api/agent/cancel`, a Cancel button; saved functions are kept and the no-memory twin is skipped (`tests/test_round9.py`)
  - [x] The Continue box is hidden while a build is running, also after a reload
  - [x] A hung model call is given up after 30 s and asked again at once (two builds lost a minute each to a 60 s wait); a thinking rewrite gets 8 s before the plain answer is used
  - [x] Replies may be 4500 tokens long (9 page replies were cut off at 2200 and asked again)
  - [x] `\n` inside a Lisp string becomes a line break and commas between CSS rules are dropped, in new builds and (once, with backups) in the saved pcrm functions
  - [x] A failed round of screenshot fixes no longer fails the build; fix steps are told what the pictures showed; fixing goes on only while each look finds fewer problems (at most 2 rounds)
  - [x] Leftover headless-browser profile folders are removed in the background and swept once per process
  - [x] Context compaction: every call's REGISTRY block is fitted to a budget (tools the call names keep a full line, the rest shrink to signatures; kit helpers to one line), the project note is one sentence outside planning, a failure is reported once (`compact_registry`, `tests/test_round10.py`). Replaying the logged prompts of four live builds: 37% less prompt text
  - [x] Short replies: a repair may be a few small edits or leave out unchanged tests, and test repairs return only tests (`compaction.py`, `tests/test_compaction.py`)
  - [x] Every Lisp evaluation of a build goes through the session's one warm REPL: regression checks and answer calls no longer start a process each (was 122 ms each, 69 of them in four builds)
  - [x] The CSS-comma repair is linear-time: its first version backtracked without end on a long stylesheet and froze the dashboard during a live build (a 4000-rule test guards it)
  - [x] Live-verify compaction: input tokens per call, how often the model answers a repair with an edit and how often the edit applies, and that first-try pass rates did not drop with the shorter registry
  - [x] Compaction ran live (2 builds): 34-39% less prompt text, 9 of 19 repairs sent as small edits (8 applied), regression checks 8 ms instead of seconds
  - [x] Stylesheet and pages agree on class names: page steps are told the classes the stylesheet defines, the stylesheet step the classes the pages use, the planner both plus the classes with no rule; a stylesheet that does not cover the pages' classes is not saved (`_style_problem`); after a build the stylesheet is brought up to date (`_style_repair`) (`tests/test_round11.py`, `tests/test_round12.py`)
  - [x] A single function built straight from the plan call in an app is followed by the app check, the stylesheet repair and the screenshot look
  - [x] Plain calls no longer sample above temperature 0 (17 empty replies in the logs, all sampled); a reply cut off at the token limit is asked again instead of patched; definitions are capped at 7000 characters
  - [x] State split over extra name/rows arguments is folded into one state; a doubled percent in a string is one; the cause of a compile error reaches the model (it used to see only the printed form)
  - [ ] Live-verify the class agreement: that a styling prompt on pcrm (20 page classes without a rule at the time of writing) ends with every used class styled, and what the lint costs in repair calls
  - [ ] Live-verify this round: the timeouts, the longer replies and the two-round screenshot loop (needs paid runs)
  - [ ] The planner still renames functions across follow-up prompts (render-products-page next to products-page-html); the note against it is new and unproven
  - [x] Password hashing for mounted apps: `hash-password` and `password-matches-p` in the web kit, required by the contract, and a free check that fails a build whose password is readable in its users table (`tests/test_webkit_auth.py`)
  - [x] The 15 prioritized pipeline issues of `issues.md` (2026-10-10): each row there has a Status; summary in `documents/issues-triage.md`, Round 11
  - [x] A finished app is tried like a visitor without a model call (`acceptance.py`, 9 checks) and its functions are compared with each other (`interfaces.py`); what fails gets one guarded round of fixes
  - [x] A round of fixes is undone when the app answers worse, a passing check fails or the screenshots show more problems (`visual_rollback`)
  - [x] One build stops at $0.40 or 300 s and says which limit it reached; Continue is the go-ahead to spend more
  - [x] Candidates may not touch functions they do not own (`lispstyle.trust_problems`); the warm process detects a changed trusted function, restores it and restarts (`tamper`)
  - [x] Cancel is honoured at every stage (4 bugs fixed, `tests/test_cancel_stages.py`); registry, request log, project and app-server races fixed (6 bugs, `tests/test_concurrency_evidence.py`)
  - [x] The end-of-run card is never squeezed away: short by default with "Show full report", and "Checked by the harness itself" at the top
  - [ ] Live-verify this round on pcrm: the free checks report seven real faults there (logout does not end the session, plain-text password, Edit and Delete buttons that call functions no script defines, a form posting to a missing route, a POST form and a GET link the router does not handle); follow-up prompts should clear them (needs paid runs)
  - [x] `interfaces.py` reports a form or link that uses a method the router does not handle for that path (`method-mismatch`, 16 tests)
  - [x] Visitor checks for update and delete flows (they run on a throwaway state, so delete is safe to click); a button whose onclick calls a function no script defines is reported as a fault
  - [x] Machinery evidence, like-for-like: `replay_evidence.py` runs every logged live reply as written and again after today's free repairs (763 replies: 29% pass as written, 40% after; first drafts 32% to 44%; 78 rescued, none broken)
  - [x] Machinery evidence from the logs: `specialization.py` (what each mechanism saved, measured or labelled as an estimate; periods over app-sized builds) and a Thesis tab section "The machinery specialises too" fed by `GET /api/agent/machinery`
  - [ ] The logs do NOT yet show a saved function getting cheaper over time: among app-sized builds, paid repairs per function fell from 2.53 to 1.54 since Oct 9 while free repairs rose from 0.73 to 1.05, but dollars per function stayed near $0.013. A fixed set of prompts rebuilt after each harness change would settle it (needs paid runs)
  - [ ] Re-run `uv run python replay_evidence.py` after new live builds or new normalisers (about 3 minutes, no model calls); the dashboard shows the saved result
  - [ ] Show `economics.py` per-project figures in the dashboard (command line only today)
  - [ ] A production key-derivation function for mounted apps (salted SHA-256 with 1,000 rounds is a demo-grade stand-in)
  - [ ] Live-verify lanes and thinking over more builds: wall time per app build, 429 count, and whether thought-out plans need fewer repairs (needs a paid run)
  - [ ] Watch a live build to confirm the node spawn and edge-draw animations (not observable in the automated browser pane)
  - [ ] Live-verify the shortened system prompt and the cheaper rewrite settings against first-try pass rate
  - [ ] Show `lispstyle.style_notes` per tool in the tools table

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