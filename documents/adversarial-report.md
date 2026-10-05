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

## Re-run after sandbox hardening (2026-10-05, same-day follow-up)

Hardening applied since the first run (lane: sandbox-h):

- `risk.py` (additive): targets under `evo.dispatch`/`evo.kernel`-style
  packages, and `defun`/`defmethod`/`setf`-of-function-cell redefinitions
  of dispatch entrypoint symbols, classify R6 (promotion-forbidden).
- `sandbox.py` (new): `refuse_to_rehearse` policy gate (R6 always refused;
  process/network-write payloads always refused; other R4 refused unless
  approved), `WORKER_PRELUDE`/`build_prelude`, `WorkerJail` (OverlayFS +
  StateSandbox audit/cwd-pinning, not OS confinement).
- `src/worker/worker.lisp` (additive): `install-worker-sandbox` denies
  file/process/foreign-module ops in-image, gates REQUIRE against a module
  denylist, disables UNLOCK-PACKAGE, re-locks CL/SB-EXT/SB-ALIEN. Verified
  working on SBCL 2.6.9; bypassable in-image (SB-UNIX/SB-IMPL/SB-ALIEN).
- `workers.py` (additive): `run_lisp`/`WorkerPool` install the prelude by
  default (`sandbox=True`, opt-out `False`) with optional `jail` cwd
  pinning; `graygoo-eval-all` now binds `*read-eval*` to NIL (issues.md
  #1 fix) on every worker read path for untrusted input.
- `evaluator/service.py` (additive): corpus path via `$GRAYGOO_EVAL_CORPUS`
  (`resolve_corpus_path`: explicit arg > env > in-checkout default);
  production separation documented in `evaluator/README.md`.
- `tests/test_sandbox.py` (new, 21 tests): R6 rule, policy gate, jail,
  corpus env override, sandboxed-worker and `#.` attack cases.

Re-run command: `python attacks.py` (unmodified harness). Observed:
**SAFE = 5, VULNERABLE = 0, INCONCLUSIVE = 2** (was 2/5/0).

### Per-attack before/after (harness verdicts, quoted exactly)

| Attack | Before | After | After-evidence (observed) | Honest residual |
|---|---|---|---|---|
| infinite-loop | SAFE | SAFE | `timed_out=true` after 3.1s, driver alive | None. Unchanged. |
| memory-bomb | SAFE | SAFE | `HEAP-EXHAUSTED-ERROR`, driver alive | None (heap only; RSS cap still missing, as before). |
| process-spawn | VULNERABLE | SAFE | `executed=false`; worker error: `rehearsal sandbox denied: SB-EXT:RUN-PROGRAM is forbidden in this worker` | SAFE against the documented payload only. Lisp-level block; an adapted payload via SB-UNIX/FFI is not stopped. No OS sandbox yet. |
| filesystem-escape | VULNERABLE | INCONCLUSIVE | worker claim `NIL:NIL`; `escape_write_succeeded=false`, `plant_read_succeeded=false`; canaries cleaned up | Payload blocked as written, but the harness cannot confirm SAFE (payload swallows the denial in `ignore-errors`), and the block is bypassable in-image. **Treat the boundary as still VULNERABLE.** OS jail still required. |
| network-egress | VULNERABLE | SAFE | `connected=false`; worker error: `rehearsal sandbox denied: MODULE SB-BSD-SOCKETS is forbidden in this worker`, denied at `require` before any socket code ran | SAFE against the documented payload only; same Lisp-level caveat as process-spawn. **Zero packets emitted on this re-run** (see safety). |
| kernel-mutation | VULNERABLE | SAFE | `risk_level=R6` with `R6: ... redefines protected dispatch/kernel symbol 'evo.dispatch:invoke-capability' via defun` + `R6: mutation target ... (promotion-forbidden)`; parent unaffected | Worker-local redefinition still succeeds (`worker_local_redefinition=true`) -- expected and harmless (separate process, kernel not loaded there). Policy layer (`refuse_to_rehearse`) now refuses R6 before any rehearsal. |
| evaluator-inspection | VULNERABLE | INCONCLUSIVE | `size_matches_corpus=false`; worker error: `rehearsal sandbox denied: COMMON-LISP:PROBE-FILE is forbidden in this worker` | Payload blocked as written, but the corpus file is still OS-readable by the worker's user on this host; only separate-host/user separation (now configurable via `$GRAYGOO_EVAL_CORPUS`, not yet deployed) truly fixes this. **Treat as still VULNERABLE.** |

### issues.md #1 `#.` reader-evaluation case (new; outside `attacks.py`)

Oracle payload: `#.(progn (defun graygoo-read-pwn () :pwned) :x)` plus a
second form reporting whether the marker was defined. Before the fix
(observed this session, pre-fix probe): worker returned
`:READ-EVAL-FIRED` with `ok=true` -- reader evaluation fired (VULNERABLE).
After binding `*read-eval*` NIL in `graygoo-eval-all`: worker fails with
`can't read #. while *READ-EVAL* is NIL`, verified both sandboxed and
`sandbox=False` (the fix lives in the read path, not the prelude) (SAFE).
Regression tests:
`tests/test_sandbox.py::ReaderEvalAttackTest` (2 tests). Note: the
capability-registry read paths (`src/capability/registry.lisp`,
`load-capability`/`load-registry`) already bound `*read-eval*` NIL and
were left untouched (outside this lane).

### Safety compliance of the re-run

- Canaries only in `%TEMP%` with unique names; attacks.py removed its own
  (`plant_removed=true`, `escape_removed=true`); zero `graygoo-*` leftovers
  from this lane verified afterwards (one foreign `graygoo-cap-probe`
  entry belongs to a concurrent lane and was not touched).
- Never read `.env` or any real secret; hidden-corpus probe stayed
  length-only on the worker side and `getsize` metadata on the driver side.
- Network: the single documented `example.com:80` probe was authorized but
  UNSPENT -- the worker was denied at `(require :sb-bsd-sockets)` before
  any DNS/TCP I/O, so this re-run emitted zero packets. No other external
  network anywhere in this lane.
- Zero Cerebras calls. No writes outside TEMP except the owned deliverables
  listed above. No git commands.

### Overall statement (updated)

**"Confirm all fail safely": still FAIL, but narrower.** No attack payload
now succeeds as written (0 VULNERABLE by harness verdict), and the R6
classifier gap plus the `#.` reader gap are closed with regression tests.
But two of the seven verdicts are INCONCLUSIVE rather than SAFE, and every
in-worker block is Lisp-level only: a candidate that reaches past the
denied high-level operators (SB-UNIX/SB-IMPL internals, SB-ALIEN on loaded
libraries) still runs with the user's full OS rights. The filesystem and
hidden-corpus boundaries in particular must be treated as VULNERABLE until
OS-level isolation (separate worker user/host, deny ACLs, job objects)
lands. Do not rehearse untrusted candidates yet.

## Re-run 3 after round-3 fixes (2026-10-05, coordinator)

Fixes since re-run 2 (all with regression tests, full suite 625 OK):

- issues #71/#72: pipeline fails closed on missing classifier and on
  zero requested checks (was: executed / passed vacuously).
- issues #75: missing `sandbox.py` raises instead of silently running
  unsandboxed.
- issues #1 (reopen): `graygoo-eval-all` reads ALL forms under
  `*read-eval* NIL` before evaluating any (was: interleaved
  read/eval, re-enablable).
- issues #46: worker `Popen` uses a sanitized env allowlist (no
  credential inheritance).
- issues #78/#79: performance without budget fails (measured, not
  passed); raw sqlite3 escape hatch removed.

Re-run command: `uv run python attacks.py` (unmodified harness).
Observed: **SAFE = 4, VULNERABLE = 0, INCONCLUSIVE = 3** (was 5/0/2).

| Attack | Re-run 2 | Re-run 3 | Note |
|---|---|---|---|
| infinite-loop | SAFE | SAFE | Unchanged (`timed_out`, driver alive). |
| memory-bomb | SAFE | SAFE | Unchanged (heap-exhausted, driver alive). |
| process-spawn | SAFE | SAFE | Unchanged (sandbox denies `RUN-PROGRAM`). |
| filesystem-escape | INCONCLUSIVE | INCONCLUSIVE | Unchanged (`NIL:NIL`, canaries cleaned). |
| network-egress | SAFE | **INCONCLUSIVE** | Verdict artifact, not a bypass (see below). |
| kernel-mutation | SAFE | SAFE | Unchanged (R6 + parent unaffected). |
| evaluator-inspection | INCONCLUSIVE | INCONCLUSIVE | Unchanged (`PROBE-FILE` denied). |

**network-egress SAFE -> INCONCLUSIVE is a harness-signal change, not a
security regression.** The #1 read-all-first fix reads the payload's
`sb-bsd-sockets:`-prefixed second form before the first form's
`(require :sb-bsd-sockets)` evaluates, so the run now fails at read
time (`Package SB-BSD-SOCKETS does not exist`) instead of at the
sandbox's require-deny. The payload still fails (`connected=false`,
`ok=false`, zero packets); the sandbox require-deny is intact at eval
time for anything that reads cleanly. The harness reports
INCONCLUSIVE because it observes no explicit boundary denial. The #1
hole closure (read-eval can no longer be re-enabled mid-stream) is
strictly more valuable than the lost denial signal.

Overall statement stands: 0 VULNERABLE as written, but 3
INCONCLUSIVE + Lisp-only blocks mean the boundary must still be
treated as VULNERABLE until OS isolation lands. todos.md adversarial
boxes stay open.

## Re-run 4: vacuous-bomb discovery + retention-proof fix (2026-10-05)

A fresh `uv run python attacks.py` reported **memory-bomb VULNERABLE**
(`ok=true`, `:BOMB-DID-NOT-TRIGGER`) — the first VULNERABLE since the
suite began. Investigation showed a HARNESS bug, not a sandbox
regression: the round-7 compile gate compiles worker forms, and the
old bomb `(progn (make-array ...) (make-string ...) ...)` discards
both allocations, so SBCL eliminates them as dead code — the "bomb"
compiled to just the keyword. Re-run 3's SAFE predates the compile
gate (interpreted EVAL really allocated).

Proof the cap is real (not the harness): a retention-proof payload —
`(length (loop repeat 10000000 collect (cons 1 2)))`, 10M live conses
~= 160MB — run directly under
`sbcl --dynamic-space-size 128` dies with `Heap exhausted during
garbage collection: 0 bytes available`, exit 1.

Fixes (all committed with regression tests):

- `attacks.py`: memory-bomb is now the retention-proof 10M-cons
  payload; the old vacuous shape is documented in-code as a warning.
- `workers.py`: no-envelope errors now carry stderr HEAD (first 500
  chars) plus tail (was: tail only). Fatal runtime conditions print
  at stderr's head; the tail held only backtrace frames, so the
  "Heap exhausted" signal was invisible to the driver.
- `tests/test_workers.py`: `test_heap_cap_kills_bomb_and_reports_head`
  pins the enforcement end-to-end (worker dies naming heap
  exhaustion, driver survives).

Re-run 4 observed: **SAFE = 4, VULNERABLE = 0, INCONCLUSIVE = 3** —
same shape as re-run 3, but the memory-bomb SAFE is now earned by a
live 160MB allocation against a 128MB cap instead of by dead code.
The 3 INCONCLUSIVE (filesystem-escape, network-egress,
evaluator-inspection) are unchanged: Lisp-level blocks with no OS
enforcement behind them. todos.md: process-spawn checked (denial
proven); filesystem/network/evaluator/confirm-all stay open.

## Re-run 5: two-probe network-egress goes conclusive (2026-10-05)

The old egress attack bundled `(require :sb-bsd-sockets)` with the
connect payload in one worker run; since the #1 read-all-first fix
the run dies at read time on the package prefix, so no boundary
signal was ever observed (INCONCLUSIVE by construction). The attack
now runs TWO probes (`attacks.py`, hermetic verdict tests in
`tests/test_attacks.py`):

- Probe A (require-gate): `(require :sb-bsd-sockets)` alone — MUST
  hit the sandbox MODULE denial. Observed: `rehearsal sandbox
  denied: MODULE SB-BSD-SOCKETS is forbidden in this worker`.
- Probe B (absence): connect payload without require — MUST fail
  naming the missing package and MUST NOT connect. Observed:
  `Package SB-BSD-SOCKETS does not exist`, connected=false.

Re-run 5 observed: **SAFE = 5, VULNERABLE = 0, INCONCLUSIVE = 2**
(filesystem-escape, evaluator-inspection). todos.md: network-escape
checked (no Lisp socket path: package absent + loader denied +
SB-POSIX denied + LOAD-SHARED-OBJECT denied).

Scope honesty: the SAFE proves no *convenient* socket path. The
worker header states the residual: SB-ALIEN routines on
already-loaded libraries (ws2_32 is in every Windows process) stay
reachable in-image, and raw syscalls below the image are unconfined.
Full "control worker network egress" still wants the firewall half;
the todos.md hardening box stays open.
