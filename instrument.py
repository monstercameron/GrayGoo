"""Pipeline-to-ledger wiring: thin event-logging helpers.

Implements the todos.md "Build event logging" items on top of the
append-only :class:`events.EventLedger` (plan.md sections 41-42, 76-77).

Every helper takes PLAIN DICTS (plus scalar linkage ids) and writes ONLY
through the ledger's append paths (``append_event`` / ``log_model_call``).
This module imports stdlib plus ``events`` -- never sibling lane modules.

Event-type vocabulary follows plan.md section 41; ``candidate rejected``
extends it for the promotion/rejection item.
"""

import json

import events

PROVIDER_DEFAULT = "cerebras"

FAILURE_EVENT_TYPES = frozenset({"candidate failed", "capability failed"})


def _require_dict(mapping, helper):
    if not isinstance(mapping, dict):
        raise TypeError(
            "%s(): expected a plain dict, got %s"
            % (helper, type(mapping).__name__)
        )


def _require_keys(mapping, keys, helper):
    for key in keys:
        if key not in mapping:
            raise ValueError(
                "%s(): missing required key '%s'" % (helper, key)
            )


def _require_scalar(value, name, helper):
    if not value:
        raise ValueError("%s(): '%s' is required" % (helper, name))


def log_model_call(ledger, result_dict):
    """Log one generate()-style model result.

    Required keys: ``model``. Optional: ``provider`` (default
    ``"cerebras"``), ``latency_ms``, ``input_tokens``,
    ``output_tokens``, ``cost_usd`` (or ``cost``), ``request_id``,
    ``text``/``result``, ``finish_reason``, ``generation``,
    ``candidate_id``, ``task_id``, ``context_hash``, ``prompt_version``.

    Writes one model-call row plus exactly one ``model called`` event
    whose payload carries the request id and the ``call_id`` link.
    Returns the stored event row as a dict.
    """
    helper = "log_model_call"
    _require_dict(result_dict, helper)
    _require_keys(result_dict, ("model",), helper)
    cost = result_dict.get("cost_usd", result_dict.get("cost"))
    call = ledger.log_model_call(
        provider=result_dict.get("provider", PROVIDER_DEFAULT),
        model=result_dict["model"],
        latency_ms=result_dict.get("latency_ms"),
        input_tokens=result_dict.get("input_tokens"),
        output_tokens=result_dict.get("output_tokens"),
        context_hash=result_dict.get("context_hash"),
        prompt_version=result_dict.get("prompt_version"),
        generation=result_dict.get("generation"),
        candidate_id=result_dict.get("candidate_id"),
        result=result_dict.get("result", result_dict.get("text")),
        cost=cost,
        request_timestamp=result_dict.get("request_timestamp"),
        response_timestamp=result_dict.get("response_timestamp"),
    )
    return ledger.append_event(
        "model called",
        generation=result_dict.get("generation"),
        task_id=result_dict.get("task_id"),
        candidate_id=result_dict.get("candidate_id"),
        payload={
            "call_id": call["call_id"],
            "provider": call["provider"],
            "model": call["model"],
            "latency_ms": call["latency_ms"],
            "input_tokens": call["input_tokens"],
            "output_tokens": call["output_tokens"],
            "cost": call["cost"],
            "request_id": result_dict.get("request_id"),
            "finish_reason": result_dict.get("finish_reason"),
        },
    )


def log_candidate(ledger, candidate_dict, task_id):
    """Log one synthesized candidate.

    Required keys: ``candidate_id``. Optional linkage: ``generation``.
    The full dict becomes the payload of exactly one
    ``candidate generated`` event. Returns the stored event row.
    """
    helper = "log_candidate"
    _require_dict(candidate_dict, helper)
    _require_keys(candidate_dict, ("candidate_id",), helper)
    _require_scalar(task_id, "task_id", helper)
    return ledger.append_event(
        "candidate generated",
        generation=candidate_dict.get("generation"),
        task_id=task_id,
        candidate_id=candidate_dict["candidate_id"],
        payload=dict(candidate_dict),
    )


def log_compile_test(ledger, verdict_dict, candidate_id):
    """Log one compile/test verdict for a candidate.

    Required keys: ``passed`` (truthy = pass). Optional linkage:
    ``task_id``, ``generation``. Emits exactly one ``candidate passed``
    or ``candidate failed`` event carrying the full verdict as payload.
    Returns the stored event row.
    """
    helper = "log_compile_test"
    _require_dict(verdict_dict, helper)
    _require_keys(verdict_dict, ("passed",), helper)
    _require_scalar(candidate_id, "candidate_id", helper)
    event_type = "candidate passed" if verdict_dict["passed"] \
        else "candidate failed"
    return ledger.append_event(
        event_type,
        generation=verdict_dict.get("generation"),
        task_id=verdict_dict.get("task_id"),
        candidate_id=candidate_id,
        payload=dict(verdict_dict),
    )


def log_promotion(ledger, decision_dict):
    """Log one promotion/rejection decision.

    Required keys: ``candidate_id``, ``approved``. Optional linkage:
    ``capability_id``, ``capability_version``, ``task_id``,
    ``generation``. Emits exactly one ``candidate promoted`` or
    ``candidate rejected`` event. Returns the stored event row.
    """
    helper = "log_promotion"
    _require_dict(decision_dict, helper)
    _require_keys(decision_dict, ("candidate_id", "approved"), helper)
    event_type = "candidate promoted" if decision_dict["approved"] \
        else "candidate rejected"
    return ledger.append_event(
        event_type,
        generation=decision_dict.get("generation"),
        task_id=decision_dict.get("task_id"),
        candidate_id=decision_dict["candidate_id"],
        capability_id=decision_dict.get("capability_id"),
        capability_version=decision_dict.get("capability_version"),
        payload=dict(decision_dict),
    )


def log_invocation(ledger, capability_id, version, ok, latency_ms):
    """Log one capability invocation outcome.

    Emits exactly one ``capability invoked`` (``ok`` truthy) or
    ``capability failed`` event. Returns the stored event row.
    """
    helper = "log_invocation"
    _require_scalar(capability_id, "capability_id", helper)
    event_type = "capability invoked" if ok else "capability failed"
    return ledger.append_event(
        event_type,
        capability_id=capability_id,
        capability_version=version,
        payload={"ok": bool(ok), "latency_ms": latency_ms},
    )


def log_rollback(ledger, capability_id, from_v, to_v, reason):
    """Log one version rollback.

    Emits exactly one ``candidate rolled back`` event; the
    ``capability_version`` column records the restored version.
    Returns the stored event row.
    """
    helper = "log_rollback"
    _require_scalar(capability_id, "capability_id", helper)
    return ledger.append_event(
        "candidate rolled back",
        capability_id=capability_id,
        capability_version=to_v,
        payload={
            "from_version": from_v,
            "to_version": to_v,
            "reason": reason,
        },
    )


def log_task_outcome(ledger, task_id, success, summary):
    """Log one task outcome.

    Emits exactly one ``task completed`` event. Returns the stored
    event row.
    """
    helper = "log_task_outcome"
    _require_scalar(task_id, "task_id", helper)
    return ledger.append_event(
        "task completed",
        task_id=task_id,
        payload={"success": bool(success), "summary": summary},
    )


def _payload_success(payload_text):
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError):
        return None
    if isinstance(payload, dict) and "success" in payload:
        return payload["success"]
    return None


def recent_failures(ledger, task_id, limit=10):
    """World-model projection (plan.md section 42): recent failures.

    Read-only: returns failure events for *task_id* (``candidate
    failed``, ``capability failed``, and ``task completed`` with a
    falsy ``success``), most recent first, up to *limit* rows.
    """
    matches = []
    for event in ledger.get_events_by_task(task_id):
        if event["event_type"] in FAILURE_EVENT_TYPES:
            matches.append(event)
        elif event["event_type"] == "task completed":
            success = _payload_success(event["payload"])
            if success is not None and not success:
                matches.append(event)
    matches.sort(key=lambda e: e["event_id"], reverse=True)
    return matches[:limit]
