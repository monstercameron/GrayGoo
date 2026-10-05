# SBCL regression probes (`tests/lisp/`)

Standalone Lisp integration probes for issues #15–#18. Each probe loads
its accused sources **by file path** (no ASDF system, no image build),
runs its checks, prints `ok:` / `FAIL:` lines plus one summary marker,
and exits **0 on PASS, 1 on FAIL** — so any runner can treat them as
ordinary tests.

Reference runtime: SBCL 2.6.9 (repo standard). Locate it the same way
`workers.py` does: explicit path, else `%GRAYGOO_SBCL%`, else
`%LOCALAPPDATA%\sbcl-local\sbcl-2.6.9\PFiles\Steel Bank Common Lisp\sbcl.exe`.

## Probes

| Probe | Covers | Checks |
|---|---|---|
| `epoch-nested.lisp` | #17 nested calls across pinned epochs | latest vs pinned resolution, explicit `:epoch`, double nesting (BAZ→BAR→FOO) |
| `epoch-drain-refusal.lisp` | #17 drain-while-pinned refusal | latest/unknown/pinned refusals, routing intact after refusal, post-drain behavior, versions retained |
| `epoch-rollback.lisp` | #17 rollback visibility | instant pointer move, pinned epochs unaffected, re-promotion, nothing deleted |
| `dispatch-concurrency.lisp` | #16 control-plane lock | 4 threads × 60 register/promote/invoke/pin cycles, all `:OK`, pins drain to zero |
| `capability-identity.lisp` | #15 stable UUID ids + display names | mint/validate, constructor defaults, explicit identity, derivation inheritance, registry save/load round-trip in `%TEMP%` |
| `persistence-integrity.lisp` | #42–#45 registry persistence | package-escaped filenames, write-once versions, temp+rename, FNV integrity hash, tamper refusal, `.tmp` invisibility |
| `capability-model.lisp` | #49–#51 constructor validation | explicit-NIL clearing (lists/TTL/source), omitted-key inheritance, risk-level validation |
| `schema-stubs.lisp` | #18 migration design, evolution disabled | flag on, `apply-migration` refuses, inert design records, compatibility stub verdicts |

## Run them

From the repo root (PowerShell):

```powershell
$sbcl = "$env:LOCALAPPDATA\sbcl-local\sbcl-2.6.9\PFiles\Steel Bank Common Lisp\sbcl.exe"
& $sbcl --non-interactive --no-userinit --no-sysinit --disable-debugger `
  --load tests/lisp/epoch-nested.lisp
```

Expected tail output:

```text
  ok: double-nested call under latest resolves v2 chain
PROBE epoch-nested: PASS
```

(`$LASTEXITCODE` is 0. On failure the tail reads `PROBE <name>: FAIL …`
and the exit code is 1.)

Run all six:

```powershell
$sbcl = "$env:LOCALAPPDATA\sbcl-local\sbcl-2.6.9\PFiles\Steel Bank Common Lisp\sbcl.exe"
Get-ChildItem tests/lisp/*.lisp | ForEach-Object {
  & $sbcl --non-interactive --no-userinit --no-sysinit --disable-debugger `
    --load $_.FullName
  if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $($_.Name)" }
}
```

## Notes

- Probes are hermetic: each `sbcl` invocation is a fresh image, so no
  state leaks between probes and no reset protocol is needed.
- `capability-identity.lisp` writes temp capability files under
  `%TEMP%\graygoo-cap-probe\` and deletes them afterwards (best effort).
- `dispatch-concurrency.lisp` uses SBCL threads (`sb-thread`) and is
  expected to run only on SBCL, like the rest of the runtime.
