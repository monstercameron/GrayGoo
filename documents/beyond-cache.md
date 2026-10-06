# Beyond the cache: precision, scaling, compression

Session goal 2026-10-06: attack the three priority gaps that separate
GrayGoo from a very good executable cache — applicability precision,
scaling retrieval, and abstraction compression.

## 1. Applicability precision: mined veto words + adversarial traps

Mechanism: when consolidation finds a harm pair (cap fires on a task
but solves it wrong), it mines discriminative veto words — prompt
words of the harmed task that stem-match nothing in the cap's safe
prompts, signature, or a function-word stoplist — attaches them, and
re-measures. Vetoes that eliminate the harm with zero coverage loss
keep the capability (precision repair); prompt-indistinguishable
pairs still quarantine. Vetoes override all positive evidence in
both `applies_to` (REUSE) and compose-search `viable()`.

Three near-match traps (new `adversarial` split, never in pinned
transfer counts) each misfired before vetoes and abstain after:

- R-ADV-01: semicolon CSV vs the comma parser (control: the learned
  precondition already abstains; vetoes add semicolon-words anyway).
- R-ADV-02: empty→null CSV vs the empty→"" parser (was REUSE-fail).
- W-ADV-01: UPPERCASE-normalize vs the lowercase normalizer
  (was REUSE-fail; mined veto is exactly `uppercase`).

Result: traps pass 3/3 via ADAPT with zero REUSE, transfer routing
byte-identical (W 9/9, R 12/12). Note the failure that motivated the
stoplist: mining first vetoed glue words (`then`), which would have
killed compose chains — vetoes must be content words.

Honest control observation: an un-consolidated cap (fresh
lc-a-exp-08) immediately misfired on R-ADV-01 — consolidation is
load-bearing, not ceremonial.

## 2. Scaling: hierarchical evidence pre-filter + distractor proof

Compose search now filters to evidence-clearing, veto-clear steps
BEFORE chain enumeration (evidence → type links → trial), preserving
results exactly while keeping enumeration proportional to survivors.
Distractor benchmark (realistic keyword/type mix, seeded):

| registry | ms/check | routing vs baseline |
|---|---|---|
| 13 | 1.7 | — |
| 113 | 8.8 | identical |
| 513 | 32.2 | identical |

Linear, not cubic. Committed test pins winner-equality with 100
distractors. Known limit probed and documented: if EVERY distractor
shares a third of its signature with the prompt (far beyond any
plausible library), no-op chains survive below rank 0 and enumeration
slows — rank 0 still wins. Middle-step vacuity pruning was considered
and rejected: a step identical on the trial input may be load-bearing
on other checks.

## 3. Compression: sibling collapse into a parameterized abstraction

`distill.generalize_pair` collapses two same-category, same-type
siblings into ONE capability with a prompt-bound parameter: the
caller names the flag and each sibling's value, Qwen writes the
unified `solve(text, mode)` (gate now allows a second arg with a
string default), distill verifies each (task, mode) pair through
rehearsal + repair, and alt triggers are mined deterministically
(the same discriminators veto-mining finds). Binding is a pure
function of the task prompt — no shared state.

Live demo: lc-a-exp-05 (empty→"") + lc-a-exp-08 (empty→null, freshly
distilled, $0.0024) → lc-abs-a-exp-05-a-exp-08 (merge $0.0014, first
attempt). Registry: would-be 14 caps → 13, now spanning 13
procedures. R-ADV-02 graduates from trap to 0-call REUSE (null
binding); R-ADV-01 still abstains (semicolon vetoes re-mined onto
the merged cap). Transfer identical live (R 11/12 with the known
NOVEL miss, W 9/9); the abstraction serves REUSE and COMPOSE (map
producer in R-CMP-01).

Remaining gap (stated, not hidden): sibling DETECTION and flag
naming are still experimenter-driven — the caller passes the
param spec. Automatic clustering + spec proposal is the next rung.
Single-alt parameters only; N-way needs a binds map.

## Totals

Live spend this goal: ~$0.004 (sibling distill + merge). Suite: 809
tests, green except the pre-existing user-owned `test_lessons`
error. 14 new committed tests (veto ×5, adversarial routing ×4,
scaling ×1, generalize ×4).
