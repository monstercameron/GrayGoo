# QA / refinement report

Date: 2026-10-05. Lane: qa-reviewer (report fixes, make none).
Scope: root `*.py`, `benchmarks/`, `evaluator/`, `tests/`; SBCL 2.6.9
`graygoo` load. Other lanes worked concurrently; test counts grew
mid-pass (new `test_adaptive/embeddings/experiments/lesson_lifecycle/
metrics/playbooks` files plus new root modules appeared between runs).

Every finding below was reproduced by executing the real code, and
every one has a proving test in `tests/test_qa_*.py` (new files only;
no existing file was edited). `KNOWN-FAIL` = `@unittest.expectedFailure`:
fails today, goes green when fixed. No fix was applied by this lane.

Related prior art: `documents/adversarial-report.md` already reports a
`risk.py` R6 gap for `evo.dispatch:invoke-capability` redefinition
(classified R2). QA-08 below is adjacent but distinct (core dynamic
forms `eval`/`open`/`load` as targets read R0); a bare `dispatch`
target also reads R0 today, corroborating that report rather than
re-filing it.

## Findings (top 12 by severity)

| ID | Sev | Location | Finding | Status | Suggested fix |
|---|---|---|---|---|---|
| QA-01 | Med | `benchmarks/runner.py:202-210` (`compare`, exact path) | Non-string recorded output (e.g. JSON `12345`) raises `AttributeError: 'int' has no attribute 'strip'`, aborting the whole run and discarding all other results | Open, proven (`test_qa_runner.py` KNOWN-FAIL) | Coerce non-string `actual` to `""`/fail the check (json path already does this); see QA-12 |
| QA-02 | Med | `benchmarks/runner.py:308-340,440` (`summarize`/`main`) | Zero selected tasks → `0/0 tasks passed`, exit 0: a typo'd `--only` reports success (`--only NO-SUCH-TASK` verified exit 0) | Open, proven (KNOWN-FAIL) | Return 2 from `main` when the filter selects zero tasks |
| QA-03 | Med | `pipeline.py:432-446` (`run_candidate`) | Unknown stage keys silently ignored: `tests={"directt": [...]}` → `ok=True`, "no checks requested"; caller believes checks ran | Open, proven (KNOWN-FAIL) | Fail (or explicitly record) unknown stage names |
| QA-04 | Med | `pipeline.py:90-119` vs `workers.py:263-278` | Envelope type drift: real worker always reports `return_value` as a PRIN1 string (`"3"`), but `_eval_code_item` compares raw `==`, so numeric `expect` (3) can never pass against the real worker | Open, proven (KNOWN-FAIL) | Normalize the comparison (printed representations) or document string-only expects |
| QA-05 | Med | `promotion.py:59-69` (`_safe_name`) | Filename collisions merge histories: `"a/b"` and `"a_b"` share `a_b.json`; second promote absorbs v1 under the wrong id (verified end to end: one file, `capability_id` overwritten) | Open, proven (KNOWN-FAIL, offline subprocess) | Collision-proof naming (hash-qualified) or reject ids that don't round-trip |
| QA-06 | Med | `promotion.py:171-182,214-220` (`_append_ledger`/`reject`) | Ledger outage escapes as an exception: broken `ledger.append_event` propagates instead of returning the reject dict, so the caller gets no decision | Open, proven (KNOWN-FAIL) | Wrap ledger appends; reject path must always return its dict (fail closed, audibly) |
| QA-07 | Med | `risk.py:304-360` (`_scan_atom`, head-only tables) | `(mapcar open xs)` classifies **R0** "pure computation": dangerous primitives in value position are invisible (only head position is table-checked) | Open, proven (KNOWN-FAIL) | Scan value-position atoms against the opener/dynamic tables |
| QA-08 | Med | `risk.py:463-475` (target scan) | Mutation target naming a core dynamic form (`eval`/`open`/`load`) classifies **R0**; redefining `eval` is not pure computation | Open, proven (KNOWN-FAIL) | Flag targets naming R4-dynamic heads (R3/R4 per spec decision) |
| QA-09 | Low-Med | `promotion.py:96-111` (`_load_capability_doc`/`_load_epoch`) | Corrupt `versions/<cap>.json` treated as "never promoted" → promotes v1 with "all gates passed"; corrupt `epochs.json` restarts epoch at 1 (non-monotonic) | Open, proven (KNOWN-FAIL) | Reject on unreadable state, or promote only with an explicit audited reset reason |
| QA-10 | Low-Med | `pipeline.py:122-139` (`_run_item_stage`) | Empty check list vacuous pass: `tests={"direct": []}` → `ok=True`, "all requested stages passed", `total=0` | Open, proven (KNOWN-FAIL) | Fail (or explicitly skip) stages that run zero items |
| QA-11 | Low | `risk.py:363-414` (`_walk`) | 3000-deep definition raises `RecursionError`, outside the documented `TypeError`/`ValueError` contract (parse-built input is capped at 200 by `s_expr`; direct callers are not; pipeline contains it via its broad catch) | Open, proven (KNOWN-FAIL) | Depth guard in `_walk` raising `ValueError` |
| QA-12 | Low | `benchmarks/runner.py:227-256` (`validate_tasks`) | Schema gate doesn't enforce string types for check `input`/`expected` (numeric values report zero problems), letting QA-01 crash-path fixtures through | Open, proven (KNOWN-FAIL) | Type-check check values in `validate_tasks` |

Severity counts: Medium 8, Low-Medium 2, Low 2. All 12 open (this lane
fixes nothing); each has a failing-or-guarding test committed.

## Coverage gaps (not in the top 12)

- Ledger tail truncation invisible: deleting the last event row leaves
  `verify_chain()` True (no external anchor can fix this in-module;
  needs caller-persisted count/tip hash). Committed as KNOWN-FAIL with
  the limitation explained; all other tamper variants (payload, type,
  timestamp, middle delete, link rewrite, fixed-hash rewrite) verified
  detected by guards.
- `retrieve.compose_plan(max_depth=0)` still returns a direct
  (length-1) hit — BFS honors the depth, the shortcut doesn't.
  Committed as low KNOWN-FAIL.
- `s_expr` number lexer uses Python `int()`/`float()`: `1_000`,
  Arabic-Indic digits, `1e999`→`inf` parse as numbers though the CL
  reader would disagree. Guards document current behavior; info-level.
- `context.compile_context` at extreme budgets retains section headers
  with empty bodies while `sections` still lists them as retained
  (cap itself always holds — verified). Info-level.
- `evaluator.service.evaluate` with caller-supplied empty corpus + zero
  thresholds returns `pass`; the shipped default corpus is non-empty
  and `load_hidden_cases` rejects empties, so only explicit-caller
  misuse. Guarded.
- `transfer.promote_or_hold` with `min_reuses=0` promotes on zero
  evidence — caller choice (default 3), not a code defect. Noted only.
- `contracts.py` (green-stop): raising-predicate and non-mapping
  paths already guarded by existing tests; no gap found.
- Bare-`dispatch` target reads R0 — corroborates the adversarial
  report's R6 dispatch finding; not separately filed.

## Suite health

| Check | Result |
|---|---|
| `uv run python -m unittest discover -s tests` (run 1) | 305 tests, 9.165s, OK |
| Same (run 2, timed) | OK, ~11.1s |
| Same (run 3, after lanes added files) | 387 tests, 10.6s, OK |
| `uv run python -m unittest evaluator.test_evaluator` | 23 tests, 1.54s, OK |
| `runner.py --adapter stub --recorded stub_all_pass.json` | 35/35, exit 0 |
| `runner.py --adapter stub --recorded stub_all_fail.json` | 0/35, exit 1 |
| SBCL `graygoo` ASDF load (2.6.9, temp script) | `GRAYGOO-LOAD-OK`, exit 0, no warnings |
| New QA tests (`tests/test_qa_*.py`, 7 files, 68 tests) | 54 pass + 14 expected failures, ~2s, suite stays green |

- Flakes: none observed (3 full runs green; counts differ only
  because concurrent lanes added test files mid-pass).
- Warnings: none (no `Warning` lines in any run output).
- Slow spots: `tests.test_workers` 8 tests / 6.2s — SBCL-spawn bound
  (~0.25s per cold `run_lisp`; `WorkerPool` prewarms `size` SBCLs at
  construction, no lazy option). `tests.test_promotion` ~2.3s —
  evaluator-subprocess bound. All other files <1s each. One 4.1s
  `test_s_expr` per-file reading did not reproduce (0.063s clean
  rerun); attributed to concurrent-lane contention, not the product.
- Notes: (1) unittest writes progress to stderr, which PowerShell
  surfaces as error-styled text — results above are judged by
  unittest's own `Ran`/`OK` lines. (2) The SBCL load probe needs
  `(require "asdf")` first under `--no-sysinit` (harness detail, not a
  repo bug). (3) No live API calls; no secrets printed; zero repo
  files modified.

## Files created (only writes this lane made)

- `tests/test_qa_runner.py` — QA-01/02/12 KNOWN-FAILs + 3 guards
- `tests/test_qa_pipeline.py` — QA-03/04/10 KNOWN-FAILs + 4 guards
  (incl. live `run_lisp`→`_run_worker` seam proof, 2 SBCL runs)
- `tests/test_qa_risk.py` — QA-07/08/11 KNOWN-FAILs + 10 tricky-candidate
  guards (R5/R6 edges, masking, fail-safe FPs, deliberate R0s)
- `tests/test_qa_events.py` — 8 tamper-variant guards + truncation
  KNOWN-FAIL (limitation)
- `tests/test_qa_promotion.py` — QA-05/06/09 KNOWN-FAILs + 4 fail-closed
  guards (offline evaluator subprocess)
- `tests/test_qa_retrieve_context.py` — max_depth KNOWN-FAIL + 8 budget
  guards (empty/zero/negative/huge)
- `tests/test_qa_sexpr_eval.py` — 18 s_expr/evaluator adversarial guards
  (caps at exact boundary, quote edges, unicode, NaN, wrong types)
- `documents/qa-report.md` — this report
