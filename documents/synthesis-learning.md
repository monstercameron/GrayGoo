# Synthesis learning: from hand-written seeds to distilled capabilities

Session goal 2026-10-06: stop hand-writing capabilities; exposure tasks go
through Qwen synthesis → rehearsal → verification → registration; the SAME
held-out Family-R/W transfer tasks must then solve with zero-LLM
reuse/composition from actually-synthesized capabilities.

## Pipeline (`distill.py`)

Per exposure task: Qwen writes `solve/applies` + `# TYPES` + `# SIG`
(six words) → AST gate (pure-stdlib subset) → subprocess rehearsal
(`applies` first, `solve` only where it fires, per-index errors) →
verification (own checks + cross-task negatives through the full runtime
gate: category + prompt overlap + `applies`) with repair feedback loop
(escalating temperature 0.2→0.9, max 6 attempts) → registration as
`ExecCapability` → consolidation (harm quarantine, then greedy set-cover).
`load_learned` re-gates every source on load. Trust boundary preserved:
untrusted model code runs in-process only after gate+verify.

Live distill cost: 28 attempts, $0.044. Result: **13/13 distilled and
kept** (8 Family-W + 4 Family-A exposure procedures + synthetic
table-csv inverse), 0 dropped, 0 quarantined, 0 uncoverable in run scope.

## Retrieval changes (`execaps.py`, all covered by regression tests)

Learned SIGs inflect exposure wording (`dedupe`/`duplicates` vs transfer
`dedup`/`duplicate`), which exact-token matching silently dropped:

- `sig_word_hits` / `sig_overlap`: inflection-tolerant stem matching
  (shared prefix ≥ 4 chars); short stems (`to`/`token`) still rejected.
  Used identically by the synthesis gate, the REUSE gate, and compose
  search — `tests/test_execaps.py::SigMatchTest` pins the consistency.
- Compose mean-overlap gate REMOVED (was undocumented; the docstring
  specifies per-step evidence only). It rejected correct learned chains
  and once decided by float rounding (`(0.5+2/6+4/6)/3` passes or fails
  depending on summation order). Mean remains a ranking signal.
- Joint coverage floor (≥ 3 distinct procedure words) replaces it:
  steps resting on the same two generic words (`json`+`array`) do not
  compose. Separates cleanly: spurious W-NOV-01 roundtrip covers 2,
  every legitimate R/W chain covers ≥ 5.

Synthesis prompt now demands *neededness* in `applies()` (dedup abstains
when no duplicates exist; never raise), which fixed the v4 harmful reuse
(learned dedup fired exact-row logic on the by-email W-ADP-01 task).

## Transfer results (SAME held-out tasks, learned registry)

| run | learned arm D | hand-seed arm D |
|---|---|---|
| W stub (6) | 6/6, REUSE 1 + COMPOSE 2 + ADAPT 2 + NOVEL 1 | 6/6, 2+2+1+1 |
| R stub (8) | 8/8, REUSE 3 + COMPOSE 2 + ADAPT 2 + NOVEL 1 | 8/8, 4+2+1+1 |
| W live | 6/6, same routing, $0.0008 | — |
| R live | 7/8 (only R-NOV-01, pure-model path) | 7/8 (same NOVEL miss) |

Zero-LLM claim: all 8 REUSE/COMPOSE outcomes across W+R used **0 model
calls** (verified per-record in `d-summary.json`), via 8 distinct learned
caps/plans (e.g. `seq:lc-w-exp-01>lc-w-exp-05>lc-w-exp-06` in correct
mention order). Calls/task: 0.833 (W) / 0.500 (R) live; tokens/task 132/75.
Harmful reuse on transfer: **0** (no REUSE/COMPOSE outcome failed;
`transfer_clean: true` in every verdict).

Artifact flatness: 13 artifacts at registration, **0 new artifacts**
across all 14 transfer tasks. Tasks-per-capability (zero-LLM solves):
8 caps used, 13 solve incidences, max 2 per cap.

## Honest gaps (not closed)

- **No cross-procedure compression**: consolidation dropped 0 — still one
  function per exposure procedure. Transfer flatness holds (nothing new
  learned at transfer), but many-tasks-into-fewer-abstractions does not.
- **Adversarial semantic siblings**: A-EXP-05 (empty→`""`) vs A-EXP-08
  (empty→`null`) share input shape and near-identical prompts, so NO
  input-only `applies()` can separate them. Hand seeds have the identical
  harm (verified: `cap-csv-parse` fires 1.00 overlap, same wrong output).
  Exposure-only, never routed at runtime — but the quarantine check flags
  it whenever the universe includes both.
- **Over-abstention**: single-page input makes learned paginate abstain
  (W-REU-01 check 1), routing a reuse-designed task to 1-call ADAPT.
  Safe direction (adapt passes), but REUSE count trails hand seeds by one
  on each family.
- `growth_sublinear` (§15) still fails: linear-in-exposure growth.
