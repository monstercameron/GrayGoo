# Learning efficiency: improvement per extra token (issues.md #105)

Definition (`metrics.learning_efficiency`, unit-tested):

> `(run_rate − base_rate) / max(1, run_tokens − base_tokens)`

held-out success-rate delta per additional token vs the no-memory
baseline, recomputed from raw per-task artifacts (never doc tables).
Positive = learning paid for its context. Zero with positive extra
tokens = paid context for nothing. The denominator clamps at 1, so a
cheaper-and-better run scores its full rate delta.

## Measured values (transfer held-out, stripped)

| Run | Pass | Tokens | Δrate | Δtokens | Efficiency |
|---|---|---|---|---|---|
| A old-8 (base) | 5/8 | 2463 | — | — | — |
| B old-8 | 6/8 | 5709 | +0.125 | +3246 | **3.85e-05** |
| D old-8 | 6/8 | 8449 | +0.125 | +5986 | **2.09e-05** |
| A new-8 (base) | 6/8 | 1893 | — | — | — |
| B new-8 | 6/8 | 4855 | 0.000 | +2962 | **0.0** |
| D new-8 | 6/8 | 7595 | 0.000 | +5702 | **0.0** |

Reading: the only positive efficiency in the project is the TRN-08
edge — one task for 3–6K tokens. Text memory (B) is ~2× more
token-efficient than dual (D) at buying it. On the new set,
efficiency is exactly zero both arms: thousands of tokens, no
movement. This metric is now the single number that must go up; the
falsification bar (#93.1) is its sign flipping non-positive on a
blind re-run.

## Why no stream row

Streams change population per round (6 → 12 → 18 tasks), and the
metric requires same-population rates. The stream cost story is
reported separately and is the mirror image: round 3 holds success
(16/18, fails known-hard) at 88.5 tokens/task vs round 1's 273.5 —
cost collapsing at fixed success, i.e. efficiency through savings
rather than improvement. A same-population stream rerun (round 1 =
all 18, then repeats) would make the metric applicable; not yet run.
