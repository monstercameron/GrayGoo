"""Bounded repair mode + minimal failure summaries.

Implements plan.md sections 34-35 (structured conditions/restarts and
aggressively compressed model-facing failure summaries), 53-55 (mutation
budgets, storm protection, oscillation detection), 68-69 (task execution
and candidate rehearsal algorithms), and the "Add repair mode" items from
todos.md:

- Return a minimal failing counterexample to the model.
- Allow one repair attempt by default (``max_repairs=1``).
- Rehearse every repair from scratch (a fresh ``pipeline_fn`` verdict).
- Escalate only after bounded repair failure.
- Detect repair oscillation (same failure signature twice -> freeze and
  escalate, no further repairs).

The rehearsal pipeline and the model client are INJECTED callables — this
module never imports ``pipeline.py`` or ``cerebras_client.py``:

- ``pipeline_fn(candidate_text, tests)`` rehearses a candidate from
  scratch and returns a verdict mapping. Green is any verdict with
  ``"passed"`` truthy, ``"verdict" == "pass"``, or a pass-like
  ``"status"`` (``pass``/``passed``/``green``/``success``/``ok``).
- ``generate_fn(candidate_text, failure_summary)`` asks the model for a
  repaired candidate given the minimal summary from
  :func:`summarize_failure`.

Stdlib only.
"""

from typing import Any, Callable, Dict, List, Mapping, Tuple

# Pipeline verdicts carry failures as a list of dicts under "failures";
# evaluator verdicts carry per-check results under evidence["cases"].
# Both shapes are tolerated wherever a verdict is consumed.
_FAILURE_KEYS = ("property", "counterexample", "expected", "actual",
                 "callsite")

_PASS_STATUSES = frozenset({"pass", "passed", "green", "success", "ok"})


def is_green(verdict: Mapping[str, Any]) -> bool:
    """True when ``verdict`` reports success (pipeline or evaluator shape)."""
    if not isinstance(verdict, Mapping):
        return False
    if verdict.get("passed"):
        return True
    if verdict.get("verdict") == "pass":
        return True
    status = verdict.get("status")
    return isinstance(status, str) and status.strip().lower() in _PASS_STATUSES


def _first_failure(verdict: Mapping[str, Any]) -> Mapping[str, Any]:
    """Extract the primary failure dict from a pipeline/evaluator verdict."""
    failures = verdict.get("failures")
    if isinstance(failures, (list, tuple)) and failures:
        first = failures[0]
        if isinstance(first, Mapping):
            return first
    evidence = verdict.get("evidence")
    if isinstance(evidence, Mapping):
        cases = evidence.get("cases")
        if isinstance(cases, (list, tuple)):
            for case in cases:
                if not isinstance(case, Mapping):
                    continue
                for check in case.get("checks", []):
                    if (isinstance(check, Mapping)
                            and not check.get("passed", True)):
                        merged = dict(check)
                        merged.setdefault("property",
                                          case.get("id", check.get("key")))
                        merged.setdefault("callsite", case.get("id"))
                        return merged
    return {}


def _pick(failure: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in failure and failure[name] is not None:
            return failure[name]
    return None


def summarize_failure(verdict: Mapping[str, Any]) -> Dict[str, Any]:
    """Compress ``verdict`` to a minimal model-facing counterexample.

    Returns exactly ``{"property", "counterexample", "expected", "actual",
    "callsite"}`` following the plan.md section 35 layout
    (FAILED PROPERTY / COUNTEREXAMPLE / EXPECTED / ACTUAL / CALLSITE).
    Unknown fields are None. Full logs are never included: only these
    five keys are ever picked out of the verdict.
    """
    if not isinstance(verdict, Mapping):
        raise TypeError("verdict must be a mapping")
    failure = _first_failure(verdict)
    return {
        "property": _pick(failure, "property", "test", "name", "key"),
        "counterexample": _pick(failure, "counterexample", "input", "case",
                                "value", "output"),
        "expected": _pick(failure, "expected", "want"),
        "actual": _pick(failure, "actual", "got", "error"),
        "callsite": _pick(failure, "callsite", "location", "id"),
    }


def failure_signature(verdict: Mapping[str, Any]) -> str:
    """Stable signature identifying one failure for oscillation detection.

    Derived from property + expected + actual only, so byte-identical
    repeats — a repair that changed nothing observable — collide while
    genuinely new failures do not.
    """
    summary = summarize_failure(verdict)
    return "%r|%r|%r" % (summary["property"], summary["expected"],
                         summary["actual"])


def detect_oscillation(history: List[Mapping[str, Any]],
                       signature: str) -> bool:
    """True when ``signature`` already appears in the repair history."""
    return any(entry.get("signature") == signature for entry in history)


def _escalation(reason: str, repairs_used: int,
                history: List[Dict[str, Any]],
                failure: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "escalated",
        "reason": reason,
        "repairs_used": repairs_used,
        "history": list(history),
        "failure": dict(failure),
        "frozen": True,
    }


def repair_loop(candidate_text: str,
                tests: Any,
                *,
                pipeline_fn: Callable[[str, Any], Mapping[str, Any]],
                generate_fn: Callable[[str, Dict[str, Any]], str],
                max_repairs: int = 1) -> Dict[str, Any]:
    """Run candidate -> verdict -> green? -> counterexample -> repair.

    Each attempt rehearses from scratch via ``pipeline_fn``. On failure
    the minimal summary from :func:`summarize_failure` is handed to
    ``generate_fn`` for a single repair (bounded by ``max_repairs``),
    then the repaired candidate is re-rehearsed from scratch.

    Returns a success record on green::

        {"status": "success", "candidate": ..., "verdict": ...,
         "repairs_used": int, "history": [...]}

    or an escalation record when the repair budget is exhausted
    (``reason == "repair_budget_exhausted"``) or the same failure
    signature appears twice (``reason == "oscillation"`` — the target is
    frozen and no further repairs are attempted)::

        {"status": "escalated", "reason": ..., "repairs_used": int,
         "history": [...], "failure": {...}, "frozen": True}

    ``history`` entries are ``{"signature", "summary"}`` dicts, oldest
    first. ``tests`` is passed through untouched to ``pipeline_fn``.
    """
    if max_repairs < 0:
        raise ValueError("max_repairs must be >= 0")
    history: List[Dict[str, Any]] = []
    current = candidate_text
    repairs_used = 0
    while True:
        verdict = pipeline_fn(current, tests)
        if is_green(verdict):
            return {
                "status": "success",
                "candidate": current,
                "verdict": dict(verdict),
                "repairs_used": repairs_used,
                "history": list(history),
            }
        signature = failure_signature(verdict)
        summary = summarize_failure(verdict)
        if detect_oscillation(history, signature):
            # Same failure twice: freeze the target and escalate for
            # diagnosis instead of burning more repairs (plan.md section 55).
            return _escalation("oscillation", repairs_used, history, summary)
        history.append({"signature": signature, "summary": summary})
        if repairs_used >= max_repairs:
            return _escalation("repair_budget_exhausted", repairs_used,
                               history, summary)
        current = generate_fn(current, summary)
        repairs_used += 1
