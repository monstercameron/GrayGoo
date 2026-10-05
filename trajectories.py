"""Mutation trajectory capture (learning L1).

Implements memory.md sections 9-10 and 35 (phase L1), plus the
todos.md "Capture mutation trajectories (learning L1)" items, on top of
the append-only :class:`events.EventLedger`.

A trajectory is the full development path for one task::

    goal -> candidate 1 -> failure 1 -> repair 1 -> ... -> success

Stored as one ``trajectory step`` event per step plus a single
``trajectory recorded`` summary event. Repair outcomes
(``repair outcome``) and development-strategy outcomes
(``strategy outcome``) are separate event types so the L3 miner can
cluster them independently.

This module imports stdlib plus ``events`` -- never sibling lane modules.
"""

import json

FAILURE_CLASSES = (
    "syntax",
    "compile",
    "type/contract",
    "wrong-output",
    "edge-case",
    "state-corruption",
    "effect-violation",
    "performance",
    "timeout",
    "memory",
    "stale-generation",
    "over-refactor",
    "negative-transfer",
    "test-overfit",
    "tool-misuse",
    "context-missing",
)
"""Normalized failure taxonomy (memory.md section 9): 16 classes."""

FAILURE_CLASS_SET = frozenset(FAILURE_CLASSES)

#: Explicit aliases accepted by :func:`normalize_failure_class`.
FAILURE_CLASS_ALIASES = {
    "contract": "type/contract",
    "type": "type/contract",
    "type error": "type/contract",
    "wrong output": "wrong-output",
    "edge case": "edge-case",
    "state corruption": "state-corruption",
    "effect violation": "effect-violation",
    "stale generation": "stale-generation",
    "over refactor": "over-refactor",
    "negative transfer": "negative-transfer",
    "test overfit": "test-overfit",
    "tool misuse": "tool-misuse",
    "context missing": "context-missing",
}

TRAJECTORY_STEP_EVENT = "trajectory step"
TRAJECTORY_RECORDED_EVENT = "trajectory recorded"
REPAIR_OUTCOME_EVENT = "repair outcome"
STRATEGY_OUTCOME_EVENT = "strategy outcome"


def _canonicalize(name):
    return (name.strip().lower()
            .replace("_", "-").replace(" ", "-").replace("/", "-"))


_CANONICAL_LOOKUP = {_canonicalize(c): c for c in FAILURE_CLASSES}


def normalize_failure_class(name):
    """Map *name* to its canonical failure class (memory.md section 9).

    Accepts the 16 canonical names plus common aliases
    (``contract`` -> ``type/contract``) and ``-``/``_``/`` ``/``/``
    spelling variants. Raises :exc:`ValueError` on unknown classes and
    :exc:`TypeError` on non-string input.
    """
    if not isinstance(name, str):
        raise TypeError("failure class must be a string, got %s"
                        % type(name).__name__)
    key = name.strip().lower()
    if key in FAILURE_CLASS_SET:
        return key
    if key in FAILURE_CLASS_ALIASES:
        return FAILURE_CLASS_ALIASES[key]
    canonical = _CANONICAL_LOOKUP.get(_canonicalize(name))
    if canonical is not None:
        return canonical
    raise ValueError("unknown failure class %r (expected one of: %s)"
                     % (name, ", ".join(FAILURE_CLASSES)))


def _normalize_failure(failure, index):
    if failure is None:
        return None
    if isinstance(failure, str):
        return {"class": normalize_failure_class(failure)}
    if not isinstance(failure, dict):
        raise TypeError("step %d: 'failure' must be None, a class name, "
                        "or a dict, got %s" % (index, type(failure).__name__))
    if "class" not in failure:
        raise ValueError("step %d: failure dict requires a 'class' key"
                         % index)
    normalized = dict(failure)
    normalized["class"] = normalize_failure_class(failure["class"])
    return normalized


def _normalize_step(step, index):
    if not isinstance(step, dict):
        raise TypeError("step %d must be a dict with candidate/failure/"
                        "repair/success keys, got %s"
                        % (index, type(step).__name__))
    candidate = step.get("candidate")
    if candidate is not None and not isinstance(candidate, (str, dict)):
        raise TypeError("step %d: 'candidate' must be None, a string id, "
                        "or a dict" % index)
    repair = step.get("repair")
    if repair is not None and not isinstance(repair, (str, dict)):
        raise TypeError("step %d: 'repair' must be None, a string, "
                        "or a dict" % index)
    return {
        "candidate": candidate,
        "failure": _normalize_failure(step.get("failure"), index),
        "repair": repair,
        "success": bool(step.get("success", False)),
    }


def record_trajectory(ledger, task_id, steps, family=None, generation=None,
                      goal=None):
    """Store one complete mutation trajectory on *ledger*.

    *steps* is a list of dicts with keys ``candidate`` (id or dict),
    ``failure`` (None, a failure-class name, or a dict with a ``class``
    key), ``repair`` (None, a string, or a dict), and ``success``
    (bool). Failure classes are validated against
    :data:`FAILURE_CLASSES`.

    Writes one ``trajectory step`` event per step plus one
    ``trajectory recorded`` summary event. Returns a summary dict with
    ``task_id``, ``family``, normalized ``steps``, and ``event_ids``.
    """
    if not task_id:
        raise ValueError("record_trajectory(): 'task_id' is required")
    if not isinstance(steps, list) or not steps:
        raise ValueError("record_trajectory(): 'steps' must be a "
                         "non-empty list of step dicts")
    normalized = [_normalize_step(step, i) for i, step in enumerate(steps)]
    event_ids = []
    for index, step in enumerate(normalized):
        stored = ledger.append_event(
            TRAJECTORY_STEP_EVENT,
            generation=generation,
            task_id=task_id,
            payload={
                "family": family,
                "goal": goal,
                "step_index": index,
                "step_count": len(normalized),
                "candidate": step["candidate"],
                "failure": step["failure"],
                "repair": step["repair"],
                "success": step["success"],
            },
        )
        event_ids.append(stored["event_id"])
    failures = [s["failure"]["class"] for s in normalized
                if s["failure"] is not None]
    summary = ledger.append_event(
        TRAJECTORY_RECORDED_EVENT,
        generation=generation,
        task_id=task_id,
        payload={
            "family": family,
            "goal": goal,
            "step_count": len(normalized),
            "failure_classes": failures,
            "success": normalized[-1]["success"],
        },
    )
    event_ids.append(summary["event_id"])
    return {
        "task_id": task_id,
        "family": family,
        "goal": goal,
        "steps": normalized,
        "event_ids": event_ids,
    }


def _payload_dict(event):
    try:
        payload = json.loads(event.get("payload", "{}"))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def query_trajectories(ledger, task_id=None, family=None):
    """Return recorded trajectories, optionally filtered.

    Each trajectory is a dict with ``task_id``, ``family``, ``goal``,
    ordered ``steps`` (each with candidate/failure/repair/success),
    and ``event_ids``. Results are sorted by ``task_id``.
    """
    grouped = {}
    for event in ledger.get_events_by_type(TRAJECTORY_STEP_EVENT):
        payload = _payload_dict(event)
        tid = event.get("task_id")
        entry = grouped.setdefault(tid, {
            "task_id": tid,
            "family": payload.get("family"),
            "goal": payload.get("goal"),
            "steps": [],
            "event_ids": [],
        })
        entry["steps"].append((
            payload.get("step_index", 0),
            {
                "candidate": payload.get("candidate"),
                "failure": payload.get("failure"),
                "repair": payload.get("repair"),
                "success": bool(payload.get("success", False)),
            },
            event["event_id"],
        ))
    trajectories = []
    for tid in sorted(grouped):
        entry = grouped[tid]
        if task_id is not None and tid != task_id:
            continue
        if family is not None and entry["family"] != family:
            continue
        ordered = sorted(entry["steps"], key=lambda item: item[0])
        trajectories.append({
            "task_id": tid,
            "family": entry["family"],
            "goal": entry["goal"],
            "steps": [item[1] for item in ordered],
            "event_ids": [item[2] for item in ordered],
        })
    return trajectories


def record_repair_outcome(ledger, task_id, candidate_id, failure_class,
                          repair_kind, success, detail=None,
                          generation=None):
    """Store one repair outcome (todos.md L1: repair outcomes).

    *failure_class* is validated against :data:`FAILURE_CLASSES`;
    *repair_kind* names the repair approach (e.g. ``model-repair``,
    ``deterministic-fix``, ``escalated``). Returns the stored event row.
    """
    if not task_id:
        raise ValueError("record_repair_outcome(): 'task_id' is required")
    if not candidate_id:
        raise ValueError("record_repair_outcome(): 'candidate_id' is "
                         "required")
    if not repair_kind:
        raise ValueError("record_repair_outcome(): 'repair_kind' is "
                         "required")
    return ledger.append_event(
        REPAIR_OUTCOME_EVENT,
        generation=generation,
        task_id=task_id,
        candidate_id=candidate_id,
        payload={
            "failure_class": normalize_failure_class(failure_class),
            "repair_kind": repair_kind,
            "success": bool(success),
            "detail": detail,
        },
    )


def record_strategy_outcome(ledger, task_id, strategy, success, detail=None,
                            generation=None):
    """Store one development-strategy outcome (todos.md L1: strategies).

    *strategy* names the approach tried (e.g. ``compose-first``,
    ``synthesize-monolith``, ``differential-first``). Returns the
    stored event row.
    """
    if not task_id:
        raise ValueError("record_strategy_outcome(): 'task_id' is required")
    if not strategy or not isinstance(strategy, str):
        raise ValueError("record_strategy_outcome(): 'strategy' must be a "
                         "non-empty string")
    return ledger.append_event(
        STRATEGY_OUTCOME_EVENT,
        generation=generation,
        task_id=task_id,
        payload={
            "strategy": strategy,
            "success": bool(success),
            "detail": detail,
        },
    )


def list_repair_outcomes(ledger, task_id=None):
    """Return recorded repair outcomes as dicts (optionally per task)."""
    outcomes = []
    for event in ledger.get_events_by_type(REPAIR_OUTCOME_EVENT):
        if task_id is not None and event.get("task_id") != task_id:
            continue
        payload = _payload_dict(event)
        outcomes.append({
            "event_id": event["event_id"],
            "task_id": event.get("task_id"),
            "candidate_id": event.get("candidate_id"),
            "failure_class": payload.get("failure_class"),
            "repair_kind": payload.get("repair_kind"),
            "success": payload.get("success"),
            "detail": payload.get("detail"),
        })
    return outcomes


def list_strategy_outcomes(ledger, task_id=None):
    """Return recorded strategy outcomes as dicts (optionally per task)."""
    outcomes = []
    for event in ledger.get_events_by_type(STRATEGY_OUTCOME_EVENT):
        if task_id is not None and event.get("task_id") != task_id:
            continue
        payload = _payload_dict(event)
        outcomes.append({
            "event_id": event["event_id"],
            "task_id": event.get("task_id"),
            "strategy": payload.get("strategy"),
            "success": payload.get("success"),
            "detail": payload.get("detail"),
        })
    return outcomes
