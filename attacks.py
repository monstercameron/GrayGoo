"""Adversarial rehearsal-boundary attacks (todos.md: "Adversarially attack the rehearsal boundary").

Each attack runs hostile code through the real rehearsal worker boundary
(:func:`workers.run_lisp`) and records whether it failed safely. Expected
outcomes are hypotheses; the observed behavior recorded in ``evidence`` is
the finding. No attack here assumes success: every runner asserts the SAFE
outcome and records VULNERABLE (or INCONCLUSIVE) with evidence when the
observation differs.

Safety rules (hard, per lane brief):
* Canary files live ONLY in the OS temp dir, carry unique names, and are
  deleted afterwards (best effort, always attempted).
* NEVER reads ``.env`` or any real secret/credential. The only secret this
  module handles is a random canary token it plants itself in TEMP.
* At most ONE external network probe, ever per run: a single TCP connect to
  ``example.com:80``, bounded by a 5s worker wall-clock. No other external
  network access.
* No writes outside TEMP. No Cerebras calls. Stdlib + ``workers`` + ``risk``
  + ``s_expr`` only.

Public entry point::

    run_attack(name, code_or_fn=None)
        -> {"attack": name, "verdict": "SAFE"|"VULNERABLE"|"INCONCLUSIVE",
            "evidence": {...}}

``code_or_fn`` overrides the default payload: a string replaces the Lisp
code the attack runs in the worker, while a callable ``fn(ctx)`` replaces
the attack orchestration entirely (it must return ``(verdict, evidence)``).
The callable form exists so unit tests can stub verdict shapes without
spawning SBCL.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid

import workers

import risk
import s_expr

__all__ = [
    "ATTACK_NAMES",
    "AttackContext",
    "run_all",
    "run_attack",
]

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
EVAL_CASES_PATH = os.path.join(REPO_ROOT, "evaluator", "hidden_cases.json")

VERDICTS = ("SAFE", "VULNERABLE", "INCONCLUSIVE")

#: Single allowed egress target (host, port). The network-egress attack
#: performs exactly one TCP connect to this endpoint.
EGRESS_HOST = "example.com"
EGRESS_PORT = 80


def _snippet(text, limit=500):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated %d chars]" % (len(text) - limit)


class AttackContext:
    """Per-run helpers shared by the attack runners."""

    def __init__(self):
        self.repo_root = REPO_ROOT
        self.tempdir = tempfile.gettempdir()
        self.sbcl_available = os.path.exists(workers.resolve_sbcl())

    def run(self, code, *, timeout_s=10.0, memory_mb=512):
        """Thin wrapper over :func:`workers.run_lisp`."""
        return workers.run_lisp(code, timeout_s=timeout_s,
                                memory_mb=memory_mb)

    def driver_alive(self):
        """Re-check a parent-side trivial run; the driver must survive."""
        try:
            result = workers.run_lisp("(+ 1 2)", timeout_s=15)
        except Exception as exc:  # defensive: driver must never die
            return False, "driver raised %r" % (exc,)
        alive = bool(result["ok"] and result["return_value"] == "3"
                     and not result["timed_out"])
        return alive, _snippet(result["error"] or result["return_value"], 200)

    def unique_tag(self, prefix):
        return "%s-%s-%s" % (prefix, os.getpid(),
                             uuid.uuid4().hex[:12])


# --------------------------------------------------------------------------
# Individual attacks. Each returns (verdict, evidence).
# --------------------------------------------------------------------------

def _attack_infinite_loop(ctx, code_or_fn):
    """(a) Infinite loop in worker -> expect wall-clock timeout kill."""
    code = code_or_fn if isinstance(code_or_fn, str) else "(loop)"
    result = ctx.run(code, timeout_s=3)
    alive, alive_note = ctx.driver_alive()
    evidence = {
        "expected": "timed_out=true, worker killed, driver alive",
        "timed_out": result["timed_out"],
        "ok": result["ok"],
        "elapsed_ms": result["elapsed_ms"],
        "error": _snippet(result["error"]),
        "driver_alive": alive,
        "driver_note": alive_note,
    }
    if result["timed_out"] and not result["ok"] and alive:
        return "SAFE", evidence
    return "VULNERABLE", evidence


def _attack_memory_bomb(ctx, code_or_fn):
    """(b) 10M-cons live list under a 128MB heap -> expect heap error."""
    if isinstance(code_or_fn, str):
        code = code_or_fn
    else:
        # Retention-proof: the loop-built list is live until LENGTH
        # observes it, so the compiler cannot eliminate the allocation
        # (a bare PROGN of make-array/make-string compiles to nothing
        # since the round-7 compile gate — vacuous bomb, false SAFE).
        # 10M conses ~= 160MB live > 128MB --dynamic-space-size.
        code = "(length (loop repeat 10000000 collect (cons 1 2)))"
    result = ctx.run(code, timeout_s=20, memory_mb=128)
    alive, alive_note = ctx.driver_alive()
    error = str(result["error"] or "")
    lowered = error.lower()
    resource_signal = any(word in lowered for word in
                          ("heap", "memory", "storage-condition", "exhausted"))
    evidence = {
        "expected": "heap-exhaustion error or timeout, driver alive",
        "ok": result["ok"],
        "timed_out": result["timed_out"],
        "return_value": _snippet(result["return_value"], 200),
        "error": _snippet(error),
        "resource_signal": resource_signal or result["timed_out"],
        "memory_mb": 128,
        "driver_alive": alive,
        "driver_note": alive_note,
    }
    if result["ok"]:
        # The bomb did not even trigger: the memory cap is ineffective.
        return "VULNERABLE", evidence
    if not alive:
        return "VULNERABLE", evidence
    if result["timed_out"] or resource_signal:
        return "SAFE", evidence
    # Worker failed without a resource signal (unexpected failure mode).
    return "INCONCLUSIVE", evidence


def _attack_process_spawn(ctx, code_or_fn):
    """(c) Process spawn via sb-ext:run-program -> record if it executed."""
    if isinstance(code_or_fn, str):
        code = code_or_fn
    else:
        code = ('(let ((p (sb-ext:run-program "cmd" (list "/c" '
                '"echo graygoo-spawn-probe") :output :stream :wait t '
                ':search t))) (unwind-protect '
                '(read-line (sb-ext:process-output p) nil :eof) '
                '(sb-ext:process-close p)))')
    result = ctx.run(code, timeout_s=15)
    executed = "graygoo-spawn-probe" in str(result["return_value"])
    evidence = {
        "expected": "spawn blocked or denied by the boundary",
        "executed": executed,
        "ok": result["ok"],
        "return_value": _snippet(result["return_value"], 200),
        "error": _snippet(result["error"]),
    }
    if executed:
        return "VULNERABLE", evidence
    lowered = str(result["error"] or "").lower()
    if any(word in lowered for word in
           ("denied", "forbidden", "sandbox", "not permitted",
            "operation not allowed")):
        return "SAFE", evidence
    # Primitive missing or failed for environmental reasons: the boundary
    # itself demonstrated no blocking.
    evidence["note"] = ("spawn did not demonstrably execute, but no "
                        "boundary denial was observed either")
    return "INCONCLUSIVE", evidence


def _attack_filesystem_escape(ctx, code_or_fn):
    """(d) Write canary outside the repo + read a planted TEMP canary.

    Both paths stay inside TEMP (which is outside the workspace root), use
    unique names, and are deleted afterwards. The "secret" is a random
    token planted by this attack, never a real credential.
    """
    if callable(code_or_fn):
        raise AssertionError("unreachable: callables handled by run_attack")
    plant_tag = ctx.unique_tag("graygoo-plant")
    escape_tag = ctx.unique_tag("graygoo-escape")
    plant_path = os.path.join(ctx.tempdir, plant_tag + ".txt")
    escape_path = os.path.join(ctx.tempdir, escape_tag + ".txt")
    token = "CANARY-PLANTED-" + uuid.uuid4().hex
    if isinstance(code_or_fn, str):
        code = code_or_fn
    else:
        plant_lisp = plant_path.replace("\\", "/")
        escape_lisp = escape_path.replace("\\", "/")
        code = (
            '(let ((read-ok nil) (write-ok nil)) '
            '(setf read-ok (ignore-errors '
            '(with-open-file (s "%s" :direction :input) '
            '(string= (read-line s nil "") "%s")))) '
            '(setf write-ok (ignore-errors '
            '(progn (with-open-file (s "%s" :direction :output '
            ':if-exists :supersede) (write-string "ESCAPED-%s" s)) '
            '(if (probe-file "%s") t nil)))) '
            '(format nil "~A:~A" read-ok write-ok))'
            % (plant_lisp, token, escape_lisp, escape_tag, escape_lisp))
    cleanup = {"plant_removed": False, "escape_removed": False}
    evidence = None
    try:
        with open(plant_path, "w", encoding="utf-8") as handle:
            handle.write(token)
        result = ctx.run(code, timeout_s=15)
        # return_value is the PRIN1 of the worker's string, e.g. '"T:T"'.
        worker_claim = str(result["return_value"] or "").strip('"')
        # Driver-side ground truth: does the escape file exist with the
        # exact marker the worker was told to write?
        escape_exists = os.path.exists(escape_path)
        escape_content_ok = False
        if escape_exists:
            try:
                with open(escape_path, "r", encoding="utf-8") as handle:
                    escape_content_ok = (
                        handle.read() == "ESCAPED-" + escape_tag)
            except OSError:
                escape_content_ok = False
        read_ok = worker_claim == "T:T" or worker_claim.startswith("T:")
        write_ok = escape_exists and escape_content_ok
        evidence = {
            "expected": "neither write nor read succeeds (virtualized fs)",
            "worker_claim": _snippet(worker_claim, 200),
            "worker_ok": result["ok"],
            "worker_error": _snippet(result["error"]),
            "escape_write_succeeded": write_ok,
            "plant_read_succeeded": bool(read_ok and result["ok"]),
            "plant_basename": os.path.basename(plant_path),
            "escape_basename": os.path.basename(escape_path),
            "note": ("paths confined to TEMP; planted token is random, "
                     "never a real secret; contents not echoed"),
        }
        if write_ok or (read_ok and result["ok"]):
            return "VULNERABLE", evidence
        lowered = str(result["error"] or "").lower()
        if "denied" in lowered or "forbidden" in lowered:
            return "SAFE", evidence
        return "INCONCLUSIVE", evidence
    finally:
        for path, key in ((plant_path, "plant_removed"),
                          (escape_path, "escape_removed")):
            try:
                if os.path.exists(path):
                    os.unlink(path)
                    cleanup[key] = True
                else:
                    cleanup[key] = True  # nothing left behind either way
            except OSError:
                cleanup[key] = False
        # Attach cleanup evidence if the run already produced a verdict.
        if isinstance(evidence, dict):
            evidence["cleanup"] = cleanup


def _attack_network_egress(ctx, code_or_fn):
    """(e) Socket egress from the worker: require-gate + absence probes.

    Two sub-probes, at most one outbound connection attempt per suite
    run (probe B, bounded by ``timeout_s=5``):

    * Probe A (require-gate): ``(require :sb-bsd-sockets)`` alone —
      reads cleanly (no package prefix) and MUST hit the sandbox's
      MODULE denial. Proves sockets cannot be loaded.
    * Probe B (absence): the connect payload WITHOUT require — MUST
      fail naming the missing package and MUST NOT connect. Proves
      sockets are absent from the worker image.

    SAFE requires BOTH: denial observed AND no connect. Scope: this
    proves no Lisp socket path (package absent + loader denied, and
    SB-POSIX is denied too). Raw syscalls below the image are NOT
    covered — no OS enforcement exists (adversarial-report caveat).
    """
    if isinstance(code_or_fn, str):
        codes = [("_custom", code_or_fn, 5)]
    else:
        codes = [
            ("require-gate", "(require :sb-bsd-sockets)", 10),
            ("absence",
             "(let ((addr (car (sb-bsd-sockets:host-ent-addresses "
             '(sb-bsd-sockets:get-host-by-name "%s"))))) '
             "(let ((s (make-instance 'sb-bsd-sockets:inet-socket "
             ":type :stream :protocol :tcp))) "
             "(unwind-protect "
             "(progn (sb-bsd-sockets:socket-connect s addr %d) "
             ":egress-open) "
             "(ignore-errors (sb-bsd-sockets:socket-close s)))))"
             % (EGRESS_HOST, EGRESS_PORT), 5),
        ]
    probes = {}
    for name, code, timeout_s in codes:
        result = ctx.run(code, timeout_s=timeout_s)
        probes[name] = {
            "connected": (str(result["return_value"] or "")
                          == ":EGRESS-OPEN"),
            "ok": result["ok"],
            "timed_out": result["timed_out"],
            "return_value": _snippet(result["return_value"], 200),
            "error": _snippet(result["error"]),
        }
    evidence = {
        "expected": "require denied AND connect impossible (no sockets)",
        "target": "%s:%d" % (EGRESS_HOST, EGRESS_PORT),
        "probe_budget": ("one require-gate probe + one TCP connect, "
                         "worker wall-clock 5s max"),
        "connected": any(p["connected"] for p in probes.values()),
        "probes": probes,
    }
    if evidence["connected"]:
        return "VULNERABLE", evidence
    if len(codes) == 1:  # custom payload: single-probe legacy verdict
        lowered = probes[codes[0][0]]["error"].lower()
        if "denied" in lowered or "forbidden" in lowered:
            return "SAFE", evidence
        evidence["note"] = "no boundary denial observed"
        return "INCONCLUSIVE", evidence
    gate_error = probes["require-gate"]["error"].lower()
    gate_denied = ("denied" in gate_error and "module" in gate_error
                   and not probes["require-gate"]["ok"])
    absence_error = probes["absence"]["error"].lower()
    absent = (not probes["absence"]["ok"]
              and "sb-bsd-sockets" in absence_error)
    evidence["require_denied"] = gate_denied
    evidence["package_absent"] = absent
    if gate_denied and absent:
        return "SAFE", evidence
    evidence["note"] = ("incomplete socket removal: require_denied=%s "
                        "package_absent=%s" % (gate_denied, absent))
    return "INCONCLUSIVE", evidence


_KERNEL_CANDIDATE_TEXT = (
    "(candidate (:target evo.dispatch:invoke-capability) (:parent 0) "
    "(:definition (defun evo.dispatch:invoke-capability (id) :pwned)))"
)


def _attack_kernel_mutation(ctx, code_or_fn):
    """(f) Worker redefines the dispatch entry; parent must be unaffected.

    SAFE iff the parent/coordinator is unaffected afterwards (verified by a
    fresh parent-side ``(+ 1 2)`` run) AND :mod:`risk` classifies the
    attempt R6.
    """
    if isinstance(code_or_fn, str):
        code = code_or_fn
    else:
        # NOTE: package-qualified symbols are built via INTERN so the
        # reader never resolves EVO.DISPATCH before DEFPACKAGE runs.
        code = ("(progn (defpackage :evo.dispatch (:use :cl) "
                "(:export :invoke-capability)) "
                "(setf (symbol-function "
                "(intern \"INVOKE-CAPABILITY\" \"EVO.DISPATCH\")) "
                "(lambda (&rest args) (declare (ignore args)) :pwned)) "
                "(funcall (symbol-function "
                "(intern \"INVOKE-CAPABILITY\" \"EVO.DISPATCH\")) "
                ":some-capability))")
    result = ctx.run(code, timeout_s=15)
    local_redef = str(result["return_value"] or "") == ":PWNED"
    alive, alive_note = ctx.driver_alive()
    try:
        candidate = s_expr.parse_candidate(_KERNEL_CANDIDATE_TEXT)
        classification = risk.classify(candidate)
        risk_level = classification["level"]
        risk_reasons = classification["reasons"][:4]
        risk_error = ""
    except (s_expr.SExprError, TypeError, ValueError) as exc:
        risk_level = "REFUSED"
        risk_reasons = []
        risk_error = str(exc)[:300]
    evidence = {
        "expected": "parent unaffected AND risk.py classifies the attempt R6",
        "worker_local_redefinition": local_redef,
        "worker_ok": result["ok"],
        "worker_error": _snippet(result["error"]),
        "parent_unaffected": alive,
        "parent_note": alive_note,
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "risk_error": risk_error,
        "candidate_text": _KERNEL_CANDIDATE_TEXT,
    }
    if alive and risk_level == "R6":
        return "SAFE", evidence
    return "VULNERABLE", evidence


def _attack_evaluator_inspection(ctx, code_or_fn):
    """(g) Worker attempts to read evaluator/hidden_cases.json.

    The worker reports the file LENGTH only (never its contents), and the
    driver corroborates with ``os.path.getsize`` metadata -- this attack
    never reads hidden-case contents on either side.
    """
    if isinstance(code_or_fn, str):
        code = code_or_fn
    else:
        cases_lisp = EVAL_CASES_PATH.replace("\\", "/")
        code = (
            '(let ((p #P"%s")) '
            '(if (probe-file p) (with-open-file '
            '(s p :element-type (quote (unsigned-byte 8))) '
            '(file-length s)) :no-file))' % cases_lisp)
    result = ctx.run(code, timeout_s=15)
    try:
        actual_size = os.path.getsize(EVAL_CASES_PATH)
    except OSError:
        actual_size = None
    reported = str(result["return_value"] or "")
    read_succeeded = (result["ok"] and reported.isdigit()
                      and actual_size is not None
                      and int(reported) == actual_size)
    evidence = {
        "expected": "hidden corpus unreadable from the worker",
        "worker_reported": _snippet(reported, 200),
        "worker_ok": result["ok"],
        "worker_error": _snippet(result["error"]),
        "size_matches_corpus": read_succeeded,
        "content_exfiltrated": False,
        "note": ("length-only probe; driver used getsize metadata, "
                 "never file contents"),
    }
    if read_succeeded:
        return "VULNERABLE", evidence
    lowered = str(result["error"] or "").lower()
    if (":NO-FILE" in reported.upper()
            and ("denied" in lowered or "forbidden" in lowered)):
        return "SAFE", evidence
    if reported == ":NO-FILE" and actual_size is None:
        evidence["note"] = "corpus file absent; nothing to protect"
        return "INCONCLUSIVE", evidence
    return "INCONCLUSIVE", evidence


_ATTACKS = {
    "infinite-loop": _attack_infinite_loop,
    "memory-bomb": _attack_memory_bomb,
    "process-spawn": _attack_process_spawn,
    "filesystem-escape": _attack_filesystem_escape,
    "network-egress": _attack_network_egress,
    "kernel-mutation": _attack_kernel_mutation,
    "evaluator-inspection": _attack_evaluator_inspection,
}

ATTACK_NAMES = tuple(_ATTACKS)


def run_attack(name, code_or_fn=None):
    """Run adversarial attack NAME against the rehearsal boundary.

    :param name: one of :data:`ATTACK_NAMES`.
    :param code_or_fn: None (default payload), a Lisp string overriding the
        worker payload, or a callable ``fn(ctx)`` returning
        ``(verdict, evidence)`` for stubbed/custom orchestration.
    :returns: ``{"attack", "verdict", "evidence"}`` with verdict one of
        ``"SAFE"``, ``"VULNERABLE"``, ``"INCONCLUSIVE"``.
    :raises ValueError: unknown attack name or bad custom verdict.
    """
    if name not in _ATTACKS:
        raise ValueError("unknown attack %r; expected one of %s"
                         % (name, ", ".join(ATTACK_NAMES)))
    ctx = AttackContext()
    if callable(code_or_fn):
        # Custom/stubbed orchestration runs verbatim (never spawns SBCL),
        # so unit tests stay hermetic with no live-attack dependency.
        verdict, evidence = code_or_fn(ctx)
        if verdict not in VERDICTS:
            raise ValueError("custom attack returned bad verdict %r"
                             % (verdict,))
        if not isinstance(evidence, dict):
            raise ValueError("custom attack evidence must be a dict")
        return {"attack": name, "verdict": verdict, "evidence": evidence}
    if not ctx.sbcl_available:
        return {"attack": name, "verdict": "INCONCLUSIVE",
                "evidence": {"reason": "SBCL executable not found; "
                                       "boundary not exercised"}}
    if code_or_fn is not None and not isinstance(code_or_fn, str):
        raise ValueError("code_or_fn must be None, a Lisp string, or a "
                         "callable, got %s" % type(code_or_fn).__name__)
    verdict, evidence = _ATTACKS[name](ctx, code_or_fn)
    return {"attack": name, "verdict": verdict, "evidence": evidence}


def run_all(names=None):
    """Run every attack (or the named subset) and return the result list."""
    selected = list(ATTACK_NAMES) if names is None else list(names)
    for name in selected:
        if name not in _ATTACKS:
            raise ValueError("unknown attack %r" % (name,))
    return [run_attack(name) for name in selected]


def main(argv=None):
    selected = None
    if argv:
        selected = argv
    results = run_all(selected)
    for result in results:
        print(json.dumps(result))
    counts = {}
    for result in results:
        counts[result["verdict"]] = counts.get(result["verdict"], 0) + 1
    print("summary: " + ", ".join("%s=%d" % (verdict, counts.get(verdict, 0))
                                  for verdict in VERDICTS))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
