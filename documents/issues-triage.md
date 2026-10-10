# Issues triage — issues.md vs current tree (2026-10-05)

Triage lane. `issues.md` predates much of the tree; each of the 40 issues
was re-checked against current sources. Verdicts:

- VERIFIED-FIXED — implementation + committed tests observed in-session
- PARTIAL — real coverage exists; the gap listed under "missing" remains
- OPEN — gap confirmed; no implementation found
- SUPERSEDED / DEFERRED / NOTE / PENDING-VERIFY — see reason

Counts at triage time: FIXED 11 · PARTIAL 14 · OPEN 12 ·
PENDING-VERIFY 1 · NOTE 2. Final counts after this lane plus the
concurrent sandbox-lane #1 fix (verified-after below): FIXED 23 ·
PARTIAL 12 · OPEN 2 (#37/#38, deferred by design) · PENDING-VERIFY 1 ·
NOTE 2.

Sibling lanes own: `risk.py`, `workers.py`, `src/worker/*`,
`evaluator/service.py`, `evaluator/README.md`,
`documents/adversarial-report.md` (sandbox); `demo.py`,
`documents/demo-e2e.md`, `artifacts/demo-e2e/**` (demo); `todos.md`
(coordinator, read-only). This lane fixes only inside its allowed paths;
everything else is triage-only by brief.

## Per-issue table

| # | Issue | Verdict | Evidence | Action |
|---|-------|---------|----------|--------|
| 1 | `*read-eval*` unbound in worker read path | VERIFIED-FIXED (sandbox lane; verified-after this session) | First read showed `workers.py:192-199` `graygoo-eval-all` calling `(read in nil in)` unbound; the sandbox lane then bound `*read-eval*` to NIL on the worker read path — re-verified in-session at `workers.py:192-198`. `tests/test_sandbox.py`: 21 tests OK incl. `#.` attack cases; `documents/adversarial-report.md:113-126`. Registry loads already bound NIL (`src/capability/registry.lisp:171,200`). | None — sibling lane owns `workers.py`. |
| 2 | Worker isolation is process-only | PARTIAL | Covered: `sandbox.py` (`refuse_to_rehearse`, `WORKER_PRELUDE`, `WorkerJail`), `src/worker/worker.lisp:138-169` `install-worker-sandbox` (denies file/process/foreign ops, gates REQUIRE, relocks packages; verified SBCL 2.6.9), `effects.py` harness guardrails, `attacks.py` adversarial suite. Missing: OS enforcement — same user, full rights, no job objects/containers; `sandbox.py` and `worker.lisp:82-87` state this honestly. | Triage-only (no allowed path covers OS isolation). |
| 3 | "Prewarmed pool" not persistent | OPEN → FIXED this lane | Was: `workers.py:424-500` `WorkerPool` spawns a fresh SBCL per job (semaphore only). Fix: new `pool.py` long-lived stdio workers. | `pool.py` + `tests/test_pool.py`. |
| 4 | Generation fingerprint too weak | OPEN → FIXED this lane | Was: `workers.py:134-143` `sbcl=<ver>\|asd=<hash>\|epoch=<id>` only. Fix: new `fingerprint.py` (source tree + asd + deps + capability manifest + schema gen + protocol version). NOT wired into `workers.py` (sibling-owned); wiring doc included. | `fingerprint.py` + `tests/test_fingerprint.py`. |
| 5 | `rehearse` orchestration stub | PARTIAL (triage-only) | Covered: `pipeline.py:318-450` `run_candidate` = parse → risk → direct/regression/property/differential/performance, cheap-first, early stop, one compact verdict; `tests/test_pipeline.py`. Missing: plan.md phases B (isolated compile as a stage), F (adversarial inputs), H (state audit) have no pipeline stage; phase I (hidden eval) lives in the `promotion.py` gate instead; Lisp `evo.rehearsal:rehearse` is still a bootstrap stub (`src/rehearsal/rehearsal.lisp:31-35`). | None — assess only. |
| 6 | Risk classification unimplemented | VERIFIED-FIXED | `risk.py` full R0–R6 static scan + cumulative gates + R4-floor conservatism + R6 dispatch-redefinition hardening; `tests/test_risk.py`, `tests/test_qa_risk.py`. | None. |
| 7 | Trusted evaluator stub | VERIFIED-FIXED | `evaluator/service.py` separate-process hidden-test service + `protocol.py` + `hidden_cases.json` + `transforms.py` (6 seeded metamorphic transforms) + transfer gate (`transfer.py`, `promotion.py` gate b). `tests/test_evaluator.py` (moved from `evaluator/` by a sibling lane mid-session): 23 tests OK in-session, also covered by the full-suite run. Note: default corpus is in-checkout (dev); production separation via `GRAYGOO_EVAL_CORPUS` (`service.py:51-55`). | None. |
| 8 | Promotion authority stub | VERIFIED-FIXED | `promotion.py` = generation check → risk/transfer gates → mandatory hidden-evaluator pass (fail closed, no override) → immutable version record + monotonic epoch + ledger event; `tests/test_promotion.py`, `tests/test_qa_promotion.py`. | None. |
| 9 | Context compiler is packaging only | PARTIAL (triage-only) | Covered: `context.py` minimal task context, 8-section priority drop order, hard token budget, never full history; `tests/test_context.py`. Missing: true compression/summarization; token accounting is a documented word-count approximation; ranking lives in `retrieve.py` (composable, not integrated). | None. |
| 10 | Capability retrieval unimplemented | VERIFIED-FIXED | `retrieve.py` `find_capabilities` (keyword + input/output type + effect + reuse, max 8) + `compose_plan` + `find_capabilities_semantic` (offline TF-IDF backend via `embeddings.py`); `tests/test_retrieve.py`. Semantic is TF-IDF, not neural — functional, documented. | None. |
| 11 | Learning loop not operational | PARTIAL (triage-only) | Covered: executable loop wired end-to-end in `demo.py` (retrieve→context→generate→validate→risk→pipeline→repair→patch→transfer); `patches.py`, `transfer.py`, `skills.py`, `lessons.py`, `mine.py`, `replay.py`, `playbooks.py`, `adaptive.py` all exist with tests; `experiments/key_experiment.py` exists. Missing: lesson key experiment not run; "Measure true learning" / "Define experiment success" todos unchecked; L5/L6 partial (see #39, adaptive gaps). | None. |
| 12 | Event ledger in-memory | VERIFIED-FIXED | `events.py` SQLite-backed append-only ledger + hash chain + `verify_chain` + model-call log; `tests/test_events.py`. | None. |
| 13 | todos.md overstates completion | OPEN → FIXED this lane (corrections list; `todos.md` itself is coordinator-owned) | Both directions confirmed: e.g. "Prewarm a worker pool" checked but pool is fresh-process-per-job (`workers.py:424-436`); conversely L4 "decay confidence / counterexamples" unchecked but implemented (`lessons.py:641-760`, `tests/test_lesson_lifecycle.py`). | `documents/todos-corrections.md`. |
| 14 | Roadmap/status docs disagree | PARTIAL (triage-only + corrections list) | Confirmed: `documents/roadmap.md:54-61` "Current status" says structured S-expr output is "next", but `s_expr.py` + `cerebras_client.py` structured mode shipped long ago. `todos.md` mismatches covered by the corrections list (#13). No generated single-source-of-truth exists. | Corrections list covers `todos.md`; roadmap staleness noted for coordinator. |
| 15 | Capability identity tied to symbols | OPEN → FIXED this lane | Was: `capability-id` is `'symbol` (`src/capability/capability.lisp:12-19`); persistence stores name+package (`registry.lisp:82-111`). Fix: additive stable string/UUID ids + display names. | `src/capability/*` (additive). |
| 16 | Dispatch globals race | OPEN → FIXED this lane | Was: `*cells*`, `*current-versions*`, `*latest-epoch*`, `*epoch-snapshots*`, `*epoch-pins*` (`src/kernel/dispatch.lisp:16-40`) updated with no synchronization. Fix: SBCL mutex + `with-dispatch-lock`, additive. | `src/kernel/dispatch.lisp` (additive). |
| 17 | Epoch semantics need integration tests | OPEN → FIXED this lane | Was: no `tests/lisp/` directory at all. Fix: committed SBCL regression probes + README. | `tests/lisp/**`. |
| 18 | Schema rollback unsolved | OPEN → FIXED this lane (design + disabled stubs) | Was: `src/state/schema.lisp` is a version-number stub; no migrations, no compatibility checks. Fix: versioned-migration design + compatibility-check stubs behind explicit `SCHEMA-EVOLUTION-DISABLED`; migration NOT enabled. | `src/state/*` (additive). |
| 19 | Effect isolation not implemented | PARTIAL (triage-only) | Covered: `effects.py` (`EffectGrant`, `OverlayFS`, `RecordedTransport`, `StateSandbox`, `DenyNetwork`) + `sandbox.py` gates + `worker.lisp` denials; failed candidates cannot touch canonical harness state. Missing: raw worker syscalls unconfined (no containers/job objects); email/payment/clock/randomness mediated at harness level only. | None — no allowed path. |
| 20 | Compile safety vs semantic safety | PARTIAL (triage-only) | Covered: hidden cases (`evaluator/`), differential checks (`pipeline.py:183-239`), property/invariant checks (caller-supplied, `pipeline.py:163-166`), wall-clock + heap limits (`workers.py`), static effect gates (`sandbox.py`). Missing: OS-level resource limits (no CPU quota/RSS cap), invariant synthesis (checks are caller-authored), effect validation is static-only. | None. |
| 21 | No adversarial eval-gaming defense | PARTIAL (triage-only) | Covered: `evaluator/transforms.py` 6 seeded semantics-preserving transforms with solver-checked `check_case`; hidden corpus never in `benchmarks/`; `attacks.py` boundary suite. Missing: `evaluator/service.py` evaluates the static corpus per run — no per-evaluation fresh-case generation wired into the promotion path (no transform/random use in `service.py`/`protocol.py`). | None. |
| 22 | Model too involved in own evaluation | PARTIAL (triage-only) | Covered: acceptance dominated by deterministic oracles — pipeline checks, risk gates, hidden-evaluator verdict (mandatory, fail-closed). Model authors candidates + bounded repairs by design; test/check authorship is caller-supplied, not model-certified. Residual: repair `generate_fn` is model-driven (bounded by `max_repairs=1` + oscillation freeze). | None. |
| 23 | No durable experiment baseline pipeline | VERIFIED-FIXED | `benchmarks/runner.py` + `benchmarks/run_bcd.py` (B=text, C=patch, D=dual, two-phase w/ frozen P1 memory) + `metrics.py` recomputation + recorded runs under `artifacts/baseline-a/`, `artifacts/baselines-bcd/` (per-task JSON + summaries + frozen memory); `tests/test_experiments.py`. | None. |
| 24 | No benchmark freeze | VERIFIED-FIXED | `benchmarks/family-a/`: 35 fixed tasks (18 exposure + 8 transfer + 5 adversarial + 4 equivalent-transform) + `README.md` split policy + `recorded/` offline stubs. | None. |
| 25 | No production-grade secret handling | PARTIAL → policy doc this lane | Verified: no credential flow into worker code/prelude — `workers.py` reads only `LOCALAPPDATA`/`GRAYGOO_SBCL`; `cerebras_client.py` never prints the key; `attacks.py`/`demo.py` never touch `.env`. Residual (live): `workers.py:370-376` `Popen` inherits the full parent env, so `CEREBRAS_API_KEY` is visible to worker OS env; fix requires `workers.py` (sibling-owned). `.env` tracked historically (`.gitignore` note) → rotation still pending. | `documents/secret-policy.md`; no code (residual fix is sibling lane's). |
| 26 | No candidate grammar beyond raw forms | PARTIAL (triage-only) | Covered: `s_expr.py` `parse_candidate` enforces `(candidate :target :parent :definition ...)` schema + 100KB / depth-200 limits, rejects malformed before execution; `tests/test_s_expr.py`. Missing: definitions remain unrestricted Lisp forms — no constrained candidate S-expression grammar. | None. |
| 27 | No worker reset protocol | OPEN → FIXED this lane | Was: no reset — fresh process per job was the only hygiene (`workers.py`). Fix: `pool.py` reset/recycle thresholds. | `pool.py` + `tests/test_pool.py`. |
| 28 | No worker health model | OPEN → FIXED this lane | Was: no health checks/poison detection/generation ownership. Fix: `pool.py` health checks + poison detection + generation ownership. | `pool.py` + `tests/test_pool.py`. |
| 29 | No mutation-storm controls | PARTIAL → remainder FIXED this lane | Covered: oscillation detection + target freeze + escalation in `repair.py:107-193` (`detect_oscillation`, `failure_signature`, `_escalation`); `tests/test_repair.py`. Missing (added): freeze after N consecutive failures, cross-task cycle detection, persisted escalation records → new `storm.py` (no duplication of `repair.py`). | `storm.py` + `tests/test_storm.py`. |
| 30 | No capability consolidation | VERIFIED-FIXED | `consolidate.py` dup/overlap/unused detection, merge/generalize proposals, `rehearse_consolidation`, retire/expire/renew, retrieval-quality harness; `tests/test_consolidate.py`. | None. |
| 31 | No lesson validation loop | VERIFIED-FIXED | `replay.py` `ab_replay` (control vs lesson; first-pass/repairs/tokens/time/regressions; promote/reject) + `measure_lesson_effect`; `tests/test_lesson_lifecycle.py`. | None. |
| 32 | No confidence decay | VERIFIED-FIXED | `lessons.py` `decay_confidence` + `record_counterexample` + `refine_or_deprecate` + thresholds; `tests/test_lesson_lifecycle.py`, `tests/test_lessons.py`. (`todos.md` L4 boxes unchecked anyway — see corrections.) | None. |
| 33 | No test-yield learning | VERIFIED-FIXED | `adaptive.py` `learn_test_order` + `test_check_scores`; `tests/test_adaptive.py`. | None. |
| 34 | No TTVM dashboard | PARTIAL → additive extension this lane | Was: `metrics.py:352-368` `time_to_verified_mutation` aggregates `total_ms` only; stage breakdowns documented "optional" but never consumed; `full_report` wires it empty. Fix: additive per-stage breakdown aggregation (context/model/worker/compile/test/eval/promotion). | `metrics.py` (additive) + tests. |
| 35 | Python/Lisp boundary debt | NOTE (triage-only) | Architectural observation, still true: orchestration (pipeline/repair/promotion/retrieval) lives in Python; Lisp owns kernel/dispatch/capability/state substrate; Lisp `rehearse` is a stub. No action in scope. | None. |
| 36 | No "one vertical slice" test | PENDING-VERIFY (demo lane) | `demo.py` wires the full slice per task (retrieve→context→generate→validate→risk→pipeline→repair→patch→transfer) with `--smoke` offline check. Owned by demo lane; not re-verified here. | None. |
| 37 | No shadow execution | OPEN, deferred by design (triage-only) | Confirmed absent; `todos.md:266-274` explicitly gates shadow/canary/schema-evolution on the core experiment succeeding. No action. | None. |
| 38 | No canary promotion | OPEN, deferred by design (triage-only) | Same as #37. | None. |
| 39 | No applicability calibration | PARTIAL (triage-only) | Covered: `skills.py` `match_applicability` (when/when_not) + `record_outcome` (success/failure counts, success_rate) — positive/negative evidence recorded per family. Missing: no automatic boundary learning from outcomes (`adaptive.py` has no applicability learner; when/when_not are static). | None. |
| 40 | Scope creep risk | NOTE (triage-only) | Process advisory. Minimum convincing experiment is defined (`todos.md:276-287`, `documents/roadmap.md:42-52`); post-v1 gating list exists (`todos.md:266-274`). No action. | None. |

## Post-fix counts

Lane fixes turn OPEN→fixed for #3, #4, #13, #15, #16, #17, #18, #27,
#28, and PARTIAL→fixed for #29 (storm remainder) and #34 (TTVM
breakdown); #25 got the policy doc with a live residual left for the
sandbox lane (stays PARTIAL). With the sibling-lane #1 fix
(verified-after): FIXED 23 · PARTIAL 12
(#2, #5, #9, #11, #14, #19, #20, #21, #22, #25, #26, #39) · OPEN 2
(#37/#38, deferred by design) · PENDING-VERIFY 1 (#36, demo lane) ·
NOTE 2 (#35, #40). No OPEN item is unowned.

## Coordinator wiring note (fingerprint → workers)

`fingerprint.py` is deliberately NOT wired into `workers.py`
(sibling-owned). The swap is 3 lines in `workers.py` (full text in the
`fingerprint.py` docstring): import `fingerprint`, and replace the
body of `generation_fingerprint(epoch_id)` with
`fingerprint.compute_string(epoch_id=epoch_id)`. Same signature,
stronger digest; `pool.py` already consumes `fingerprint` for its
default generation.

## Round 3 (2026-10-05): issues #71-79 + #1 reopen + #46

Source: follow-up review appended to `issues.md` (found #71-79 plus 2
reopenings). Coordinator verified every premise against code with
executable probes where applicable.

FIXED this round (fail-closed, with regression tests):

- #71 (CRITICAL): `risk_fn=None` / `unclassified` executed candidates.
  Probed `ok=True` + worker called; now fails at `risk` with zero
  worker use. `pipeline.py`, tests in `test_qa_pipeline.py` +
  `test_pipeline.py` (old fail-open test rewritten to the new
  contract).
- #72 (CRITICAL): `tests={}` / `None` returned `ok=True` with zero
  checks. Now fails at `tests`. `pipeline.py`, tests added.
- #75 (CRITICAL): missing `sandbox.py` silently ran unsandboxed.
  `_sandbox_prelude` now raises when sandboxing is requested but the
  module is absent. `workers.py`, test added.
- #78 (MEDIUM): performance stage passed with samples but no budget.
  Now records `measured: True` and FAILS (measurement is not
  verification). `pipeline.py`, test added.
- #79 (MEDIUM): `StateSandbox.connection` exposed the raw sqlite3
  handle (zero in-repo users). Removed. `effects.py`, test added.
- #1 (reopen, HIGH): interleaved READ/EVAL let an evaluated form
  re-enable `#.` for later reads. `graygoo-eval-all` now reads ALL
  forms under `*read-eval* NIL` before evaluating any. `workers.py`
  template, live SBCL regression test added.
- #46 (P0, kept): worker inherited the full coordinator env. `Popen`
  now uses `_sanitized_env()` (allowlist: SYSTEMROOT/WINDIR/PATH/
  PATHEXT/TEMP/TMP). `workers.py`, tests added; full worker suite
  proves SBCL still starts.
- #74 (HIGH): `(eval '(sb-ext:run-program ...))` slipped the hard
  process deny inside skipped quoted code. `_payload_walk` now scans
  quoted subtrees under eval-like operators (`eval/apply/funcall/
  compile/load`); plain quoted data stays unscanned (no false
  positive). `sandbox.py`, 2 tests added (refusal + no-false-positive).
- #77 (HIGH): performance stage measured spawn+startup, not the
  candidate. Worker template now times the candidate eval in-worker
  and reports `candidate_ms` in the envelope; the performance stage
  prefers it (fallback: driver wall time). `workers.py`,
  `pipeline.py`, 4 tests added (2 live SBCL, 2 preference/fallback).

## Round 4 (2026-10-05): issues #41-70 triage + top fixes

Triage lane checked all 29 against current code (offline probes):
REAL 23 · ALREADY-FIXED 5 (#41, #61, #62, #64, #65) · DUPLICATE 1
(#55→#42) · INVALID 0. Coordinator verified the top items with own
probes and fixed:

- #56 (HIGH): worker envelope spoofable — PROVEN live (`ok=True`,
  `return_value='SPOOFED'` even sandboxed: fake envelope via
  unbound `*terminal-io*` + first-marker-wins + in-image fingerprint
  read). Fixed: bind `*terminal-io*` to captured output + last-pair
  envelope parsing (real prints last). 5 tests (2 live SBCL spoof
  defeats, 3 parser unit). Residual (documented): no in-band secret
  can authenticate when attacker shares image+stream; sleeper
  threads / exotic raw fds remain theoretical post-daters.
- #63 (HIGH): transfer rows caller-fabricated. Fixed:
  `evaluate_promotion(..., transfer_tracker=...)` corroborates every
  row against tracker-recorded `(task_id, helped)`; uncorroborated
  rows reject. Opt-in (legacy callers unchanged); 4 tests.
- #42/#43/#44/#45 (HIGH): registry persistence — package-escaped
  filenames (cross-package collision gone), write-once versions
  (identical re-save idempotent, differing overwrite errors),
  temp+rename atomic writes (`.tmp` invisible to the registry glob),
  FNV-1a integrity hash with v2 format (legacy v1 loads with a
  warning). New SBCL probe `persistence-integrity.lisp` (7 checks);
  all 7 Lisp probes PASS. Hash is corruption-detection, not a MAC
  (stated in code).
- Already-fixed claims accepted on triage evidence (#41 gate logic +
  12/12 suite, #61 wired fingerprint + 12/12, #62 distinct-task
  counting, #64/#65 dashboard-only metrics + dead Lisp).

Round-4 remainder CLOSED: #66-70 all fixed in Round 9 below.
No REAL-but-unfixed items remain from the #41-70 triage.

## Round 10 (2026-10-05): issues #80-110 (scientific validity)

Line 895 of issues.md names #80, #83, #88, #93, #98-100, #109
highest-value; all get verdicts below. Verified against code, not
assumed (each ALREADY-FIXED cites the mechanism + test/probe).

ALREADY-FIXED (6):

- #81 (contamination): transfer-2 (A-TRN-09..16) IS procedurally
  generated — `make_transfer2.py` reference generator, byte-pinned by
  `tests/test_transfer2.py`. A synthetic family exists.
- #83 (zero-call gaming): the gated fast-path fires ONLY on exact
  recorded-input repeats (transfer held-out: 0/32 fires) and
  `documents/repeat-stream.md` scopes the #2/#3 flips to
  repeated-input streams explicitly. Memorization is fenced, named,
  and measured — exactly what the issue asks.
- #84 (normalized growth): repeat-stream reports lib size per
  cumulative check (slopes 0.92 → 0.61) + naive-vs-dedup counts.
  Qualitative "slows" replaced by numbers.
- #92 (price dependence): every usage record carries input/output
  tokens + calls + latency alongside USD (`runner.py` snapshot,
  `cerebras_client.complete`). USD never stands alone.
- #101 (cold vs warm): repeat-stream round 1 (cold, 2.000
  calls/task) vs round 3 (warm, 0.722) on the SAME 18 tasks, twice
  replicated. Clean accumulated-value estimate exists.
- #107 (reproducibility target): `manifest.py` pins python/platform/
  git/SBCL/ASDF-SHA/tree-fingerprint/deps/model/sandbox;
  `documents/reproducibility.md` defines the reproduce recipe.
  Random seeds: temp-0.0 runs replicate bit-identically (B-rerun,
  repeat-stream, transfer-2 revalidation).

MOOT (1):

- #88 (replay overfit): `replay.ab_replay` has ZERO production
  callers (tests only). No live loop validates repeatedly on any
  corpus, so overfit-to-corpus cannot occur. Matters only if/when
  replay drives promotion — recorded as a precondition then.

REAL, fixed this round (docs + offline batch):

- #93 (HIGH, failure criterion): ADDED — `documents/core-experiment-
  gate.md` "Falsification criteria" section: exact numeric bars whose
  breach retires the transfer thesis (docs-only, no code).
- #109 (HIGH, headline experiment): DECLARED — same doc: the
  canonical headline is now "transfer-2 16-task B/D vs A" (11/16 vs
  12/16, sole TRN-08 edge, 4× replicated). One primary result,
  named and cited.
- #102 (catastrophic memory): DEMONSTRATED offline —
  `tests/test_catastrophic_memory.py` (stub): inject wrong patch →
  exactly 1 misfire → hurt row revokes → retire → re-store recovers
  (uses production `PatchMemory`/`consolidate` paths, no new code).
- #103 (retrieval overhead): MEASURED offline —
  `tests/test_retrieval_scale.py` seeds 500 patches, asserts
  retrieval latency bounded generously + reports ms/100-patches;
  numbers recorded in `documents/retrieval-scale.md`.
- #105 (learning efficiency): DEFINED + COMPUTED —
  `metrics.learning_efficiency()` (held-out improvement per extra
  token vs no-memory baseline) + unit tests; computed for B/D (old +
  transfer-2) and repeat-stream in `documents/learning-efficiency.md`.
- #106 (restart durability): PROVEN offline — stream round, close,
  reopen `PatchMemory` on the same dir, repeats still hit
  (`tests/test_repeat_stream.py` addition, stub).
- #108 (artifact bundle): BUILT — `bundle.py` writes one
  `bundle.json` (manifest + summary + git sha + event-log hash) per
  run dir (+ lists member files); `tests/test_bundle.py`.
- #85 (entropy definition): DOCUMENTED — `metrics.entropy_from_counts`
  is Shannon nats over stated units (patch ids / source task ids);
  definition + units + worked example added to
  `documents/metrics-and-success-criteria.md`. No code change: the
  function already implements exactly this.
- #91 (TTVM decomposition): retrieval split out of the context stage
  (`ttvm_sample.py`, timed separately); evaluator/promotion stay
  explicitly UNMEASURABLE (documented in-module: fixed-corpus
  scoring, task samples don't target it). Verified by a 2-sample
  live run ($0.002).

REAL, fixed this round (live batch, ~$0.02):

- #89 + #97 (lesson counterexamples + precision): WIRED —
  `key_experiment` condition C now records per-task lesson outcomes
  (retrieved ids → helped=passed) and counterexamples on failures;
  `lessons.precision()` added (successes/retrievals over usage).
  C rerun (16 calls): first measured lesson precision numbers.
- #95 (forgetting A/B): TTL arm ADDED — `--retire-ttl-rounds K`
  (retire by age regardless of hits) vs usage-weighted; churn
  stream rerun with TTL=1 (~$0.004): lib controlled both ways,
  usage-weighted keeps hot patches TTL drops (numbers in doc).
- #98 (factorial attribution): E=both ARM ADDED — `key_experiment`
  gains condition E (distilled lessons + patch memory), completing
  neither/lesson/capability/both (16 calls): attribution table.
- #99 + #100 (difficulty/order bias): RANDOMIZED stream — repeat
  order shuffled by seed, rerun live (~$0.005): reuse curve
  order-invariant (numbers in doc); difficulty-by-position analysis
  from round-1 data included (fails cluster mid-list, not late —
  no curriculum gradient).

REAL, recorded (needs design/scale/user):

- #80 (HIGH, provider drift): PARTIAL — usage records carry
  provider-reported model id + request_id + finish_reason, and
  sampling params are documented per experiment; but temperature/
  max_tokens/reasoning/date are NOT in the stored record. Fix
  designed (extend `complete()` result + runner snapshot) but
  touches usage-entry keys asserted across the suite — needs a
  careful key-migration pass, scheduled next.
- #86 (lesson confound): MOOT-WHILE-NULL — a handcrafted-guidance
  control only matters when claiming learning; lesson-key is null
  (D=C=B). Reopen if any lesson arm ever beats A.
- #87 (weak transcript baseline): REAL — condition B retrieves
  same-category excerpts with truncation (crude but not verbatim);
  fairer = overlap-ranked like text memory. Needs protocol change +
  rerun; scheduled with the next lesson-key round.
- #90 (first-pass synthesis): REAL — `replay` tracks first-pass per
  replay and `repair_loop` returns repairs_used, but NO loop
  aggregates first-pass/repair counts across runs. Needs an
  aggregating production loop that does not exist yet.
- #94 (cross-family transfer): REAL-BIG — needs a second benchmark
  family (all current families live in Family A). Design task.
- #96 (consolidation ablation): REAL — flat-vs-family retrieval
  comparison unrun. Needs skills-family retrieval harness first.
- #104 (near-match benchmark): REAL — observed instances exist
  (diet ADV-05, TRN-03/04 breakage) but no dedicated task set.
  Deferred: transfer-2 just landed; another task wave is scope
  creep today.

Evidence: full suite green (count at commit), 10/10 probes, new
tests test_catastrophic_memory/test_retrieval_scale/test_bundle +
metric/stream/runner additions, live spend +~$0.025 (session stays
far under $50).

## Round 9 (2026-10-05): issues #66-68

- #66 (MEDIUM): model call records lacked task/run/candidate/
  generation/cost identity. Fixed: `inference-metadata` now carries
  `task-id run-id candidate-id generation cost-usd request-id`
  (`src/model/model.lisp`); probe `model-provenance.lisp` pins the
  contract (7 checks: lineage round-trip, provenance-key presence,
  contract pass/fail, staleness consistency).
- #67 (MEDIUM): world-model projections had no provenance/freshness
  contract. Fixed: every projection carries
  `:derived-through-event :state-generation :active-epoch
  :projected-at` (`src/model/world.lisp`); same probe pins required
  keys + staleness comparison.
- #68 (HIGH): hidden-evaluator verdicts could leak per-case hidden
  outcomes into mutable memory. Fixed: new `detail` protocol field
  (`evaluator/protocol.py`, default `"full"` for trusted offline
  use); `evaluate(..., detail="coarse")` returns verdict + summary
  rates only — no `cases`, no `unexpected_outputs` (the latter
  dropped too: key-probing would enumerate hidden case IDs).
  `promotion._call_evaluator` / `evaluate_promotion` request
  `"coarse"` BY DEFAULT (`evaluator_detail` opt-out for trusted
  debugging). Promotion never persisted per-case blobs anyway (only
  the verdict + check counts in reject reasons); the coarse default
  now also protects the envelope in transit, logs, and future
  callers. 7 tests (5 evaluator incl. a leak scan asserting no
  `HID-A-*` IDs in coarse evidence, 2 promotion default/opt-in).
- Evidence: evaluator+promotion 49/49 OK, 10/10 Lisp probes PASS
  (incl. new `model-provenance.lisp`), ASDF 25 packages.
- #69 (MEDIUM): ASDF claimed zero deps while generations depend on
  Lisp+Python+SBCL+OS+model jointly. Fixed: `manifest.py`
  `collect_manifest()` pins all five (python/platform/git/sbcl/asd
  SHA/source-tree fingerprint/dep versions/model id+prices/sandbox
  posture); `--check` is CI gate 5. Never touches secrets —
  tested: live key absent from rendered JSON. 9 tests +
  `documents/reproducibility.md`.
- #70 (HIGH): agent-checked boxes vs stub code. Fixed by rule +
  mechanism: `.github/workflows/ci.yml` runs the five offline gates
  (suite, smoke, ASDF load, all probes, manifest check);
  `documents/ci-status.md` makes CI the source of truth (boxes check
  only on green gates at that commit; red gate reopens boxes).
  First Actions green run pending next push; local gate evidence
  stands until then. Probe README fixed (10th probe row, "six"→ten).

## Round 8 (2026-10-05): issues #47-48

- #47 (CRITICAL): verified zero code callers — the flag guards
  nothing. No in-image behavioral fix exists (any lock is rebindable
  by definition), so: headers now state NOT-A-BOUNDARY with pointers
  to the real layers (sandbox.py, worker sandbox, workers.py,
  risk.py), and new probe `kernel-stubs.lisp` PINS the limitation
  (rebinding defeats the flag) so nobody builds on it.
- #48 (HIGH): declaration contradictions now rejected
  (`:pure` must be exclusive); unknown keywords still rejected.
  Observed-vs-declared enforcement stays in effects.py (25 tests);
  headers document the split. Probe covers both.
- Evidence: 9/9 probes PASS (incl. new 6-check probe), ASDF 25
  packages. Full Lisp-side enforcement remains milestone-scale
  (brokered handles, OS sandboxing) per adversarial-report.

## Round 7 (2026-10-05): issues #57-60

- #60 (HIGH): "Compile inside rehearsal worker" was checked but the
  hot path only EVALed. Fixed: `graygoo-eval-all` now COMPILEs each
  form and funcalls the closure; compile-time errors fail rehearsal.
  Proven by a compiler-macro discriminator (expands iff compiled)
  plus a defun-under-compile test; all 25 worker tests pass
  unchanged otherwise.
- #57/#58 (MEDIUM, narrowed): triage cited a 4000-char
  `graygoo-prin1` cap that does not exist in the tree; actual
  behavior was silent length-100/level-10 abbreviation. Fixed
  honestly: full-fidelity print (circular-safe) up to a 65536-char
  cap, then abbreviate WITH an explicit `return_truncated` flag
  through the envelope; pipeline comparisons on truncated values
  fail as UNVERIFIABLE instead of mismatching confusingly.
- #59 (LOW): failures carried only condition text. Fixed:
  `run-test-thunk` returns condition-type as a 4th value (5th:
  abbreviated-p; extra values ignored by older 3-value callers in
  pool.py/rehearsal), transported as `error_type` (e.g.
  `TYPE-ERROR`), so classifiers need no stderr regex.
- Evidence: workers 25/25, pipeline 13/13, full suite 648 OK,
  smoke green, 8/8 probes, ASDF 25 packages.

## Round 6 (2026-10-05): issues #52-54

- #52 (MEDIUM): state/promotion-status could contradict. Fixed:
  `make-capability` requires promotion-status at or ahead of state
  in `*lifecycle-states*` order.
- #53 (MEDIUM): parent lineage unvalidated. Fixed: registration
  requires a claimed parent to be registered with a smaller version
  (acyclic + orphan checks); `load-registry` reads all files first,
  then registers in (id, version) order so parents always precede
  children regardless of glob order.
- #54 (HIGH): version branching under concurrency. Fixed:
  `*registry-lock*` (non-recursive SBCL mutex; lock-free `%`
  cores + single-lock wrappers after finding SBCL has no
  `:kind :recursive`), atomic check-and-set registration,
  `next-version`, and `derive-and-register-version` (child =
  max(parent, registered)+1 — concurrent derives fork forward,
  never collide or lose updates).
- Probe `capability-model.lisp` extended (10 new checks incl. 4
  threads x 10 concurrent derivations = versions 1-41 exactly);
  all 8 probes PASS; ASDF loads (`evo-packages=25`).

## Round 5 (2026-10-05): issues #49-51

- #49 (MEDIUM): `derive-version` could not clear list fields
  (`(or ...)` kept parent on NIL). Fixed: supplied-p flags for
  inputs/outputs/effects/dependencies (+ source/creator/model);
  explicit NIL clears, omitted keys inherit.
- #50 (MEDIUM): TTL could not be removed (NIL inherited). Fixed:
  `ttl-given-p`; explicit `:ttl nil` clears.
- #51 (MEDIUM): risk values unvalidated. Fixed:
  `*capability-risk-levels*` + `risk-level-p` (mirrors rehearsal's
  set; kept local to avoid depending on the stub package),
  enforced in `make-capability` (covers derive).
- New SBCL probe `capability-model.lisp` (13 checks); all 8 Lisp
  probes PASS; ASDF system loads (`GRAYGOO-LOAD-OK evo-packages=25`).
  (Triage's `*risk-levels*` pointer named the rehearsal var; the
  capability package cannot use it without a new dependency.)

ACCEPTED, documented, not yet fixed:

- #73 (HIGH) parser differential (Python scan vs SBCL read): real;
  fix is the canonical-emitter architecture the issue proposes —
  project-scale, tracked for the hardening milestone.
- #76 (HIGH) caller prelude runs post-lockdown: order verified;
  reorder is risky (trusted setup may need pre-lockdown authority),
  so the trust boundary is now explicit in the `run_lisp` docstring
  (trusted-kernel preludes only). Full lockdown-last reorder is
  future work.
- TTVM note: `documents/ttvm.md`'s worker share is now directly
  measurable per-check via `candidate_ms`; the sampler still uses
  envelope `elapsed_ms` (upper bound) — a one-line sampler switch is
  future work.

## Round 11 (2026-10-10): the 15 prioritized pipeline issues

The table appended to `issues.md` ("Prioritized issues to add") concerns the
interactive agent pipeline, not the Lisp experiment. Each row now carries a
Status column there. Summary:

- FIXED, 11 rows: unrestricted forms in the warm process (trust lint +
  tamper record), feature completion (visitor checks), integration,
  contract drift (`interfaces.py`), process contamination, edit identity,
  visual-repair regressions (snapshot and rollback), cancellation,
  per-build budgets, concurrency evidence, completion reports.
- FIXED IN PART, 4 rows: authentication (hashing adapter and a check that
  fails plain-text storage; no production KDF), registry relevance (still
  word overlap), visual coverage (no update/delete flows), lifecycle
  economics (command line only).
- Bugs found by the new tests and fixed in the same round: 4 in
  cancellation, 6 in concurrency, 1 in the s-expression reader (a string
  that reads `quote-marker` was taken for the quote sign).
- Still open and unchanged: OS-level isolation of the warm SBCL process (#46).

Verified offline only (scripted model, real SBCL, real headless browser).
None of it has run against the live model yet.

## Round 12 (2026-10-10): evidence that the machinery specialises

Aim: test the claim "the surrounding development machinery can also become
faster through specialization" against the logs of real builds, with no new
model calls.

- Like-for-like (`replay_evidence.py`): 763 logged live replies, each run as
  the model wrote it and again after today's zero-cost repairs. Pass rate
  29.4% -> 39.6% (first drafts 31.8% -> 44.0%). 78 replies pass only with the
  repairs, none passes only without. At the median repair call that is about
  $0.25 and 78 model calls not spent, out of $3.17 and 967 calls.
- From the logs (`specialization.py`): compaction left out 328,357 prompt
  characters (about $0.15, estimate), the kit stood in for 27 helper builds
  (about $0.14, estimate), reuse about $0.08 (estimate).
- NOT shown: a falling cost per saved function over time. Among app-sized
  builds the paid repairs per function fall and the free ones rise since
  Oct 9, but dollars per function do not fall, and the prompts differ
  between periods. The claim holds for "fewer failures reach the model";
  it is not yet shown for "a build costs less".
- Closed from Round 11's open list: method mismatches in `interfaces.py`,
  update and delete visitor checks, dead onclick buttons.
