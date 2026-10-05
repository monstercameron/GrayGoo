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

Still REAL, not yet fixed: #47/#48 (Lisp security/effects stubs),
#66-70 (provenance/metrics/source-of-truth).

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
