# `todos.md` corrections (issues.md #13, #14)

For the coordinator (this lane may not edit `todos.md`). Verified
2026-10-05 against the current tree. Line numbers refer to `todos.md`
as of this date. Each entry gives the box, the evidence, and the
correct state.

Conventions used below: `[x]→[ ]` means "checked but not done";
`[ ]→[x]` means "done but unchecked". "Harness exists, run pending"
means code + tests are committed but no recorded live run was found.

## Overstated: checked but false or partial

1. `Create the rehearsal system / Prewarm a worker pool` [x] (`todos.md:34`)
   — `workers.WorkerPool` spawns a **fresh SBCL per job** (semaphore
   only, `workers.py:424-436`). No persistent workers existed.
   UPDATE this session: `pool.py` now implements long-lived stdio
   workers with recycle/reset/health (`tests/test_pool.py`, 23 tests).
   Correct state: keep `[x]` only with a pointer to `pool.py`, or
   reword to distinguish the one-shot `workers.WorkerPool`.

2. `Create the rehearsal system / Add worker generation fingerprints` [x]
   (`todos.md:33`) — `workers.generation_fingerprint` exists but is
   weak (`sbcl|asd|epoch` only, `workers.py:134-143`); most source
   edits do not move it (issues.md #4).
   UPDATE this session: strong `fingerprint.py` exists (source tree +
   asd + deps + capability manifest + schema generation + protocol
   version) but is **not wired into `workers.py`** (sibling-owned).
   Correct state: partial — `[x]` overstates until the 3-line wiring
   (see `fingerprint.py` docstring) lands.

3. `Create the rehearsal system / Add CPU/memory limits` [x] (`todos.md:36`)
   — only wall-clock timeout + Lisp-heap cap exist; **no CPU quota**
   and **no OS-level RSS cap** (`workers.py:17-29` documents both
   gaps). Correct state: partial.

4. `Create the external evaluator / Add fresh randomized cases` [x]
   (`todos.md:54`) — `evaluator/transforms.py` generates fresh seeded
   metamorphic variants, but `evaluator/service.py` evaluates the
   **static** corpus per run: no per-evaluation fresh-case generation
   is wired into the promotion path (no transform/random use in
   `service.py`/`protocol.py`). Correct state: partial (generators
   `[x]`, promotion-path wiring `[ ]`).

5. `Mine lessons automatically / Ask Qwen for concise candidate lesson per
   pattern` [x] (`todos.md:186`) — `mine.py` uses **deterministic
   templates**; model phrasing is an explicit future hook
   (`mine.py:9,137`). Correct state: `[ ]` (or reword to "template
   lesson statements", which is `[x]`).

6. `Adversarially attack the rehearsal boundary / Kernel mutation attempt`
   [x] (`todos.md:233`) — was checked while the attack was VULNERABLE
   (`risk.py` classified the dispatch redefinition R2, not R6;
   `documents/adversarial-report.md:21`). The per-attack boxes mix two
   semantics ("harness exists" vs "verified fail-safe").
   UPDATE this session (sandbox lane): R6 routing landed
   (`risk.py:300-310`), re-run verdict is SAFE
   (`documents/adversarial-report.md:110`, worker-local caveat).
   Correct state: `[x]` is defensible **now**; recommend defining all
   per-attack boxes as "verified fail-safe" (see #13–#14 below).

7. `Add effect isolation` parent + children [x] (`todos.md:220-226`) —
   `effects.py` (OverlayFS, RecordedTransport, StateSandbox,
   DenyNetwork) + `sandbox.py` gates + `install-worker-sandbox` are
   real, but the module headers state the boundary honestly:
   **process-level harness guardrails, not syscall confinement**
   (`effects.py:3-13`, `sandbox.py:3-13`); raw worker syscalls remain
   unconfined and email/payment/clock/randomness are mediated at the
   harness level only. Correct state: partial — true for
   harness-mediated effects only.

## Understated: unchecked but implemented (and mostly tested)

8. `Implement capability retrieval / Search by semantic intent` [ ]
   (`todos.md:92`) — `retrieve.find_capabilities_semantic` with an
   offline TF-IDF backend (`retrieve.py:38-50`, `embeddings.py`) plus
   `tests/test_retrieve.py`. Correct state: `[x]` (TF-IDF; neural
   embeddings still future).

9. `Create the first benchmark family / Establish baseline success with no
   learning` [ ] (`todos.md:105`) — Baseline A recorded **live** runs
   exist: 35 tasks × raw/stripped under `artifacts/baseline-a/`
   (e.g. `stripped-summary.json`: 29/35 passed, 72 calls, request IDs,
   latency, cost). Correct state: `[x]`.

10. `Add semantic procedural memory / Compare semantic+code memory vs
    code-only memory` [ ] (`todos.md:135`) —
    `experiments/semantic_compare.py` implements the comparison and a
    **live pilot run is recorded**
    (`artifacts/semantic-compare/summary.json`: code-only 6/8 vs
    semantic+code 6/8 over the 8 transfer tasks). Correct state: `[x]`
    (pilot scope).

11. `Seed manual lessons / Measure effect on first-pass success and tokens`
    [ ] (`todos.md:181`) — `replay.measure_lesson_effect` implements
    exactly this measurement (`replay.py`, exercised in
    `tests/test_lesson_lifecycle.py:170-204`). Correct state: code
    `[x]`; a dedicated L2 seeding measurement *run* is still pending.

12. `Validate lessons by replay / Decay confidence on runtime/model upgrades;
    revalidate or downgrade` [ ] (`todos.md:195`) —
    `lessons.decay_confidence` + `DECAY_FACTOR` exist with tests
    (`lessons.py:641-700`, `tests/test_lesson_lifecycle.py:48-101`).
    Correct state: `[x]`.

13. `Validate lessons by replay / Track counterexamples per lesson; refine
    overgeneralized lessons` [ ] (`todos.md:196`) —
    `lessons.record_counterexample` + `refine_or_deprecate` + thresholds
    exist with tests (`lessons.py:701-760`,
    `tests/test_lesson_lifecycle.py:103-167`). Correct state: `[x]`.

14. `Consolidate lessons into principles and playbooks` children [ ]
    (`todos.md:199-203`) — `playbooks.py` implements merge
    (`merge_lessons`), generalize (`generalize_to_principle`), playbook
    compile (`compile_playbook`), and Lisp workflow specs
    (`playbook_to_workflow_spec`) with `tests/test_playbooks.py`.
    Correct state: implementation `[x]` × 4 (all four children);
    production runs pending.

15. `Close the adaptive-development loop` children [ ] (`todos.md:205-208`)
    — `adaptive.py` implements context-composition learning
    (`learn_context_weights`), test ordering
    (`learn_test_order`/`test_check_scores`), repair routing
    (`route_repair`), and escalation policy
    (`learn_escalation_policy`), with `tests/test_adaptive.py`.
    Correct state: `[x]` for those four; `[ ]` correctly remains for
    risk-classification learning, applicability-boundary learning, and
    rollback postmortems (absent from `adaptive.py`).

16. `Run the lesson key experiment` (`todos.md:212-219`) — pilot harness
    (`experiments/key_experiment.py`, conditions A/B/C/D) plus a
    **live pilot run** (`artifacts/lesson-key/summary.json`: A 5/8, B
    6/8, C 5/8, D 6/8). Correct state: harness + pilot run `[x]`;
    condition boxes and `D > C > A` confirmation stay `[ ]` — the
    pilot does **not** confirm the hypothesis (C == A, D == B on
    n=8), and the full experiment is unrun.

17. `Harden worker sandbox / Fix risk classifier: route dispatch/entrypoint
    redefinition to R6` [ ] (`todos.md:259`) — landed in `risk.py`
    (`_is_protected_dispatch_symbol`, `_scan_dispatch_redefinition`,
    `risk.py:300-445`); kernel-mutation re-run verdict is SAFE with
    quoted R6 reasons (`documents/adversarial-report.md:110`).
    Correct state: `[x]`.

## Correctly unchecked (confirmed, no change)

- `Adversarially attack… / Process spawn attempt`, `/ Network escape
  attempt` (`todos.md:230,232`): harness now reports SAFE against the
  documented payloads (Lisp-level blocks, zero packets on re-run;
  `documents/adversarial-report.md:98-111`), but the blocks are
  bypassable in-image with no OS sandbox — keep `[ ]` until the
  sandbox lane declares them fail-safe (or check with the caveat).
- `… / Filesystem escape attempt`, `/ Evaluator inspection attempt`
  (`todos.md:231,234`): re-run verdicts are INCONCLUSIVE and the
  report says to **treat both as still VULNERABLE**
  (`documents/adversarial-report.md:108,111,145-154`). Keep `[ ]`.
- `… / Confirm all fail safely` (`todos.md:235`): report states FAIL
  (narrower). Keep `[ ]`.
- `Harden worker sandbox` remaining children (`todos.md:260-264`):
  Lisp-level pieces exist (denied ops, `refuse_to_rehearse`,
  `WorkerJail`, `$GRAYGOO_EVAL_CORPUS`), but OS enforcement (deny
  ACLs, separate user/host, firewall, job objects) and the all-green
  re-run are missing. Keep `[ ]`.
- `Measure true learning` / `Define experiment success`
  (`todos.md:237-257`): harnesses exist (`metrics.py`, incl. the new
  TTVM stage breakdown; `documents/metrics-and-success-criteria.md`
  defines the criteria), but these boxes are *achieved outcomes*,
  several still TBD for lack of recorded inputs
  (e.g. `metrics.full_report()["time_to_verified_mutation"]` is TBD:
  no TTVM samples recorded yet). Keep `[ ]`.
- `Only after the core experiment succeeds` (`todos.md:266-274`):
  correctly gated. Note: `src/state/schema.lisp` now carries the
  versioned-migration *design* plus compatibility stubs, but
  `SCHEMA-EVOLUTION-DISABLED` is true and migration is refused —
  the `Add schema evolution` box stays `[ ]`.
- `Shadow execution`, `canary promotion` (issues.md #37/#38):
  absent by design per the gate above. No action.

## Stale status prose (not `todos.md`, for the coordinator)

- `documents/roadmap.md:54-61` "Current status" says structured
  S-expression output is "next", but `s_expr.py` +
  `cerebras_client.generate_candidate`/`validate_model_output`
  (`cerebras_client.py:168-250`) shipped long ago. The roadmap status
  section needs a refresh (or deletion in favor of generated status).
