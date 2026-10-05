# Secret policy (issues.md #25)

Verified 2026-10-05 against the current tree (grep of credential and
environment flows; no live calls; no secret values printed).

## Findings

No credential flows into worker *code or prelude*:

- `workers.py` reads only `LOCALAPPDATA` (SBCL path) and `GRAYGOO_SBCL`
  (`workers.py:74,99`); the worker script template embeds code, prelude,
  and fingerprint only — no keys.
- `cerebras_client.py` reads `CEREBRAS_API_KEY` (preferred) or `CEREBRAS`
  from the environment / project `.env`, and never prints the value
  (`cerebras_client.py:7-8,32-37`).
- `attacks.py` never reads `.env` or any real credential; its only
  secret is a self-planted random canary in `%TEMP%` (`attacks.py`
  docstring).
- `demo.py` never prints secrets (docstring).
- `pool.py` (new persistent pool) scrubs `CEREBRAS_API_KEY`/`CEREBRAS`
  from every spawned worker's environment (`pool._scrub_env`, covered
  by `tests/test_pool.py:ProtocolUnitTest.test_scrub_env`), and its
  worker script embeds no secrets
  (`tests/test_pool.py:ProtocolUnitTest.test_build_script_shape`).

One live residual (fix belongs to the **sandbox lane** —
`workers.py` is sibling-owned, read-only for this lane):

- `workers.run_lisp` spawns SBCL via `Popen` **without** an `env=`
  argument (`workers.py:370-376`), so each rehearsal worker inherits
  the parent's full OS environment, including `CEREBRAS_API_KEY`.
  Candidate code running in the worker can therefore read the live key
  out of its own environment (the module docstring already warns that
  workers inherit OS privileges). Suggested fix: pass a scrubbed env
  (same shape as `pool._scrub_env`) in `run_lisp`'s `Popen` call.

## Rotation reminder (still pending)

- `.env` holds a live key and is tracked historically (`.gitignore`
  header note; `AGENTS.md` §4). The rotation noted in repo history has
  not happened. Coordinator action: rotate the Cerebras key, purge the
  old value from history on explicit owner approval, and keep `.env`
  out of all future commits (it is gitignored going forward, but the
  historical blob remains until purged).

## Rules going forward

1. Secrets live in the environment or the local `.env` only — never in
   source, tests, fixtures, prompts, or worker prelude/code.
2. Every new subprocess spawn that runs untrusted code MUST pass a
   scrubbed `env` (denylist at minimum: `CEREBRAS_API_KEY`, `CEREBRAS`;
   see `pool._scrub_env` as the reference implementation).
3. Never print, log, or persist credential values — not even in
   `artifacts/` (tests assert this where practical).
4. Model-facing context (`context.py`, retrieval, lessons) MUST NOT
   include environment dumps or file reads outside the task corpus.
5. Adversarial tests MUST NOT touch real credentials; self-planted
   canaries in `%TEMP%` only (the `attacks.py` rule).
6. Any lane that finds a credential in the tree or in logs stops,
   reports to the coordinator, and treats rotation as required.
