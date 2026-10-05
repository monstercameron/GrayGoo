# Diet-B: retrieval-diet attack on FAIL #3 (2026-10-05)

Follow-up to `documents/b-rerun.md`, attacking success criterion #3
(tokens/task decreases — FAIL at 450.7 vs the 264.7 bar). Question:
does a starved memory (top-1 instead of top-2, 120-char excerpts
instead of 320) cut tokens/task toward the bar while holding success?

Command: `uv run python benchmarks/run_bcd.py --baseline b --adapter
cerebras --artifacts artifacts/baselines-b-diet --max-tokens 128
--top-k 1 --max-chars 120` (same protocol as B otherwise).

## Result: NO — cheaper, but a regression, and still over bar

| Metric | A (strip) | B (full) | B rerun | Diet-B |
|---|---|---|---|---|
| Tasks | 29/35 | 30/35 | 30/35 | 29/35 (+0) |
| Tokens/task | 264.7 | 450.7 | 450.7 | **342.7** |
| Cost | — | — | $0.0167 | $0.0129 |

- Diet holds the TRN-08 flip (still passes) but **loses A-ADV-05**,
  which A, B, and B-rerun all pass: diet fails {A-ADV-02, A-ADV-05,
  A-EXP-08, A-EXP-16, A-TRN-02, A-TRN-06}.
- 342.7 is down 24% from full B but still **+29% over the 264.7
  bar** — criterion #3 stays FAIL.
- A-ADV-05 anatomy: full B injected both A-EXP-09 checks (312 chars)
  and passed; diet injected only check 0 (175 chars) and check 1
  returned `ERROR` instead of `{"a.b": 2}`. The truncation removed
  the example the model needed.

## Interpretation (honest, both directions)

Memory content is now proven causal in BOTH directions on held-out
tasks: full memory fixed TRN-08 (2/2 runs), starved memory broke
ADV-05. But the diet curve is unfavorable: −24% tokens buys −1 task
and the first A-relative regression in any B-condition run. Linear
extrapolation says reaching 264.7 needs roughly top-0 — i.e. the bar
is unreachable by starving retrieval alone. Flipping #3 needs memory
that *replaces* model work (fewer calls / shorter outputs net of
injection), not skimpier excerpts — e.g. the input-matched fast-path
fix, which is now the only remaining plausible route.

Caveat for criterion #5 (negative transfer PASS): the PASS covers
the B/C/D conditions (0 A-relative regressions, replicated for B).
Diet-B shows memory CAN regress when starved — the safety claim does
not extend to arbitrary retrieval budgets.
