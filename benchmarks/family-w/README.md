# Benchmark Family W — API-Workflow Primitives (directive §4)

Exposure tasks intentionally teach reusable primitives; transfer tasks
require reuse, composition (2- and 3-capability), adaptation, and
novel behavior. All transfer is `split: transfer` (held-out).

## Layout

```text
benchmarks/family-w/
  README.md            This file
  exposure.json        8 primitives (W-EXP-01..08, one per capability)
  reuse.json           2 same-procedure tasks -> REUSE target
  compose.json         W-CMP-01 (paginate->dedup), W-CMP-02
                       (paginate->normalize->dedup) -> COMPOSE target
  adapt.json           W-ADP-01 key-based dedup: whole-row dedup must
                       abstain (prompt gate) -> ADAPT target
  novel.json           W-NOV-01 due-date scheduling: nothing applies,
                       nothing retrieves -> NOVEL target
  recorded/
    stub_all_pass.json Recorded passing outputs (offline proof)
```

## Task kinds

| Task | Arm C (single-cap) | Arm D (+composition) |
|---|---|---|
| W-REU-01/02 | REUSE, pass | REUSE, pass |
| W-CMP-01/02 | REUSE-path, FAIL (paginate alone leaves work undone) | COMPOSE, pass |
| W-ADP-01 | ADAPT (retrieved context + model) | ADAPT |
| W-NOV-01 | NOVEL (model scratch) | NOVEL |

Expected outputs were computed by EXECUTING the capabilities (or the
documented reference for adaptation/novelty targets), never
hand-written. Prompts are load-bearing: procedure words drive the
composition search (mention order ranks step order), and the novelty
prompt shares zero keywords with exposure (verified at generation).

## Running (offline, transfer split only)

```powershell
uv run python benchmarks/run_abcd.py --adapter stub --tasks benchmarks/family-w --split transfer --artifacts artifacts/abcd-w
```

Live (costs tokens; default to offline):

```powershell
uv run python benchmarks/run_abcd.py --adapter cerebras --tasks benchmarks/family-w --split transfer --artifacts artifacts/abcd-w-live
```
