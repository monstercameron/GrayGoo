# Retrieval overhead vs library size (issues.md #103)

Question: as patch memory grows, does retrieval itself become
expensive? Measured, not modeled: `tests/test_retrieval_scale.py`
seeds 500 patches and times `retrieve` + `find_fast_path`.

## Before: 536 ms/query (a real bug, not noise)

The first measurement showed 536 ms/query at 500 patches. Profile:
`list_patches` re-opened + re-parsed all 500 files per query
(Windows file-open overhead dominated). Worse, the naive fix (cache
+ per-file mtime validation) still cost 189 ms/query — 1,500
`os.stat` calls per query turn out to be the next bottleneck.

## Fix: mtime-validated cache over one scandir

`patches.PatchStore` now caches records keyed by filename with
mtime validation, iterating via a single `os.scandir` (mtimes free,
no per-file stat). External writers can never serve stale: changed
mtimes re-read, vanished files prune. All store mutations funnel
through `_write_patch`, and the one in-place mutator
(`consolidate.retire`) persists immediately, so cache and disk agree
(the next read re-validates by mtime regardless).

## After: 73 ms/query at 500 patches (7×)

Steady-state (one warmup query populates the cache; cold-start
population is a one-time ~500-open cost, inherent to file-backed
stores). The test pins <100 ms/query at 500 patches with a generous
10 s total budget — a smoke bound against future 10× regressions,
not a flaky perf assertion.

Scaling shape: per-query work is O(library) record visits (scoring
+ expiry checks over cached dicts) + O(1) syscalls. At 73 ms/query
for retrieve + fast-path combined, retrieval is ~7% of a 1 s
TTVM-scale mutation loop — not the bottleneck. Re-measure at 5,000
patches before claiming more.
