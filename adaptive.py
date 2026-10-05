"""Adaptive development loop (learning L6).

Closes the adaptive-development loop from memory.md sections 24-27 and 35
(phase L6) and the todos.md "Close the adaptive-development loop" items:
learn which context elements correlate with success, which rehearsal
checks to run first, how to route each failure class to a repair
strategy, and how far bounded repair escalation should go.

Learners read the append-only :class:`events.EventLedger` (trajectory,
repair, and strategy outcomes via :mod:`trajectories`) or plain
in-memory histories. Every learner degrades gracefully to a documented
default on empty or sparse evidence: it never raises on missing data and
never reports extreme estimates from a handful of observations.

Documented defaults (returned exactly on empty input):

* context weights: ``0.0`` for every element of
  :data:`CONTEXT_ELEMENTS` (no evidence means no preference).
* test order: :data:`DEFAULT_TEST_ORDER` (cheap checks first).
* repair route: :data:`DEFAULT_REPAIR_ROUTES` per failure class, and
  ``"escalate"`` for unknown classes.
* escalation policy: :data:`DEFAULT_ESCALATION_POLICY`
  (``max_repairs == 1``; oscillation always escalates).
* risk model: all-zero ``predicted_count``/``actual_incidents`` notes.
* applicability: zero tallies per declared boundary tag.
* postmortem: ``[]`` for non-rollback or unreadable records.

Stdlib plus :mod:`trajectories` only -- never sibling lane modules.
"""

import json

import trajectories

__all__ = [
    "CONTEXT_ELEMENTS",
    "CONTEXT_DEFAULT_WEIGHT",
    "DEFAULT_TEST_ORDER",
    "REPAIR_ROUTES",
    "DEFAULT_REPAIR_ROUTES",
    "DEFAULT_ESCALATION_POLICY",
    "MAX_REPAIRS",
    "learn_context_weights",
    "context_weight_details",
    "learn_test_order",
    "test_check_scores",
    "route_repair",
    "learn_escalation_policy",
    "learn_risk_model",
    "learn_applicability",
    "postmortem_trigger",
]

#: Context elements whose presence is correlated with task success
#: (memory.md section 24: contract, nearest failure, callers, lessons).
CONTEXT_ELEMENTS = ("contract", "failure", "callers", "lessons")

#: Default weight: no evidence means no preference, exactly 0.0.
CONTEXT_DEFAULT_WEIGHT = 0.0

#: Shrinkage strength for context weights. The observed lift is scaled by
#: ``contrast / (contrast + CONTEXT_PRIOR_STRENGTH)`` where ``contrast``
#: is ``min(n_with, n_without)``, so sparse or one-sided evidence stays
#: near neutral instead of reporting extreme weights.
CONTEXT_PRIOR_STRENGTH = 10

_CONTEXT_ALIASES = {
    "contract": "contract",
    "contracts": "contract",
    "failure": "failure",
    "failures": "failure",
    "current-failure": "failure",
    "nearest-failure": "failure",
    "recent-failure": "failure",
    "caller": "callers",
    "callers": "callers",
    "lesson": "lessons",
    "lessons": "lessons",
}

#: Payload/dict keys that carry a context-marker block (a mapping of
#: element -> present flag, or a list of present elements).
_CONTEXT_KEYS = ("context", "context_elements", "elements", "markers")


#: Default rehearsal order: cheap checks first. Mirrors
#: ``pipeline.STAGE_ORDER``; re-declared here because this module must not
#: import sibling lanes.
DEFAULT_TEST_ORDER = ("direct", "regression", "property",
                      "differential", "performance")

#: Assumed cost for a check run with no recorded elapsed time.
DEFAULT_CHECK_MS = 100.0


#: Repair routes (memory.md section 25).
REPAIR_ROUTES = ("deterministic-fix", "model-repair", "escalate")

#: Safe per-class default route used when recorded repair evidence is
#: missing or sparse. Syntax/stale-generation/over-refactor/
#: context-missing failures have mechanical fixes; state, effect, and
#: resource failures escalate to deeper reasoning instead of burning
#: model calls; everything else retries through the model with a minimal
#: counterexample.
DEFAULT_REPAIR_ROUTES = {
    "syntax": "deterministic-fix",
    "compile": "model-repair",
    "type/contract": "model-repair",
    "wrong-output": "model-repair",
    "edge-case": "model-repair",
    "state-corruption": "escalate",
    "effect-violation": "escalate",
    "performance": "model-repair",
    "timeout": "escalate",
    "memory": "escalate",
    "stale-generation": "deterministic-fix",
    "over-refactor": "deterministic-fix",
    "negative-transfer": "model-repair",
    "test-overfit": "escalate",
    "tool-misuse": "model-repair",
    "context-missing": "deterministic-fix",
}

#: Minimum recorded outcomes for a failure class before data may override
#: the default route, and minimum direct observations of the winning
#: route. Below either floor the default stands.
MIN_ROUTE_EVIDENCE = 5
MIN_ROUTE_DIRECT = 2

_ROUTE_ALIASES = {
    "deterministic-fix": "deterministic-fix",
    "deterministic": "deterministic-fix",
    "template": "deterministic-fix",
    "rule-based": "deterministic-fix",
    "rulebased": "deterministic-fix",
    "static-fix": "deterministic-fix",
    "auto-fix": "deterministic-fix",
    "autofix": "deterministic-fix",
    "model-repair": "model-repair",
    "model": "model-repair",
    "qwen": "model-repair",
    "llm": "model-repair",
    "llm-repair": "model-repair",
    "neural": "model-repair",
    "repair": "model-repair",
    "escalate": "escalate",
    "escalated": "escalate",
    "escalation": "escalate",
    "human": "escalate",
    "manual": "escalate",
}


#: Default bounded-escalation policy: one repair attempt, then escalate;
#: oscillation always escalates (matches ``repair.repair_loop``).
DEFAULT_ESCALATION_POLICY = {"max_repairs": 1,
                             "escalate_on_oscillation": True}

#: Hard ceiling: a learned repair budget never exceeds this.
MAX_REPAIRS = 3
#: A further repair is only budgeted when its smoothed eventual-success
#: rate reaches this floor.
MIN_MARGINAL_GAIN = 0.10
#: Conservative prior failures added to each repair-depth cohort so
#: sparse evidence cannot grow the budget.
SEQUENCE_PRIOR = 4
#: Minimum histories reaching a repair depth before it may grow the
#: budget.
MIN_SEQUENCE_EVIDENCE = 5


_SUCCESS_WORDS = frozenset({"success", "pass", "passed", "green", "ok",
                            "fixed", "resolved"})
_FAIL_WORDS = frozenset({"fail", "failed", "failure", "error", "escalate",
                         "escalated", "timeout", "timed-out", "rejected",
                         "red"})
_FALSE_WORDS = frozenset({"", "0", "false", "no", "off", "absent",
                          "missing", "none"})


def _is_ledger(source):
    """True when *source* looks like an event ledger."""
    return (hasattr(source, "get_events_by_type")
            and callable(source.get_events_by_type))


def _outcome_of(record):
    """True/False/None outcome signal from a history record dict.

    Returns None when the record carries no outcome evidence, so callers
    can skip it instead of inventing an outcome.
    """
    if not isinstance(record, dict):
        return None
    for key in ("success", "passed", "green", "ok"):
        if key in record and record[key] is not None:
            return bool(record[key])
    for key in ("status", "result"):
        value = record.get(key)
        if isinstance(value, str):
            word = value.strip().lower().replace("_", "-")
            word = word.replace(" ", "-")
            if word in _SUCCESS_WORDS:
                return True
            if word in _FAIL_WORDS:
                return False
    return None


def _route_of_kind(kind):
    """Map a repair-kind/strategy label to a route, or None."""
    if not isinstance(kind, str):
        return None
    key = kind.strip().lower().replace("_", "-").replace(" ", "-")
    if key in _ROUTE_ALIASES:
        return _ROUTE_ALIASES[key]
    if key in REPAIR_ROUTES:
        return key
    return None


def _scan_payloads(ledger):
    """Return ``(task_id, payload_dict)`` pairs for every ledger event.

    Uses the store connection when available, else probes known
    event-type names. Never raises: unreadable stores yield [].
    """
    found = []
    conn = getattr(ledger, "conn", None)
    if conn is not None and hasattr(conn, "execute"):
        try:
            rows = conn.execute(
                "SELECT task_id, payload FROM events ORDER BY event_id"
            ).fetchall()
        except Exception:
            return []
        for task_id, payload_text in rows:
            try:
                payload = json.loads(payload_text or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                found.append((task_id, payload))
        return found
    for event_type in ("test outcome", "check outcome", "test result",
                       "check result", "check completed", "test completed",
                       "pipeline verdict", "rehearsal outcome",
                       "context used", "task context"):
        try:
            rows = ledger.get_events_by_type(event_type)
        except Exception:
            continue
        for event in rows:
            if not isinstance(event, dict):
                continue
            try:
                payload = json.loads(event.get("payload", "{}"))
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                found.append((event.get("task_id"), payload))
    return found


# -- context composition (memory.md section 24) -----------------------


def _normalize_element(name):
    """Map a marker label to a canonical element, or None."""
    if not isinstance(name, str):
        return None
    key = name.strip().lower().replace("_", "-").replace(" ", "-")
    while "--" in key:
        key = key.replace("--", "-")
    return _CONTEXT_ALIASES.get(key)


def _marker_present(value):
    """True when a marker value signals the element was included."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_WORDS
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return len(value) > 0
    return bool(value)


def _markers_from_value(value):
    """Extract present elements from a marker block (mapping/list/one)."""
    markers = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if (_normalize_element(key) is not None
                    and _marker_present(item)):
                markers.add(_normalize_element(key))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            if isinstance(item, str):
                element = _normalize_element(item)
                if element is not None:
                    markers.add(element)
            elif isinstance(item, dict):
                markers |= _markers_from_value(item)
    elif isinstance(value, str):
        element = _normalize_element(value)
        if element is not None:
            markers.add(element)
    return markers


def _markers_from_dict(data, _depth=0):
    """Extract present elements from a candidate/repair/payload dict."""
    markers = set()
    if not isinstance(data, dict):
        return markers
    for key in _CONTEXT_KEYS:
        if key in data:
            markers |= _markers_from_value(data[key])
    for key, value in data.items():
        if (_normalize_element(key) is not None
                and _marker_present(value)):
            markers.add(_normalize_element(key))
    if _depth < 3:
        for key in ("candidate", "repair"):
            nested = data.get(key)
            if isinstance(nested, dict):
                markers |= _markers_from_dict(nested, _depth + 1)
    return markers


def _trajectory_outcome(traj):
    """Final-step success of a trajectory dict, or None if unknown."""
    if not isinstance(traj, dict):
        return None
    steps = traj.get("steps")
    if steps:
        last = steps[-1]
        if isinstance(last, dict):
            return bool(last.get("success", False))
        if isinstance(last, bool):
            return last
        return False
    if "success" in traj:
        return bool(traj.get("success"))
    return None


def _context_item(item):
    """One in-memory record -> (markers, success) or None."""
    if isinstance(item, dict):
        markers = _markers_from_dict(item)
        if item.get("steps"):
            for step in item["steps"]:
                if isinstance(step, dict):
                    markers |= _markers_from_dict(step.get("candidate"))
                    markers |= _markers_from_dict(step.get("repair"))
            return (frozenset(markers), _trajectory_outcome(item))
        outcome = _outcome_of(item)
        if outcome is None:
            return None
        return (frozenset(markers), outcome)
    if isinstance(item, (list, tuple)) and len(item) == 2:
        markers, success = item
        if isinstance(success, dict):
            outcome = _outcome_of(success)
            if outcome is None:
                return None
        elif isinstance(success, bool):
            outcome = success
        else:
            return None
        return (frozenset(_markers_from_value(markers)), outcome)
    return None


def _ledger_context_records(ledger):
    """``(markers, success)`` per task from trajectories + payloads."""
    markers_by_task = {}
    success_by_task = {}
    try:
        trajs = trajectories.query_trajectories(ledger)
    except Exception:
        trajs = []
    for traj in trajs:
        if not isinstance(traj, dict):
            continue
        task_id = traj.get("task_id")
        if not traj.get("steps"):
            continue
        markers = set()
        for step in traj["steps"]:
            if isinstance(step, dict):
                markers |= _markers_from_dict(step.get("candidate"))
                markers |= _markers_from_dict(step.get("repair"))
        markers_by_task[task_id] = markers
        success_by_task[task_id] = _trajectory_outcome(traj)
    payload_outcome = {}
    for task_id, payload in _scan_payloads(ledger):
        markers = _markers_from_dict(payload)
        if markers:
            markers_by_task.setdefault(task_id, set()).update(markers)
        outcome = _outcome_of(payload)
        if outcome is not None:
            payload_outcome[task_id] = outcome
    records = []
    for task_id, markers in markers_by_task.items():
        if task_id in success_by_task:
            records.append((frozenset(markers),
                            success_by_task[task_id]))
        elif task_id in payload_outcome:
            records.append((frozenset(markers),
                            payload_outcome[task_id]))
        # Tasks with markers but no outcome signal are skipped: an
        # unknown outcome must not invent confidence either way.
    return records


def _as_context_records(source):
    if source is None:
        return []
    if _is_ledger(source):
        return _ledger_context_records(source)
    if isinstance(source, dict):
        item = _context_item(source)
        return [item] if item is not None else []
    if isinstance(source, (list, tuple)):
        records = []
        for entry in source:
            item = _context_item(entry)
            if item is not None:
                records.append(item)
        return records
    return []


def _context_stats(source):
    stats = {element: {"n_with": 0, "wins_with": 0,
                       "n_without": 0, "wins_without": 0}
             for element in CONTEXT_ELEMENTS}
    for markers, success in _as_context_records(source):
        for element in CONTEXT_ELEMENTS:
            if element in markers:
                stats[element]["n_with"] += 1
                stats[element]["wins_with"] += 1 if success else 0
            else:
                stats[element]["n_without"] += 1
                stats[element]["wins_without"] += 1 if success else 0
    return stats


def learn_context_weights(ledger):
    """Correlate context-element presence with trajectory success.

    *ledger* is an event ledger (or an in-memory list of trajectory /
    ``(markers, success)`` records, or None). Returns a weight table
    ``{element: float}`` over :data:`CONTEXT_ELEMENTS`: positive means
    presence correlates with success. Weights are Laplace-smoothed
    lifts shrunk toward 0 on sparse or one-sided evidence, so an empty
    ledger yields exactly ``0.0`` everywhere and thin data stays near
    neutral instead of extreme.
    """
    stats = _context_stats(ledger)
    weights = {}
    for element in CONTEXT_ELEMENTS:
        entry = stats[element]
        rate_with = ((entry["wins_with"] + 1.0)
                     / (entry["n_with"] + 2.0))
        rate_without = ((entry["wins_without"] + 1.0)
                        / (entry["n_without"] + 2.0))
        lift = rate_with - rate_without
        contrast = min(entry["n_with"], entry["n_without"])
        shrink = contrast / (contrast + CONTEXT_PRIOR_STRENGTH)
        weights[element] = lift * shrink
    return weights


def context_weight_details(ledger):
    """Full per-element stats behind :func:`learn_context_weights`."""
    stats = _context_stats(ledger)
    weights = learn_context_weights(ledger)
    details = {}
    for element in CONTEXT_ELEMENTS:
        entry = stats[element]
        rate_with = ((entry["wins_with"] + 1.0)
                     / (entry["n_with"] + 2.0))
        rate_without = ((entry["wins_without"] + 1.0)
                        / (entry["n_without"] + 2.0))
        details[element] = {
            "weight": weights[element],
            "lift": rate_with - rate_without,
            "n_present": entry["n_with"],
            "n_absent": entry["n_without"],
            "wins_present": entry["wins_with"],
            "wins_absent": entry["wins_without"],
        }
    return details


# -- test ordering (memory.md section 26) ----------------------------


_CHECK_NAME_KEYS = ("check", "test", "stage", "name")
_ELAPSED_KEYS = ("elapsed_ms", "duration_ms", "cost_ms", "latency_ms",
                 "ms", "elapsed", "duration")
_CATCH_KEYS = ("caught", "failed", "detected", "found", "unique",
               "signal", "first_failure", "caught_issue", "found_bug",
               "detected_issue", "true_positive", "caught_regression")
_FAIL_STATUSES = frozenset({"fail", "failed", "failure", "red", "error"})


def _record_to_check(record, strict=False):
    """One outcome record -> (name, elapsed_ms, caught) or None.

    Yield counts when the record says the check caught a real issue
    (``caught``/``failed``/``detected``/..., or a failed run), unless
    ``false_positive`` is set. With ``strict`` (ledger scans) the record
    must carry at least one cost/outcome signal so unrelated payloads
    that merely mention a name are not mistaken for check runs.
    """
    if not isinstance(record, dict):
        return None
    name = None
    for key in _CHECK_NAME_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    if name is None:
        return None
    if strict and not any(key in record for key in
                          _ELAPSED_KEYS + _CATCH_KEYS
                          + ("passed", "ok", "status", "result",
                             "false_positive")):
        return None
    elapsed = DEFAULT_CHECK_MS
    for key in _ELAPSED_KEYS:
        value = record.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > 0:
            elapsed = float(value)
            break
    caught = any(bool(record.get(key)) for key in _CATCH_KEYS)
    if record.get("false_positive"):
        caught = False
    elif not caught:
        if record.get("passed") is False or record.get("ok") is False:
            caught = True
        else:
            status = record.get("status")
            if (isinstance(status, str)
                    and status.strip().lower() in _FAIL_STATUSES):
                caught = True
    return (name, elapsed, caught)


def _as_test_records(test_history):
    if test_history is None:
        return []
    if _is_ledger(test_history):
        return [parsed for _, payload in _scan_payloads(test_history)
                for parsed in [_record_to_check(payload, strict=True)]
                if parsed is not None]
    if isinstance(test_history, dict):
        parsed = _record_to_check(test_history)
        return [parsed] if parsed is not None else []
    if isinstance(test_history, (list, tuple)):
        records = []
        for entry in test_history:
            parsed = _record_to_check(entry)
            if parsed is not None:
                records.append(parsed)
        return records
    return []


def _test_aggregates(test_history):
    runs = {}
    catches = {}
    total_ms = {}
    for name, elapsed, caught in _as_test_records(test_history):
        runs[name] = runs.get(name, 0) + 1
        catches[name] = catches.get(name, 0) + (1 if caught else 0)
        total_ms[name] = total_ms.get(name, 0.0) + elapsed
    if runs:
        prior_ms = sum(total_ms.values()) / sum(runs.values())
    else:
        prior_ms = DEFAULT_CHECK_MS
    return runs, catches, total_ms, prior_ms


def test_check_scores(test_history):
    """Per-check yield-per-ms stats behind :func:`learn_test_order`."""
    runs, catches, total_ms, prior_ms = _test_aggregates(test_history)
    scores = {}
    for name in runs:
        rate = (catches[name] + 1.0) / (runs[name] + 2.0)
        avg_ms = total_ms[name] / runs[name]
        scores[name] = {
            "score": rate / max(avg_ms, 1e-6),
            "runs": runs[name],
            "catches": catches[name],
            "avg_ms": avg_ms,
        }
    for name in DEFAULT_TEST_ORDER:
        if name not in scores:
            scores[name] = {
                "score": 0.5 / max(prior_ms, 1e-6),
                "runs": 0,
                "catches": 0,
                "avg_ms": prior_ms,
            }
    return scores


def learn_test_order(test_history):
    """Rank rehearsal checks by smoothed yield-per-millisecond.

    *test_history* is a list of outcome records (each naming a check
    plus elapsed time and whether it caught an issue), an event ledger
    holding such outcomes, or None. Returns check names ordered
    fastest/highest-yield first: every known check (Laplace-smoothed
    catch rate over mean cost), then unobserved default checks in
    :data:`DEFAULT_TEST_ORDER` relative order. Empty input returns the
    default order exactly; sparse data stays near it.
    """
    scores = test_check_scores(test_history)
    order = list(DEFAULT_TEST_ORDER)
    for name in scores:
        if name not in order:
            order.append(name)
    position = {name: index for index, name in enumerate(order)}
    return sorted(order,
                  key=lambda name: (-scores[name]["score"],
                                    position[name], name))


# -- repair routing (memory.md section 25) ---------------------------


def _strategy_route_for_class(outcome, canonical):
    """Route of a strategy outcome attributed to *canonical*, or None."""
    if not isinstance(outcome, dict):
        return None
    route = _route_of_kind(outcome.get("strategy"))
    detail = outcome.get("detail")
    detail_class = None
    if isinstance(detail, dict):
        if route is None:
            route = _route_of_kind(detail.get("route"))
            if route is None:
                route = _route_of_kind(detail.get("repair_kind"))
        raw = detail.get("failure_class", detail.get("class"))
        if isinstance(raw, str):
            try:
                detail_class = trajectories.normalize_failure_class(raw)
            except (ValueError, TypeError):
                detail_class = None
    elif isinstance(detail, str):
        try:
            detail_class = trajectories.normalize_failure_class(detail)
        except (ValueError, TypeError):
            detail_class = None
    if route is None or detail_class != canonical:
        return None
    return route


def route_repair(failure_class, ledger=None):
    """Route a failure class to deterministic-fix/model-repair/escalate.

    With a ledger, recorded repair outcomes (and failure-attributed
    strategy outcomes) for the class may override the safe default from
    :data:`DEFAULT_REPAIR_ROUTES` -- but only with at least
    ``MIN_ROUTE_EVIDENCE`` total observations, ``MIN_ROUTE_DIRECT``
    direct observations of the winner, and a strictly better smoothed
    success rate. Otherwise, and whenever *ledger* is None, the default
    stands. Unknown classes safely escalate. Never raises. The ledger
    may also be passed first: ``route_repair(ledger, failure_class)``.
    """
    if _is_ledger(failure_class) and isinstance(ledger, str):
        failure_class, ledger = ledger, failure_class
    try:
        canonical = trajectories.normalize_failure_class(failure_class)
    except (ValueError, TypeError, AttributeError):
        return "escalate"
    default = DEFAULT_REPAIR_ROUTES.get(canonical, "escalate")
    if ledger is None:
        return default
    wins = {}
    totals = {}
    try:
        repair_outcomes = trajectories.list_repair_outcomes(ledger)
    except Exception:
        repair_outcomes = []
    for outcome in repair_outcomes:
        if not isinstance(outcome, dict):
            continue
        if outcome.get("failure_class") != canonical:
            continue
        route = _route_of_kind(outcome.get("repair_kind"))
        if route is None:
            continue
        totals[route] = totals.get(route, 0) + 1
        if outcome.get("success"):
            wins[route] = wins.get(route, 0) + 1
    try:
        strategy_outcomes = trajectories.list_strategy_outcomes(ledger)
    except Exception:
        strategy_outcomes = []
    for outcome in strategy_outcomes:
        route = _strategy_route_for_class(outcome, canonical)
        if route is None:
            continue
        totals[route] = totals.get(route, 0) + 1
        if outcome.get("success"):
            wins[route] = wins.get(route, 0) + 1
    if sum(totals.values()) < MIN_ROUTE_EVIDENCE:
        return default

    def smoothed(route):
        return ((wins.get(route, 0) + 1.0)
                / (totals.get(route, 0) + 2.0))

    best = default
    for route in REPAIR_ROUTES:
        if (totals.get(route, 0) >= MIN_ROUTE_DIRECT
                and smoothed(route) > smoothed(best)):
            best = route
    return best


# -- bounded escalation (memory.md sections 25-27, todos.md L6) ------


def _flags_from_attempts(attempts):
    """Attempt list -> (success flags, any outcome evidence)."""
    flags = []
    known = False
    for attempt in attempts:
        if isinstance(attempt, bool):
            flags.append(attempt)
            known = True
        elif isinstance(attempt, dict):
            flag = _outcome_of(attempt)
            if flag is None:
                flags.append(False)
            else:
                flags.append(flag)
                known = True
        elif attempt is None:
            flags.append(False)
        else:
            flags.append(False)
    return flags, known


def _truncate_at_success(flags):
    """Drop attempts after the first success (post-success noise)."""
    kept = []
    for flag in flags:
        kept.append(flag)
        if flag:
            break
    return kept


def _history_to_sequence(item):
    """One repair history -> (attempts, success) or None if unusable."""
    if isinstance(item, bool):
        return (1, item)
    if isinstance(item, (list, tuple)):
        flags, known = _flags_from_attempts(item)
        if not flags or not known:
            return None
        kept = _truncate_at_success(flags)
        return (len(kept), kept[-1])
    if isinstance(item, dict):
        outcome = _outcome_of(item)
        attempts = item.get("attempts")
        if isinstance(attempts, (list, tuple)):
            flags, known = _flags_from_attempts(attempts)
            if known and flags:
                kept = _truncate_at_success(flags)
                return (len(kept), kept[-1])
            if outcome is not None:
                return (max(len(flags), 1), outcome)
            return None
        if isinstance(attempts, int) and not isinstance(attempts, bool):
            if outcome is None:
                return None
            return (max(attempts, 1), outcome)
        repairs_used = item.get("repairs_used")
        if (isinstance(repairs_used, int)
                and not isinstance(repairs_used, bool)):
            if outcome is None:
                return None
            return (max(repairs_used, 0) + 1, outcome)
        history = item.get("history")
        if isinstance(history, (list, tuple)):
            if outcome is None:
                return None
            return (max(len(history), 0) + 1, outcome)
        if outcome is not None:
            return (1, outcome)
        return None
    return None


def _ledger_sequences(ledger):
    """Per-task ``(attempts, success)`` from repair outcomes/trajectories."""
    sequences = []
    try:
        repair_outcomes = trajectories.list_repair_outcomes(ledger)
    except Exception:
        repair_outcomes = []
    if repair_outcomes:
        by_task = {}
        for outcome in sorted(repair_outcomes,
                              key=lambda row: row.get("event_id", 0)):
            if not isinstance(outcome, dict):
                continue
            flags = by_task.setdefault(outcome.get("task_id"), [])
            flags.append(bool(outcome.get("success")))
        for flags in by_task.values():
            kept = _truncate_at_success(flags)
            if kept:
                sequences.append((len(kept), kept[-1]))
        if sequences:
            return sequences
    try:
        trajs = trajectories.query_trajectories(ledger)
    except Exception:
        trajs = []
    for traj in trajs:
        if not isinstance(traj, dict) or not traj.get("steps"):
            continue
        flags = []
        for step in traj["steps"]:
            if isinstance(step, dict):
                flags.append(bool(step.get("success", False)))
            elif isinstance(step, bool):
                flags.append(step)
        kept = _truncate_at_success(flags)
        if kept:
            sequences.append((len(kept), kept[-1]))
    return sequences


def _as_sequences(repair_histories):
    if repair_histories is None:
        return []
    if _is_ledger(repair_histories):
        return _ledger_sequences(repair_histories)
    if isinstance(repair_histories, dict):
        single = _history_to_sequence(repair_histories)
        return [single] if single is not None else []
    if isinstance(repair_histories, (list, tuple)):
        sequences = []
        for entry in repair_histories:
            parsed = _history_to_sequence(entry)
            if parsed is not None:
                sequences.append(parsed)
        return sequences
    return []


def learn_escalation_policy(repair_histories):
    """Derive bounded-escalation thresholds from repair histories.

    *repair_histories* is a list of per-task histories (attempt-flag
    lists, ``{"attempts", "success"}`` / ``{"repairs_used", "status"}``
    dicts, or ``repair_loop``-style records), an event ledger (repair
    outcomes grouped per task, else trajectory steps), or None.
    Returns ``{"max_repairs", "escalate_on_oscillation"}``. The budget
    starts at the default of 1 and only grows -- never beyond
    ``MAX_REPAIRS`` -- when a repair depth shows a smoothed
    eventual-success rate above ``MIN_MARGINAL_GAIN`` across enough
    histories. Oscillation always escalates: data never disables that
    safety invariant. Empty or sparse input returns
    :data:`DEFAULT_ESCALATION_POLICY` exactly.
    """
    sequences = _as_sequences(repair_histories)
    failures = [(attempts - 1 if success else attempts, success)
                for attempts, success in sequences]
    best = 1
    for depth in range(1, MAX_REPAIRS + 1):
        cohort = [(fails, won) for fails, won in failures
                  if fails >= depth]
        count = len(cohort)
        if count < MIN_SEQUENCE_EVIDENCE:
            continue
        wins = sum(1 for _, won in cohort if won)
        rate = wins / (count + SEQUENCE_PRIOR)
        if rate >= MIN_MARGINAL_GAIN:
            best = depth
    return {"max_repairs": best, "escalate_on_oscillation": True}


# -- risk calibration (memory.md section 27, todos.md L6) ------------
# NOTE: risk levels mirror ``risk.LEVELS``; re-declared here because
# this module must not import sibling lanes (see DEFAULT_TEST_ORDER).

#: Mutation-risk levels R0 (pure) through R6 (trust-root, forbidden).
RISK_LEVELS = ("R0", "R1", "R2", "R3", "R4", "R5", "R6")

#: Payload keys carrying a predicted risk level.
_RISK_LEVEL_KEYS = ("risk", "risk_level", "predicted", "predicted_risk",
                    "predicted_level", "classification", "risk_class",
                    "level")

#: Payload keys whose truthy value marks an actual production incident.
_RISK_INCIDENT_KEYS = ("incident", "actual_incident",
                       "production_incident", "production_regression",
                       "contamination", "state_contamination",
                       "regressed", "regression", "rollback",
                       "rolled_back")

#: Ledger event types that mark an actual production incident for the
#: linked task/candidate/capability id.
_RISK_INCIDENT_EVENTS = frozenset({
    "candidate rolled back",
    "rollback",
    "production rollback",
    "capability rolled back",
    "production incident",
    "incident",
    "capability failed",
})

#: Ledger event types carrying a predicted risk level. ``None`` payload
#: scans accept any event whose payload names a level; this set only
#: widens the get_events_by_type fallback probe list.
_RISK_PREDICTION_EVENTS = (
    "risk classified",
    "risk classification",
    "mutation classified",
    "candidate classified",
    "promotion decision",
    "candidate promoted",
    "candidate rejected",
)


def _normalize_risk_level(value):
    """Canonical R0-R6 level for *value*, or None when not a level."""
    if not isinstance(value, str):
        return None
    level = value.strip().upper().replace(" ", "")
    if level in RISK_LEVELS:
        return level
    return None


def _risk_prediction_of(payload):
    """Predicted risk level of a payload dict, or None."""
    if not isinstance(payload, dict):
        return None
    for key in _RISK_LEVEL_KEYS:
        if key in payload:
            level = _normalize_risk_level(payload[key])
            if level is not None:
                return level
    nested = payload.get("verdict")
    if isinstance(nested, dict):
        for key in _RISK_LEVEL_KEYS:
            if key in nested:
                level = _normalize_risk_level(nested[key])
                if level is not None:
                    return level
    return None


def _risk_incident_of(payload):
    """True when a payload dict explicitly reports a production incident."""
    if not isinstance(payload, dict):
        return False
    return any(bool(payload.get(key)) for key in _RISK_INCIDENT_KEYS)


def _scan_typed_payloads(ledger):
    """``(event_type, ids, payload)`` for every ledger event.

    ``ids`` is the tuple of non-null linkage ids
    (task_id, candidate_id, capability_id). Never raises: unreadable
    stores yield [].
    """
    found = []
    conn = getattr(ledger, "conn", None)
    if conn is not None and hasattr(conn, "execute"):
        try:
            rows = conn.execute(
                "SELECT event_type, task_id, candidate_id, "
                "capability_id, payload FROM events ORDER BY event_id"
            ).fetchall()
        except Exception:
            return []
        for event_type, task_id, candidate_id, capability_id, text in rows:
            try:
                payload = json.loads(text or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                ids = tuple(item for item in
                            (task_id, candidate_id, capability_id)
                            if item is not None)
                found.append((event_type, ids, payload))
        return found
    seen = set()
    for event_type in (_RISK_INCIDENT_EVENTS | {"trajectory recorded"}
                       | set(_RISK_PREDICTION_EVENTS)):
        try:
            rows = ledger.get_events_by_type(event_type)
        except Exception:
            continue
        for event in rows:
            if not isinstance(event, dict):
                continue
            marker = event.get("event_id", id(event))
            if marker in seen:
                continue
            seen.add(marker)
            try:
                payload = json.loads(event.get("payload", "{}"))
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                ids = tuple(item for item in
                            (event.get("task_id"),
                             event.get("candidate_id"),
                             event.get("capability_id"))
                            if item is not None)
                found.append((event.get("event_type"), ids, payload))
    return found


def _as_risk_pairs(source):
    """Ledger/history -> ``[(predicted_level, incident), ...]``.

    Ledger predictions join incidents on a shared task/candidate/
    capability id; in-memory records are self-contained (a predicted
    level plus an incident flag in one dict). Records without a
    recognizable predicted level are skipped. Never raises.
    """
    if source is None:
        return []
    if _is_ledger(source):
        try:
            scanned = _scan_typed_payloads(source)
        except Exception:
            return []
        predicted = {}
        incidents = set()
        for event_type, ids, payload in scanned:
            if not ids:
                continue
            key = ids[0]
            if key not in predicted:
                level = _risk_prediction_of(payload)
                if level is not None:
                    predicted[key] = (level, set(ids))
            incident = _risk_incident_of(payload)
            if not incident and isinstance(event_type, str):
                incident = (event_type.strip().lower()
                            in _RISK_INCIDENT_EVENTS)
            if incident:
                incidents.update(ids)
        return [(level, bool(ids & incidents))
                for level, ids in predicted.values()]
    if isinstance(source, dict):
        source = [source]
    if not isinstance(source, (list, tuple)):
        return []
    pairs = []
    for record in source:
        if not isinstance(record, dict):
            continue
        level = _risk_prediction_of(record)
        if level is None:
            continue
        pairs.append((level, _risk_incident_of(record)))
    return pairs


def learn_risk_model(ledger):
    """Calibrate predicted mutation risk against actual incidents.

    *ledger* is an event ledger (predictions joined to production
    incidents on a shared task/candidate/capability id), an
    in-memory list of ``{"risk": "R1", "incident": bool}``-style
    records, or None. Returns one calibration note per level,
    ``{"level", "predicted_count", "actual_incidents"}`` in R0-R6
    order: ``predicted_count`` tallies candidates classified at that
    level and ``actual_incidents`` tallies how many of them escaped
    to a production incident (memory.md section 27 -- e.g. repeated
    R1 incidents argue for reclassifying the family R2). Empty or
    unreadable input yields all-zero notes, never an exception.
    """
    try:
        pairs = _as_risk_pairs(ledger)
    except Exception:
        pairs = []
    predicted = {level: 0 for level in RISK_LEVELS}
    incidents = {level: 0 for level in RISK_LEVELS}
    for level, incident in pairs:
        predicted[level] += 1
        if incident:
            incidents[level] += 1
    return [{"level": level, "predicted_count": predicted[level],
             "actual_incidents": incidents[level]}
            for level in RISK_LEVELS]


# -- applicability boundaries (memory.md section 28, todos.md L6) ----


def _as_family_records(skills_store):
    """Skill store (or family records) -> list of family dicts.

    Accepts a store with ``list_families``, a single family record,
    or a list of family records. Never raises.
    """
    if skills_store is None:
        return []
    lister = getattr(skills_store, "list_families", None)
    if callable(lister):
        try:
            families = lister()
        except Exception:
            return []
        return [item for item in families if isinstance(item, dict)]
    if isinstance(skills_store, dict):
        return [skills_store]
    if isinstance(skills_store, (list, tuple)):
        return [item for item in skills_store if isinstance(item, dict)]
    return []


def _tags_of_outcome(outcome):
    """Task tags carried by one reuse-outcome record."""
    tags = set()
    for key in ("tags", "task_tags"):
        value = outcome.get(key)
        if isinstance(value, str) and value.strip():
            tags.add(value.strip())
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    tags.add(item.strip())
    task = outcome.get("task")
    if isinstance(task, dict):
        for key in ("tags", "task_tags"):
            value = task.get(key)
            if isinstance(value, str) and value.strip():
                tags.add(value.strip())
            elif isinstance(value, (list, tuple, set, frozenset)):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        tags.add(item.strip())
    elif isinstance(task, (list, tuple, set, frozenset)):
        for item in task:
            if isinstance(item, str) and item.strip():
                tags.add(item.strip())
    return tags


def _worked_of_outcome(outcome):
    """Worked/failed signal of one outcome record, or None if unknown."""
    for key in ("worked", "success", "succeeded"):
        if key in outcome and outcome[key] is not None:
            return bool(outcome[key])
    return _outcome_of(outcome)


def learn_applicability(skills_store, outcomes):
    """Tally when/when_not evidence per skill family (pure function).

    *skills_store* is a skill store (anything with
    ``list_families``), a single family record, or a list of family
    records; *outcomes* is an iterable of reuse-outcome dicts with a
    ``family_id``, task tags (``tags``/``task_tags``/``task``), and
    a worked signal (``worked``/``success``). Returns
    ``{family_id: {"when": {tag: {"success", "failure"}},
    "when_not": {...}}}`` counting, for each declared boundary tag,
    how often retrieval on tasks carrying that tag worked or failed
    (memory.md section 28). Declared tags with no observations keep
    explicit zero tallies; outcomes for unknown families, without
    tags, or without an outcome signal are skipped.

    Pure: reads the passed-in records only, mutates nothing, and
    never writes to the store.
    """
    families = _as_family_records(skills_store)
    tallies = {}
    declared = {}
    for family in families:
        family_id = family.get("family_id")
        if not family_id or not isinstance(family_id, str):
            continue
        app = family.get("applicability") or {}
        when = [tag for tag in (app.get("when") or [])
                if isinstance(tag, str)]
        when_not = [tag for tag in (app.get("when_not") or [])
                    if isinstance(tag, str)]
        declared[family_id] = (set(when), set(when_not))
        tallies[family_id] = {
            "when": {tag: {"success": 0, "failure": 0} for tag in when},
            "when_not": {tag: {"success": 0, "failure": 0}
                         for tag in when_not},
        }
    if isinstance(outcomes, dict):
        outcomes = [outcomes]
    if not isinstance(outcomes, (list, tuple)):
        return tallies
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        family_id = outcome.get("family_id")
        if family_id not in tallies:
            continue
        worked = _worked_of_outcome(outcome)
        if worked is None:
            continue
        when, when_not = declared[family_id]
        for tag in _tags_of_outcome(outcome):
            if tag in when:
                bucket = tallies[family_id]["when"][tag]
            elif tag in when_not:
                bucket = tallies[family_id]["when_not"][tag]
            else:
                continue
            bucket["success" if worked else "failure"] += 1
    return tallies


# -- rollback postmortem (memory.md section 30, todos.md L6) ---------


#: Event types recognized as production rollbacks (see
#: ``instrument.log_rollback``: "candidate rolled back").
_ROLLBACK_EVENT_TYPES = frozenset({
    "candidate rolled back",
    "rollback",
    "production rollback",
    "capability rolled back",
})

#: Payload keys that mark an untyped dict as a rollback record.
_ROLLBACK_SIGNAL_KEYS = ("reason", "from_version", "to_version",
                         "from-version", "to-version",
                         "failure_class", "failure-class",
                         "regressed_version")


def _rollback_payload(event):
    """Payload dict of a ledger event row or bare record.

    Ledger rows carry the payload as a JSON string under ``payload``;
    bare records already are the payload. Never raises.
    """
    if not isinstance(event, dict):
        return {}
    payload = event.get("payload", event)
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _is_rollback_record(event):
    """True when *event* is recognizable as a rollback record."""
    if not isinstance(event, dict):
        return False
    if "event_type" in event:
        event_type = event.get("event_type")
        return (isinstance(event_type, str)
                and event_type.strip().lower() in _ROLLBACK_EVENT_TYPES)
    payload = _rollback_payload(event)
    return any(key in payload for key in _ROLLBACK_SIGNAL_KEYS)


def postmortem_trigger(rollback_event):
    """Extract lesson-candidate dicts from a rollback record.

    *rollback_event* is one ledger event row (as produced by
    ``instrument.log_rollback``: type ``"candidate rolled back"``
    with payload ``from_version``/``to_version``/``reason``) or a
    bare payload-style dict. Returns a list of lesson-candidate
    dicts in the :func:`mine.propose_lesson_candidate
    <mine.propose_lesson_candidate>` shape (``statement``,
    ``lesson_class``, ``failure_class``, ``family``, evidenced
    ``evidence``/``applies_when``, single-incident ``confidence``
    0.55, ``status`` ``"candidate"``, plus rollback ``source``
    provenance) -- one candidate per rollback, ready for replay
    validation per memory.md section 30. Non-rollback events,
    untyped dicts without rollback signals, and non-dicts yield []
    instead of raising.
    """
    if not _is_rollback_record(rollback_event):
        return []
    payload = _rollback_payload(rollback_event)
    capability = rollback_event.get("capability_id",
                                    payload.get("capability_id"))
    if not isinstance(capability, str) or not capability:
        capability = payload.get("capability") or "unknown capability"
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "no reason recorded"
    else:
        reason = reason.strip()
    from_version = payload.get("from_version",
                               payload.get("from-version"))
    to_version = payload.get("to_version", payload.get("to-version"))
    versions = ""
    if from_version is not None or to_version is not None:
        versions = " (v%s -> v%s)" % (from_version, to_version)
    failure_class = None
    for key in ("failure_class", "failure-class", "class"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            try:
                failure_class = trajectories.normalize_failure_class(
                    value.strip())
            except (ValueError, TypeError):
                failure_class = value.strip()
            break
    if failure_class is None:
        failure = payload.get("failure")
        if isinstance(failure, dict):
            raw = failure.get("class")
            if isinstance(raw, str) and raw.strip():
                try:
                    failure_class = trajectories.normalize_failure_class(
                        raw.strip())
                except (ValueError, TypeError):
                    failure_class = raw.strip()
        elif isinstance(failure, str) and failure.strip():
            try:
                failure_class = trajectories.normalize_failure_class(
                    failure.strip())
            except (ValueError, TypeError):
                failure_class = failure.strip()
    family = payload.get("family")
    if not isinstance(family, str) or not family:
        family = None
    if failure_class:
        statement = (
            "Production rollback of capability '%s'%s after %s "
            "failure (%s): add a rehearsal check covering this "
            "failure mode before promotion."
            % (capability, versions, failure_class, reason))
    else:
        statement = (
            "Production rollback of capability '%s'%s (%s): add a "
            "rehearsal check covering this failure mode before "
            "promotion." % (capability, versions, reason))
    when = "tasks touching capability '%s'" % capability
    if failure_class:
        when += " with failure class '%s'" % failure_class
    event_ids = []
    if rollback_event.get("event_id") is not None:
        event_ids.append(rollback_event["event_id"])
    task_ids = []
    task_id = rollback_event.get("task_id", payload.get("task_id"))
    if task_id is not None:
        task_ids.append(task_id)
    return [{
        "statement": statement,
        "lesson_class": "failure",
        "failure_class": failure_class,
        "family": family,
        "evidence": {"event_ids": event_ids, "task_ids": task_ids,
                     "count": 1},
        "applies_when": {
            "when": when,
            "when_not": "tasks outside capability '%s'" % capability,
        },
        "confidence": 0.55,
        "status": "candidate",
        "source": "rollback",
    }]
