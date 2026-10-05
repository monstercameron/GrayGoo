# Benchmark Family R — Reuse-Designed Held-Out Tasks

Family A transfer tasks deliberately need FRESH procedures, so executable
reuse cannot fire on them by construction (gated fast-path: 0/32 fires).
Family R measures the complementary skill: **reusing learned procedures
on unseen inputs** — the core thesis.

All 7 tasks are `split: transfer` (held-out; never train on these).

## Layout

```text
benchmarks/family-r/
  README.md            This file
  reuse.json           4 same-procedure tasks (prompts identical to
                       A-EXP-01/05/09/13, fresh inputs) -> REUSE target
  trap.json            1 applicability trap (EXP-01 prompt, one ISO-week
                       line the date capability must abstain on) -> NOVEL
  compose.json         2 composite tasks (csv+dates, flatten+csv) ->
                       COMPOSE target for arm D, honest misfire for arm C
  recorded/
    stub_all_pass.json Recorded passing outputs (offline proof)
```

## Task kinds

| Task | Procedure | Arm C (single-cap) | Arm D (+composition) |
|---|---|---|---|
| R-REU-01..04 | Same as one exposure task | REUSE, pass | REUSE, pass |
| R-ADP-01 | One input outside every contract | abstain -> model | abstain -> model |
| R-CMP-01/02 | Two capabilities + thin glue | REUSE-path, FAIL (misfire documents the composition gap) | COMPOSE, pass |

Expected outputs were computed by EXECUTING the capabilities
(`execaps.py`), never hand-written (generator: throwaway script, kept
out of the repo per AGENTS.md §7).

## Running (offline)

```powershell
uv run python benchmarks/run_abcd.py --adapter stub
```

Live (costs tokens; default to offline):

```powershell
uv run python benchmarks/run_abcd.py --adapter cerebras --arms abcd
```
