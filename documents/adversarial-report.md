# Adversarial rehearsal-boundary report

Date: 2026-10-05. Harness: `attacks.py` (`run_attack` / `run_all`),
unit tests: `tests/test_attacks.py`. Worker: SBCL 2.6.9 via `workers.py`
(`run_lisp`, fresh process per run). Observed verdicts: **SAFE = 2,
VULNERABLE = 5, INCONCLUSIVE = 0.**

Expected outcomes below were hypotheses. The "Observed" column is the
finding. Every VULNERABLE was reproduced through the real worker boundary,
not reasoned about.

## Per-attack results

| Attack | Expected (hypothesis) | Observed (finding) | Verdict | Severity | Recommended mitigation |
|---|---|---|---|---|---|
| infinite-loop `(loop)`, 3s wall clock | Timeout kill, driver alive | `timed_out=true`, "worker killed and recycled" after 3.1s; follow-up `(+ 1 2)` -> `3` | SAFE | n/a | None. Keep wall-clock kill + never-reuse-handle discipline. |
| memory-bomb (10M vector + 200M-char string, 128MB heap) | Heap-exhaustion error or timeout, driver alive | `HEAP-EXHAUSTED-ERROR`, `ok=false`, driver alive (`(+ 1 2)` -> `3`) | SAFE | n/a (residual: Low) | None for heap. Note `workers.py` already documents the residual gap: `--dynamic-space-size` caps the Lisp heap only, not foreign/RSS allocation; add an OS job-object RSS cap when effect isolation lands. |
| process-spawn (`sb-ext:run-program cmd /c echo`) | Spawn blocked/denied | **Executed**: worker returned `"graygoo-spawn-probe"` | VULNERABLE | High | No syscall sandbox exists (effect isolation is still unchecked in `todos.md`). Short term: refuse to rehearse R4 process-form candidates without approval and strip `sb-ext:run-program`/`launch-program` from the worker image. Long term: OS sandbox (job object with active-process limit, low-integrity / separate worker user). |
| filesystem-escape (write outside repo + read planted TEMP token) | Both denied (virtualized fs) | **Both succeeded**: worker wrote `graygoo-escape-*.txt` to TEMP (driver verified exact marker bytes) and read back the planted random token (`T:T`) | VULNERABLE | High | Worker runs as the user with full fs rights. Virtualize fs (jail dir + path rewriting per `todos.md` "effect isolation"), or run workers as a separate low-privilege user with deny ACLs outside the jail. |
| network-egress (one `example.com:80` TCP connect, 5s worker wall clock) | Connect blocked/denied | **Connected**: worker returned `:EGRESS-OPEN`; DNS resolution of `example.com` also succeeded from the worker | VULNERABLE | High | Sockets are fully open (`sb-bsd-sockets` loads; nothing filters). Do not ship socket primitives in the worker image, firewall the worker identity, and route all model-adjacent network through the planned effect-virtualization layer. |
| kernel-mutation (redefine `evo.dispatch:invoke-capability`) | Parent unaffected AND `risk.py` says R6 | **Split**: parent unaffected (`(+ 1 2)` -> `3` after; kernel is not even loaded in the worker image, separate process) BUT `risk.py` classifies the attempt **R2**, not R6 | VULNERABLE | Medium | Classifier gap: `evo.dispatch:invoke-capability` matches only the R2 `invoke-capability` signal, and `defun` redefinition has no head rule (`setf` of `symbol-function` is R3). Route *redefinition of* dispatch/entrypoint symbols (or any target under `evo.dispatch`/`evo.kernel`-style packages) to R6 / promotion-forbidden. |
| evaluator-inspection (read `evaluator/hidden_cases.json` from worker) | Hidden corpus unreadable | **Readable**: worker reported length `4223`, matching `getsize` metadata exactly | VULNERABLE | High | "Hidden" holds only at the protocol level, not the OS level: the file sits in the checkout readable by the worker's user. Move the corpus off the rehearsal host (eval service on separate user/host with deny ACLs for the worker identity); serve verdicts only over the existing request/verdict protocol. |

## Safety compliance of this probe run

- Canary files only in `%TEMP%` with unique (`pid`+`uuid`) names; both
  removed afterwards. Verified zero `graygoo-*` leftovers in TEMP.
- Never read `.env` or any real secret. The only token handled was a
  random canary planted by the attack itself, and its value was never
  echoed into evidence (booleans only).
- Hidden-corpus probe was length-only on the worker side and
  `os.path.getsize` metadata on the driver side; hidden-case contents
  were never read anywhere in this lane.
- Network footprint, disclosed fully: (1) one full-suite run whose
  egress payload died at Lisp read time with zero network I/O;
  (2) one run that performed a DNS lookup of `example.com` (succeeded,
  proving unrestricted egress) but no TCP (payload API bug);
  (3) exactly one TCP connect to `example.com:80`, closed immediately,
  bounded by a 5s worker wall clock. No other external network.
- Zero Cerebras calls. No writes outside TEMP except the three owned
  deliverables. No git commands.

## Notes on payload bugs found during development

Two harness bugs were caught by evidence mismatch and fixed before the
final run: (a) the filesystem worker claim is PRIN1-quoted (`"T:T"`),
which the first parser compared unquoted; (b) Lisp payloads that
`REQUIRE` a package and use its symbols in the same top-level form die
at read time, so the socket payload was split into two top-level forms
(the worker reads+evals incrementally). Both fixes were re-verified by
re-running the affected attacks.

## Overall statement

**"Confirm all fail safely": FAIL.** The resource boundary holds
(timeouts kill runaways, the heap cap raises catchable exhaustion, the
driver always survives), but there is currently no *action* boundary:
a rehearsed candidate can spawn processes, read/write the filesystem,
and open network connections, and it can read the hidden evaluator
corpus at the OS level. Additionally, `risk.py` under-classifies a
kernel-dispatch redefinition as R2 instead of R6. Do not rehearse
untrusted candidates until at least the R4 process/filesystem/network
gates and the hidden-corpus separation are enforced at the OS level.

## Reproduction

```powershell
uv run python attacks.py                                  # full suite (7 attacks; 1 TCP probe)
uv run python attacks.py infinite-loop memory-bomb        # named subset, no network
uv run python -m unittest tests.test_attacks -v           # harness unit tests (stubbed, no SBCL)
```
