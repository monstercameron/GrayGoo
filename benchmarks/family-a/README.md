# Benchmark Family A — Parsing / Data Transformation

Source requirements: `plan.md` §81 (Family A: dates, CSV, nested records,
logs), `plan.md` §37 (equivalent transforms), `plan.md` §21 Phase F
(adversarial inputs), `documents/benchmark-plan.md`, and the "first
benchmark family" items in `todos.md`.

## Layout

```text
benchmarks/
  runner.py                 Offline runner + swappable adapter interface
  family-a/
    README.md               This file
    exposure.json           Exposure tasks (system may learn from these)
    transfer.json           Held-out transfer tasks (primary learning evidence)
    adversarial.json        Boundary / malformed / empty inputs
    equivalent-transform.json  Semantically equivalent variants (§37)
    recorded/
      stub_all_pass.json    Recorded outputs: every check passes (offline proof)
      stub_all_fail.json    Recorded outputs: every check fails (fail detection proof)
```

## Task schema

Each `*.json` file (top level, not `recorded/`) is an array of tasks:

| Key | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | Unique id: `A-EXP-NN`, `A-TRN-NN`, `A-ADV-NN`, `A-EQV-NN` |
| `family` | string | yes | Always `"A"` |
| `split` | string | yes | One of `exposure`, `transfer`, `adversarial`, `equivalent-transform` |
| `category` | string | no | `dates`, `csv`, `records`, or `logs` |
| `prompt` | string | yes | Task instruction given to the model (system message) |
| `checks` | array | yes | Non-empty list of hidden fixtures |
| `checks[].input` | string | yes | Fixture input given to the model (user message) |
| `checks[].expected` | string | yes | Expected model output |
| `checks[].compare` | string | yes | `exact` (modulo trailing newlines) or `json` (parsed deep-equality) |
| `notes` | string | yes | Rationale, edge cases, cross-references |
| `derived_from` | string | eqv only | Base exposure task id |
| `transforms_applied` | array | eqv only | §37 transforms used |

A task passes iff **all** of its checks pass.

## Split policy

- **Exposure (majority).** Training material: the system may learn, distill
  lessons, and promote capabilities from these. Covers all four categories
  (dates incl. timezones/relative/weekday, CSV incl. embedded newlines and
  doubled quotes, nested records incl. flatten/unflatten, logs incl. CLF,
  key=value, aggregation).
- **Transfer (held-out).** Same family, unseen variants (RFC-2822 dates, ISO
  week dates, semicolon-delimited data, comment-tolerant CSV, deep merge,
  address normalization, syslog, sessionization). Never train on these; they
  are the primary evidence of learning vs. memorization.
- **Adversarial.** Boundary / malformed / empty / ragged inputs per §21
  Phase F. Each task also carries at least one well-formed check so that a
  degenerate always-`ERROR` policy fails.
- **Equivalent-transform.** Per §37 anti-gaming: rename identifiers, permute
  ordering, rescale values, inject irrelevant fields, change formatting.
  Each task records `derived_from` + `transforms_applied`; they detect
  candidates that optimize for test structure rather than semantics.

## Running (offline)

```powershell
# All 43 tasks against the recorded passing fixture: expect 43/43, exit 0
# (35 original + 8 supplemental transfer A-TRN-09..16, see below)
python benchmarks/runner.py --recorded benchmarks/family-a/recorded/stub_all_pass.json

# Fail-detection proof: expect 0/43, exit 1
python benchmarks/runner.py --recorded benchmarks/family-a/recorded/stub_all_fail.json

# One split, one task, or just list
python benchmarks/runner.py --recorded <file> --split transfer
python benchmarks/runner.py --recorded <file> --only A-EXP-05 --verbose
python benchmarks/runner.py --list
```

No network access is used in the default path. The `cerebras` adapter
(`--adapter cerebras`) is opt-in only and makes live API calls with
reasoning disabled and a tiny token budget.

## Adapter interface

```python
class ModelAdapter:
    def solve(self, task: dict, check_index: int, check_input: str) -> str: ...
```

`task["prompt"]` is the instruction, `check_input` the fixture input; return
the model's raw output text. See `StubAdapter` (recorded replay) and
`CerebrasAdapter` (live, opt-in) in `benchmarks/runner.py`.

## Adding tasks

1. Append to the matching split file (ids sequential within the prefix).
2. Keep fixtures deterministic and unambiguous; prefer `json` compare for
   structured output so key order cannot fail a correct answer.
3. Adversarial tasks must include a well-formed check (anti always-ERROR).
4. Equivalent transforms must set `derived_from` + `transforms_applied`.
5. Regenerate `recorded/stub_all_pass.json` from the new expected outputs and
   re-run the runner both ways before finishing.

## Supplemental transfer set (A-TRN-09..16, 2026-10-05)

Appended per the recipe above (pure append: the original 35 tasks are
byte-identical). Reference generator:
`benchmarks/family-a/make_transfer2.py` — every expected output is
computed by an independent stdlib reference implementation, and
`tests/test_transfer2.py` pins byte-identity
(`make_transfer2.py --check`) plus the frozen-task invariant.
Motivation: transfer recall cannot settle at n=8
(`documents/core-experiment-gate.md`); the 8 new tasks (epoch
seconds, 12-hour+EST, quoted-tab TSV, backslash pipes, unflatten,
group-by-count, Apache combined, quoted key=value) double the
held-out set without touching the frozen 35 or their recorded
baselines (all baseline artifacts + verifiers pin the original runs).
