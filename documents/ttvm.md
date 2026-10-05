# Time-to-verified-mutation (TTVM) — first measurement

Date: 2026-10-05. Sampler: `experiments/ttvm_sample.py`. Raw data:
`artifacts/ttvm/samples.json` (gitignored; 10 samples, per-stage ms).

## Method

10 trivial Lisp-expression tasks (single arithmetic / list op each) run
through the REAL path stage by stage: `retrieve` + `context`
compilation, live Cerebras `generate_candidate` (qwen-3.8-27b, ≤128
completion tokens, reasoning off, temp 0.0 then 0.7 on retry),
`normalize_candidate` + `parse_candidate`, R0 risk gate, real SBCL
rehearsal via `pipeline.run_candidate` (1 check per sample). Max 2
attempts per sample; only rehearsal-passing samples count as verified
mutations. Live cost ≈ $0.01 (12 tiny calls).

Two robustness notes from the run: the model sometimes returns a full
`(candidate ...)` envelope instead of a bare expression (the sampler
unwraps `:definition` forms via `dumps_body`), and temp-0.0 retries
repeat failures verbatim (hence temp escalation on retry).

## Result (n = 8 verified / 10)

| Statistic | Value |
|---|---|
| Median TTVM | 655 ms |
| Mean TTVM | 708 ms |
| Verify rate | 8/10 (ttvm-02, ttvm-09 failed both attempts) |

Per-stage breakdown (mean / share of stage-mean sum):

| Stage | Mean | Median | Share |
|---|---|---|---|
| model | 377 ms | 300 ms | 53% |
| worker (SBCL envelopes) | 330 ms | 336 ms | 47% |
| compile | 0.7 ms | 0.2 ms | 0.1% |
| test (harness overhead) | 0.4 ms | 0.4 ms | 0.1% |
| context | 0.07 ms | 0.06 ms | ~0% |
| eval | UNMEASURABLE (n=0) | — | — |
| promotion | UNMEASURABLE (n=0) | — | — |

## Scope and limits

- "Verified" = passes its rehearsal checks. The hidden-evaluator and
  promotion stages are absent BY DESIGN: promotion scores the fixed
  hidden corpus, which task-scoped samples do not target. Completing
  those two stages needs a capability whose task IS in the hidden
  corpus (future work, see roadmap).
- Tasks are trivial on purpose: this measures pipeline latency, not
  model reasoning power. Real-task TTVM will be dominated even more by
  the model stage (longer generations, repairs).
- Single-machine, fresh SBCL per check (no warm pool): the worker share
  is an upper bound; `pool.py` long-lived workers should cut it.
