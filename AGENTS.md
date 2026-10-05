# AGENTS.md — Working Agreement for Coding Agents

Instructions for any agent (human-directed or autonomous) doing engineering
work in this repo. Follow this file plus the language/tool skills loaded for
the task.

## 1. Sources of truth (in order)

1. `plan.md` — the system specification
2. `memory.md` — the learning-layer design
3. `todos.md` — the execution-ordered task list
4. `documents/` — planning/research digests (update when a spec changes)

Where a digest disagrees with a spec, the spec wins. Never silently widen
scope beyond what the specs and the user's request authorize.

## 2. SDLC workflow

Every change follows these gates; do not skip one to go faster.

| Gate | Activity | Exit criteria |
|---|---|---|
| 1. Requirements | Derive the contract from `plan.md`/`todos.md`, existing tests, types, and callers — not from the issue text alone. | Named todos + acceptance criteria. |
| 2. Design | Smallest design that satisfies the contract. Align with `documents/architecture-overview.md`. Flag architecture decisions in `documents/open-questions.md` instead of guessing. | Design fits existing module/package layout. |
| 3. Implement | Smallest correct diff at the root cause. Match surrounding conventions (see §4). No unrelated edits. | Diff touches only the task's files. |
| 4. Self-review | Read the full diff before finishing. Every hunk must be intentional and explained by the task. | No debug leftovers, no scope creep. |
| 5. Test | Run the repo's tests for the touched area (see §5) and add/extend tests for new behavior. A fix without a regression test is incomplete. | Green suite, observed in-session. |
| 6. Integrate | Leave the tree clean and reviewable. Commit/push ONLY on explicit user ask; stage named files, never `git add -A` in a dirty tree. | `git status` shows only intended files. |
| 7. Document | Update the todos (§6), plus specs/digests/comments the change invalidates. | No stale checkbox, doc, or comment. |

Definition of done: gates 1–7 all pass and the final report states what was
verified, how, and what remains open.

## 3. Repo map

- `src/` + `graygoo.asd` — Common Lisp system (primary runtime language)
- Root `*.py` — Python tooling/adapters (`cerebras_client.py`, `s_expr.py`, `events.py`)
- `tests/` — all test code lives here: `unittest` suites one file per module (`test_<module>.py`), standalone verifiers (`verify_*.py`), SBCL probes (`lisp/`). Never scatter tests into source dirs or `artifacts/`.
- `benchmarks/` — task families + offline runner
- `documents/` — digests; `docs/` — static GitHub Pages site; `artifacts/` — generated outputs (gitignored)

## 4. Conventions

- Python: stdlib-first, `uv run` for execution, project `.venv` at root.
- Lisp: packages from `plan.md` §73, layout from §74; no new top-level dirs without a design note.
- New capability/module? Add its tests in the same change.
- Never print secrets (`.env` holds a live key; a rotation is still pending — see repo history notes).

## 5. Build, test, verify

```powershell
uv run python -m unittest discover -s tests          # full Python suite
uv run python benchmarks/runner.py --adapter stub --recorded benchmarks/family-a/recorded/stub_all_pass.json
uv run python cerebras_client.py "Reply with exactly: OK"   # live API smoke (costs tokens; default to offline)
```

- Prefer offline verification (stubs, fixtures, recorded outputs). Live
  Cerebras calls need a reason and stay tiny (reasoning off, small `max_tokens`).
- Quote-heavy inline Python breaks in PowerShell: write throwaway scripts to
  `$env:TEMP`, never into the repo.

## 6. Todo discipline (mandatory)

- `todos.md` is the project's shared checklist. When you finish an item,
  check it (`[x]`) **in the same change** — a done-but-unchecked item is a defect.
- Only mark items you verified in-session (tests run, evidence observed).
  Partial work stays `[ ]`; note blockers in your report instead.
- Discovering new required work? Add it to `todos.md` under the right section.
- Parent items check off only when all children are done.

## 7. Artifact policy (mandatory)

- ALL generated outputs go to `artifacts/` (benchmark results, logs, coverage,
  temp DBs, recorded traces) and it is gitignored — nothing generated is
  ever committed.
- Tests must use tempfiles, `:memory:` DBs, or `artifacts/`; they must not
  write stray files into source dirs.
- Never commit `.env`, `.venv/`, `__pycache__/`, `*.fasl`, or editor/OS noise
  (see `.gitignore`).

## 8. Parallel-agent rules

- Each lane owns disjoint paths (named in its brief) and stays inside them.
- Worker lanes run NO git commands (no add/commit/push/pull/stash) — the
  coordinator owns history.
- Workers never print secrets and keep live API calls within their brief's budget.
- A lane's final report lists files created/modified plus exact verification
  evidence (commands + results), never claims without output.
