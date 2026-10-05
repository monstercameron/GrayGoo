# External Evaluator — Mandatory Promotion Gate

Source requirements: `plan.md` §36 (evaluator design), §37 (anti-gaming),
§70 (promotion algorithm: "hidden evaluator passed?"), Phase 3 acceptance
("promotion requires evaluator verdict"), and the "Create the external
evaluator" items in `todos.md`.

## Gate contract

`evaluate(candidate_id, outputs)` returns exactly one verdict:

```json
{"verdict": "pass" | "fail", "evidence": {...}}
```

**Promotion may not proceed without a `pass` verdict from this service.**
Concretely, the promotion authority MUST treat all of the following as
"do not promote":

- a `fail` verdict;
- a protocol rejection (malformed request → error envelope, *no verdict*);
- an evaluator subprocess crash, timeout, or non-JSON reply;
- a verdict whose `candidate_id` does not match the candidate under review.

There is no override path inside the runtime. Per `plan.md` §4.3 the
evaluator is part of the trust root and is not self-modifiable: changes
require an external release process.

## Layout (all paths under `evaluator/`)

```text
evaluator/
  README.md            This file (gate contract)
  __init__.py          Package marker
  protocol.py          stdio/JSON request/response schema + strict validation
  service.py           Hidden-corpus scorer; subprocess entry point; gate API
  hidden_cases.json    Seed hidden corpus (Family A parsing tasks)
  transforms.py        Metamorphic/equivalent-input generators (§37)
  test_evaluator.py    unittest suite (stdlib only)
```

## Process isolation

The service runs as a SEPARATE process (`python -m evaluator.service`):
one JSON request on stdin, one JSON verdict envelope on stdout. The
candidate side never imports the hidden corpus — scoring happens across
the process boundary (see `service.evaluate_in_subprocess`). The hidden
corpus lives here, OUTSIDE the agent-visible `benchmarks/` directory, by
design; the agent must have no read access to this directory in
production (Phase 3 acceptance: "agent cannot inspect hidden suite").

## Scoring

- Outputs are keyed `"<case_id>:<check_index>"`; a missing key fails that
  check (`missing_output`). Unknown keys pass nothing and are reported in
  `evidence["unexpected_outputs"]`.
- Compare modes mirror `benchmarks/runner.py`: `exact` ignores trailing
  newlines; `json` compares parsed deep-equality (key order irrelevant).
- Default thresholds require every case and every check to pass
  (`min_case_pass_rate = min_check_pass_rate = 1.0`). Any relaxation is an
  explicit, caller-supplied threshold in the request, echoed back in
  `evidence["thresholds"]` for audit.

## Anti-gaming (§37)

`transforms.py` generates fresh equivalence-preserving variants — rename
identifiers, permute ordering, rescale values, inject irrelevant fields,
change formatting, new boundary examples — so evaluation is not limited
to the static seed corpus. Each transform is solver-checked: the
reference solver must still map the new input to the new expected
output (`check_case`), proving semantics were preserved.

## Running the suite (offline, stdlib only)

```powershell
python -m unittest discover -s evaluator -v
```

## Production separation

The in-checkout `hidden_cases.json` is a development seed: any process
running as the developer (including rehearsal workers) can read it at the
OS level, so it is NOT hidden from a hostile candidate on this host (see
`documents/adversarial-report.md`, evaluator-inspection).

In production the evaluator runs on a SEPARATE host (or at minimum a
separate OS user) from rehearsal, and the corpus lives there -- outside
any directory the worker identity can read, with deny ACLs for that
identity. Point the service at it with:

```powershell
$env:GRAYGOO_EVAL_CORPUS = "D:\eval-private\hidden_cases.json"
python -m evaluator.service   # subprocess entry point reads the env var
```

`service.resolve_corpus_path` precedence is: explicit `path` argument,
then `$GRAYGOO_EVAL_CORPUS`, then the in-checkout default. The subprocess
entry point inherits the caller's environment, so export the variable
wherever the evaluator process is spawned. Rehearsal verdicts still cross
only the request/verdict protocol -- never the corpus bytes.
