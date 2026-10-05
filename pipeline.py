"""Mutation-pipeline orchestrator (todos.md "Build the mutation pipeline").

Implements the rehearsal flow from plan.md sections 21 (rehearsal phases)
and 68-70 (task/candidate/promotion algorithms): parse a ``(candidate ...)``
form, classify risk, then run direct, regression, property, differential
(old-vs-new), and performance checks — cheap checks first with early stop
on decisive failure — and produce ONE compact verdict.

All sibling dependencies are INJECTED callables; this module imports only
the standard library and :mod:`s_expr`. In particular it never imports
``workers.py`` or ``risk.py`` (built concurrently). Production wiring is::

    run_candidate(text, tests=..., worker_fn=workers.run, risk_fn=risk.classify)

Contracts for the injected callables:

* ``worker_fn(code)`` takes a ``str`` of code and returns the worker
  envelope dict ``{"ok", "stdout", "return_value", "error", "timed_out",
  "elapsed_ms", "candidate_ms?", "error_type?", "return_truncated?"}``
  (``candidate_ms`` is the in-worker candidate-eval time;
  ``error_type`` names the signalled condition; ``return_truncated``
  flags abbreviated values, which fail comparisons as unverifiable;
  older doubles may omit the ``?`` keys).
* ``risk_fn(parsed)`` takes the parsed-candidate dict from
  :func:`s_expr.parse_candidate` and returns a dict with at least
  ``"level"`` (``"R0"``..``"R6"`` per plan.md section 7). ``None``, a
  classifier exception, a malformed return, or an ``"unclassified"``
  level all fail the run before any worker use (fail closed).
"""

from __future__ import annotations

import s_expr

#: Check stages in cheap-first execution order (plan.md section 69:
#: "run cheap checks ... if fail -> stop").
STAGE_ORDER = ("direct", "regression", "property", "differential", "performance")

_OK = "pass"
_FAIL = "fail"
_SKIPPED = "skipped"


def _run_worker(worker_fn, code):
    """Run one code snippet via the injected worker. Never raises.

    Returns ``(passed, record)`` where ``record`` summarizes the
    envelope (or the harness failure when the worker misbehaves).
    """
    if worker_fn is None:
        return False, {
            "pass": False,
            "error": "no worker_fn injected; cannot execute checks",
        }
    if not isinstance(code, str):
        return False, {
            "pass": False,
            "error": "check item code must be a str, got %s"
            % type(code).__name__,
        }
    try:
        result = worker_fn(code)
    except Exception as exc:  # noqa: BLE001 - worker failures are evidence
        return False, {
            "pass": False,
            "error": "worker_fn raised %s: %s"
            % (type(exc).__name__, exc),
        }
    if not isinstance(result, dict):
        return False, {
            "pass": False,
            "error": "worker_fn returned %s, expected envelope dict"
            % type(result).__name__,
        }
    if result.get("timed_out"):
        return False, {
            "pass": False,
            "timed_out": True,
            "elapsed_ms": result.get("elapsed_ms"),
            "error": result.get("error") or "worker timed out",
        }
    passed = bool(result.get("ok"))
    record = {
        "pass": passed,
        "elapsed_ms": result.get("elapsed_ms"),
        "candidate_ms": result.get("candidate_ms"),
        "error_type": result.get("error_type", ""),
        "return_truncated": bool(result.get("return_truncated", False)),
        "stdout": result.get("stdout"),
        "return_value": result.get("return_value"),
    }
    if not passed:
        record["error"] = result.get("error") or "worker reported ok=False"
    return passed, record


def _return_values_match(actual, expected):
    """True when a worker ``return_value`` matches the expected value.

    The real worker always reports ``return_value`` as a PRIN1 string
    (e.g. ``"3"``), so a raw ``==`` against a numeric ``expect`` (``3``)
    can never pass. Compare printed representations as a fallback so
    equivalent numeric expects match the real envelope.
    """
    if actual == expected:
        return True
    try:
        return str(actual).strip() == str(expected).strip()
    except Exception:  # noqa: BLE001 - unprintable values simply mismatch
        return False


def _eval_code_item(item, worker_fn):
    """Evaluate one direct/regression/property item.

    An item is either a ``str`` of code (passes when the worker envelope
    reports ``ok`` without timeout) or a ``{"code", "expect"?}`` dict
    (additionally requires ``return_value`` to match ``expect`` under
    :func:`_return_values_match`; the ``"expected"`` spelling is
    accepted as an alias).
    """
    if isinstance(item, str):
        return _run_worker(worker_fn, item)
    if not isinstance(item, dict) or "code" not in item:
        return False, {
            "pass": False,
            "error": "check item must be a code str or a {'code', ...} "
            "dict, got %r" % (item,),
        }
    passed, record = _run_worker(worker_fn, item["code"])
    if not passed:
        return passed, record
    if "expect" in item or "expected" in item:
        expected = item.get("expect", item.get("expected"))
        if record.get("return_truncated"):
            # Issues.md #57/#58: an abbreviated value cannot verify a
            # comparison — fail as unverifiable, not as a mismatch.
            record = dict(record)
            record["pass"] = False
            record["error"] = ("return_value abbreviated past the "
                               "transport cap: comparison unverifiable")
            return False, record
        if not _return_values_match(record.get("return_value"), expected):
            record = dict(record)
            record["pass"] = False
            record["error"] = "return_value %r != expected %r" % (
                record.get("return_value"),
                expected,
            )
            return False, record
    return True, record


def _run_item_stage(items, worker_fn):
    """Run a list of code items; returns ``(passed, evidence)``.

    A stage that runs zero items fails instead of passing vacuously.
    """
    if isinstance(items, (str, dict)):
        items = [items]
    evidence_items = []
    failures = 0
    for index, item in enumerate(items):
        passed, record = _eval_code_item(item, worker_fn)
        record = {"index": index, "item": _describe_item(item), **record}
        evidence_items.append(record)
        if not passed:
            failures += 1
    evidence = {
        "total": len(evidence_items),
        "failed": failures,
        "items": evidence_items,
    }
    if not evidence_items:
        evidence["error"] = "stage ran zero items; empty check list fails"
        return False, evidence
    return failures == 0, evidence


def _describe_item(item):
    if isinstance(item, str):
        return item if len(item) <= 120 else item[:117] + "..."
    if isinstance(item, dict):
        return {k: v for k, v in item.items() if k in ("code", "expect",
                                                      "expected")}
    return repr(item)


def check_direct(parsed, spec, worker_fn):
    """Run direct candidate tests (plan.md phase C)."""
    del parsed  # direct items are self-contained code; candidate already parsed
    return _run_item_stage(spec, worker_fn)


def check_regression(parsed, spec, worker_fn):
    """Run regression tests guarding existing behavior (plan.md phase C)."""
    del parsed
    return _run_item_stage(spec, worker_fn)


def check_property(parsed, spec, worker_fn):
    """Run property/invariant checks (plan.md phase D)."""
    del parsed
    return _run_item_stage(spec, worker_fn)


def _resolve_side(side, worker_fn):
    """Resolve one differential side to ``(ok, value, record)``.

    A side is either a literal value or a ``{"code": ...}`` dict whose
    ``return_value`` is produced by executing the code in a worker.
    """
    if isinstance(side, dict) and "code" in side:
        passed, record = _run_worker(worker_fn, side["code"])
        if not passed:
            return False, None, record
        return True, record.get("return_value"), record
    return True, side, {"pass": True, "literal": True}


def check_differential(parsed, spec, worker_fn):
    """Run old-vs-new differential tests (plan.md phase E).

    ``spec`` is a ``{"cases": [...]}`` dict (a bare list of cases is also
    accepted). Each case is ``{"old": ..., "new": ...}`` or an
    ``[old, new]`` pair; divergence between the resolved sides fails
    the stage and is reported as evidence.
    """
    del parsed
    if isinstance(spec, dict):
        cases = spec.get("cases", [])
    else:
        cases = spec
    if not isinstance(cases, (list, tuple)):
        return False, {"error": "differential cases must be a list, got %r"
                       % (cases,)}
    if len(cases) == 0:
        return False, {"total": 0, "divergences": 0, "errors": 0,
                       "cases": [],
                       "error": "differential ran zero cases; "
                       "empty check list fails"}
    evidence_cases = []
    divergences = 0
    errors = 0
    for index, case in enumerate(cases):
        if isinstance(case, dict) and "old" in case and "new" in case:
            old_side, new_side = case["old"], case["new"]
        elif isinstance(case, (list, tuple)) and len(case) == 2:
            old_side, new_side = case
        else:
            errors += 1
            evidence_cases.append({"index": index, "pass": False,
                                   "error": "case must be {'old','new'} or "
                                   "[old, new], got %r" % (case,)})
            continue
        old_ok, old_value, old_record = _resolve_side(old_side, worker_fn)
        new_ok, new_value, new_record = _resolve_side(new_side, worker_fn)
        if not (old_ok and new_ok):
            errors += 1
            evidence_cases.append({
                "index": index, "pass": False,
                "old": old_record, "new": new_record,
                "error": "could not resolve both sides",
            })
            continue
        diverged = old_value != new_value
        if diverged:
            divergences += 1
        evidence_cases.append({
            "index": index,
            "pass": not diverged,
            "old_value": old_value,
            "new_value": new_value,
            **({"diverged": True} if diverged else {}),
        })
    evidence = {
        "total": len(evidence_cases),
        "divergences": divergences,
        "errors": errors,
        "cases": evidence_cases,
    }
    return divergences == 0 and errors == 0, evidence


def check_performance(parsed, spec, worker_fn):
    """Run performance checks against a wall-clock budget (plan.md G).

    ``spec`` is a ``{"budget_ms"?, "cases"?, "samples"?}`` dict: ``cases``
    are code snippets executed in workers (their in-worker
    ``candidate_ms`` is measured when reported, else driver
    ``elapsed_ms``), ``samples`` are pre-measured numbers.
    Any sample exceeding ``budget_ms`` fails the stage. With no budget
    the stage records its samples (``measured: True``) and FAILS: a
    measurement without a threshold is not verification. A spec that
    yields zero samples fails instead of passing vacuously.
    """
    del parsed
    if not isinstance(spec, dict):
        return False, {"error": "performance spec must be a dict, got %r"
                       % (spec,)}
    budget = spec.get("budget_ms")
    samples = []
    errors = []
    for raw in spec.get("samples", []) or []:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            errors.append("sample must be a number, got %r" % (raw,))
        else:
            samples.append(raw)
    for index, code in enumerate(spec.get("cases", []) or []):
        passed, record = _run_worker(worker_fn, code)
        if not passed:
            errors.append("case %d failed: %s"
                          % (index, record.get("error", "worker failure")))
            continue
        # Issues.md #77: prefer in-worker candidate time (excludes
        # spawn + SBCL startup); fall back to driver wall time for
        # workers that predate candidate_ms.
        elapsed = record.get("candidate_ms")
        if isinstance(elapsed, bool) or not isinstance(
                elapsed, (int, float)):
            elapsed = record.get("elapsed_ms")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
            errors.append("case %d reported non-numeric elapsed_ms %r"
                          % (index, elapsed))
        else:
            samples.append(elapsed)
    violations = [] if budget is None else [s for s in samples if s > budget]
    evidence = {
        "budget_ms": budget,
        "samples": samples,
        "max_ms": max(samples) if samples else None,
        "violations": violations,
        "errors": errors,
    }
    if errors:
        return False, evidence
    if not samples:
        evidence["error"] = "performance ran zero samples; empty check list fails"
        return False, evidence
    if violations:
        return False, evidence
    if budget is None:
        # Issue 78: a measurement without an acceptance threshold is
        # telemetry, not verification — it must not satisfy the gate.
        evidence["measured"] = True
        evidence["error"] = (
            "performance measured %d sample(s) without budget_ms: "
            "measurement is not verification" % len(samples))
        return False, evidence
    return True, evidence


_CHECKS = {
    "direct": check_direct,
    "regression": check_regression,
    "property": check_property,
    "differential": check_differential,
    "performance": check_performance,
}


def _risk_blocks(risk):
    """True when a risk classification decisively blocks promotion.

    Plan.md section 7 R6 forbids mutating the trusted kernel/evaluator,
    so an R6 classification (or an explicit ``blocked`` flag) stops the
    pipeline before any worker is used.
    """
    if not isinstance(risk, dict):
        return False
    if risk.get("blocked"):
        return True
    level = str(risk.get("level", "")).strip().upper()
    return level == "R6" or level.startswith("R6 ") or level.startswith("R6-") \
        or level.startswith("R6:")


def run_candidate(candidate_text, *, tests, worker_fn, risk_fn=None):
    """Rehearse one candidate and return a single compact result dict.

    :param candidate_text: raw ``(candidate ...)`` S-expression text.
    :param tests: mapping of stage name to stage spec (``"direct"``,
        ``"regression"``, ``"property"``, ``"differential"``,
        ``"performance"``); absent stages are skipped. Unknown stage
        names fail the run instead of being silently ignored.
    :param worker_fn: injected ``worker_fn(code)`` callable returning the
        worker envelope.
    :param risk_fn: injected ``risk_fn(parsed)`` classifier, or ``None``.
    :returns: ``{"ok", "verdict", "failures", "evidence", "risk",
        "stages"}`` where ``verdict`` is the one compact verdict dict
        ``{"pass", "reason", "failed_stage"}``.

    Ordering follows plan.md section 69: parse, then risk, then cheap
    checks first with early stop on the first decisive failure (later
    stages are marked ``skipped``). Malformed input fails immediately
    without touching ``worker_fn``.
    """
    stages = []
    failures = []
    evidence = {}

    def _record(name, status, reason=None):
        entry = {"name": name, "status": status}
        if reason is not None:
            entry["reason"] = reason
        stages.append(entry)
        return entry

    def _fail(name, reason):
        failures.append({"stage": name, "reason": reason})
        _record(name, _FAIL, reason)

    def _skip_rest(names, reason):
        for name in names:
            _record(name, _SKIPPED, reason)

    def _finish(ok, failed_stage, reason):
        return {
            "ok": ok,
            "verdict": {
                "pass": ok,
                "reason": reason,
                "failed_stage": failed_stage,
            },
            "failures": failures,
            "evidence": evidence,
            "risk": risk,
            "stages": stages,
        }

    # -- Stage (a): parse via s_expr. Malformed input fails immediately
    # -- and must not touch worker_fn (or risk_fn: nothing to classify).
    try:
        parsed = s_expr.parse_candidate(candidate_text)
    except s_expr.SExprError as exc:
        risk = {"level": "unclassified",
                "note": "parse failed; candidate not classified"}
        _fail("parse", "malformed candidate: %s" % (exc,))
        _skip_rest(["risk"] + list(STAGE_ORDER),
                   "short-circuited after parse failure")
        return _finish(False, "parse", failures[-1]["reason"])
    _record("parse", _OK)
    evidence["parse"] = {"target": parsed.get("target"),
                         "parent": parsed.get("parent")}

    # -- Stage (b): risk via injected risk_fn. Fail closed: no
    # -- classifier (or an unclassified verdict) must never execute.
    if risk_fn is None:
        risk = {"level": "unclassified",
                "note": "no risk_fn injected; refusing to execute"}
        evidence["risk"] = risk
        _fail("risk", "no risk classifier wired in: unclassified "
              "candidates must not execute")
        _skip_rest(list(STAGE_ORDER),
                   "short-circuited: no risk classification")
        return _finish(False, "risk", failures[-1]["reason"])
    else:
        try:
            risk = risk_fn(parsed)
        except Exception as exc:  # noqa: BLE001 - fail closed, record it
            risk = {"level": "unclassified",
                    "note": "risk_fn raised %s: %s"
                    % (type(exc).__name__, exc)}
            _fail("risk", "risk classification failed: %s" % (exc,))
            _skip_rest(list(STAGE_ORDER),
                       "short-circuited after risk failure")
            return _finish(False, "risk", failures[-1]["reason"])
        if not isinstance(risk, dict):
            risk = {"level": "unclassified",
                    "note": "risk_fn returned %s, expected dict"
                    % type(risk).__name__}
            _fail("risk", "risk classification malformed: %s"
                  % (risk["note"],))
            _skip_rest(list(STAGE_ORDER),
                       "short-circuited after risk failure")
            return _finish(False, "risk", failures[-1]["reason"])
    evidence["risk"] = risk
    if risk.get("level") == "unclassified":
        _fail("risk", "risk classification unclassified: refusing "
              "to execute without a risk level")
        _skip_rest(list(STAGE_ORDER),
                   "short-circuited: unclassified risk")
        return _finish(False, "risk", failures[-1]["reason"])
    if _risk_blocks(risk):
        _fail("risk", "risk level %s blocks promotion (plan.md R6: "
              "trusted kernel/evaluator is not self-modifiable)"
              % (risk.get("level"),))
        _skip_rest(list(STAGE_ORDER),
                   "short-circuited: risk classification blocks promotion")
        return _finish(False, "risk", failures[-1]["reason"])
    _record("risk", _OK, "level %s" % (risk.get("level"),))

    # -- Stages (c)+(d): checks, cheap first, early stop on failure.
    if tests is None:
        tests = {}
    if not isinstance(tests, dict):
        _fail("direct", "tests must be a mapping of stage name to spec, "
              "got %s" % type(tests).__name__)
        _skip_rest(list(STAGE_ORDER)[1:],
                   "short-circuited after invalid tests mapping")
        return _finish(False, "direct", failures[-1]["reason"])
    unknown = sorted((k for k in tests if k not in STAGE_ORDER), key=repr)
    if unknown:
        reason = "unknown stage(s): %s; known stages: %s" % (
            ", ".join(repr(k) for k in unknown), ", ".join(STAGE_ORDER))
        evidence["unknown_stages"] = list(unknown)
        failed_name = unknown[0] if isinstance(unknown[0], str) else "tests"
        _fail(failed_name, reason)
        _skip_rest(list(STAGE_ORDER),
                   "short-circuited after unknown stage key(s)")
        return _finish(False, failed_name, reason)
    ran_any = False
    for position, name in enumerate(STAGE_ORDER):
        if name not in tests or tests[name] is None:
            _record(name, _SKIPPED, "no %s cases provided" % name)
            continue
        ran_any = True
        passed, stage_evidence = _CHECKS[name](parsed, tests[name], worker_fn)
        evidence[name] = stage_evidence
        if passed:
            _record(name, _OK)
            continue
        reason = _stage_failure_reason(name, stage_evidence)
        _fail(name, reason)
        _skip_rest(list(STAGE_ORDER)[position + 1:],
                   "short-circuited after %s failure" % name)
        return _finish(False, name, reason)

    if not ran_any:
        _fail("tests", "no verification stages requested: zero checks "
              "executed is not a pass")
        return _finish(False, "tests", failures[-1]["reason"])
    return _finish(True, None, "all requested stages passed")


def _stage_failure_reason(name, stage_evidence):
    if not isinstance(stage_evidence, dict):
        return "%s failed" % name
    if stage_evidence.get("error"):
        return "%s failed: %s" % (name, stage_evidence["error"])
    if name == "differential":
        return "%s failed: %d divergence(s), %d error(s) in %d case(s)" % (
            name, stage_evidence.get("divergences", 0),
            stage_evidence.get("errors", 0), stage_evidence.get("total", 0))
    if name == "performance":
        violations = stage_evidence.get("violations", [])
        errors = stage_evidence.get("errors", [])
        if errors:
            return "%s failed: %s" % (name, errors[0])
        return "%s failed: %d sample(s) exceeded budget %s ms" % (
            name, len(violations), stage_evidence.get("budget_ms"))
    return "%s failed: %d of %d item(s) failed" % (
        name, stage_evidence.get("failed", 0), stage_evidence.get("total", 0))
