# CI-derived status (issues.md #70)

**Problem.** `todos.md` checkboxes were agent-authored claims. Lisp
modules still called themselves bootstrap stubs (or errored
`unimplemented`) while the corresponding boxes were checked, so agents
planned future work on false premises.

**Rule (in force from 2026-10-05).** Status is generated from CI, not
from TODO edits:

1. The gates in [.github/workflows/ci.yml](../.github/workflows/ci.yml)
   are the source of truth: full Python suite, offline smoke, ASDF
   load, SBCL probes, manifest check.
2. A `todos.md` box for code work may be checked only when the gates
   pass on the commit that did the work, and the commit message names
   the gate evidence (suite count, probe count).
3. Docs-only boxes (no executable behavior) may be checked on review,
   but must name the review command (e.g. "read in full, links
   checked").
4. Reopening rule: if a later gate run fails on an area whose box is
   checked, the box is unchecked again until green. A red gate always
   beats a checked box.

**What this does not do.** CI passing does not prove a todo's *intent*
is met — only that the committed acceptance tests pass. The
`documents/todos-corrections.md` list still records known cases where
checked boxes overstate what the tests actually cover; those stay open
until the tests (not the boxes) are strengthened.

**First green run.** The workflow was added in the round-9 commit; its
first green run on GitHub Actions is pending the next push (local
equivalents all pass: full suite, smoke, ASDF load with
`GRAYGOO-LOAD-OK evo-packages=25`, 10/10 probes, manifest `--check`).
Until that run lands, the pre-existing local gate evidence recorded in
commit messages stands.
