"""A/B replay validation for lesson candidates (learning L4).

Implements memory.md section 6 and 35 (phase L4), plus the todos.md
"Validate lessons by replay (learning L4)" items: replay historical
tasks with the original context (control) versus the original context
plus the lesson, measure first-pass success, repair count, tokens,
wall time, and regressions, then recommend promote/reject.

The task runner is INJECTED as ``task_fn(task, context_extras)`` so
this harness never imports the pipeline; tests pass fakes.

Stdlib only.
"""

DEFAULT_THRESHOLDS = {
    #: Lesson first-pass rate must beat control by at least this much
    #: to promote (neutral lessons are rejected per todos.md L4).
    "min_first_pass_delta": 0.0,
    #: Lesson regressions must not exceed control by more than this.
    "max_regression_delta": 0,
}
"""Default promote/reject thresholds (see :func:`ab_replay`)."""

#: Recommendation values returned by :func:`ab_replay`.
PROMOTE = "promote"
REJECT = "reject"


def _normalize_result(result, task):
    if not isinstance(result, dict):
        raise TypeError("task_fn must return a dict per task, got %s "
                        "for task %r" % (type(result).__name__, task))
    first_pass = bool(result.get("first_pass", False))
    normalized = {"first_pass": first_pass}
    for key, cast in (("repairs", int), ("tokens", int),
                      ("time_ms", float), ("regressions", int)):
        value = result.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("task %r: result %r must be numeric, got %r"
                             % (task, key, value))
        if value < 0:
            raise ValueError("task %r: result %r must be >= 0, got %r"
                             % (task, key, value))
        normalized[key] = cast(value)
    return normalized


def _aggregate(results):
    count = len(results)
    return {
        "first_pass_rate": (sum(1 for r in results if r["first_pass"])
                            / count),
        "mean_repairs": sum(r["repairs"] for r in results) / count,
        "mean_tokens": sum(r["tokens"] for r in results) / count,
        "mean_time_ms": sum(r["time_ms"] for r in results) / count,
        "total_regressions": sum(r["regressions"] for r in results),
    }


def ab_replay(task_fn, tasks, *, lesson_text, thresholds=None,
              control_extras=None, lesson_extras=None):
    """Replay *tasks* with and without a lesson; recommend promote/reject.

    *task_fn* is called as ``task_fn(task, context_extras)`` once per
    task per arm and must return a dict with ``first_pass`` (bool) plus
    numeric ``repairs``, ``tokens``, ``time_ms``, and ``regressions``
    (missing numerics default to 0). The control arm receives
    *control_extras* (default ``{}``); the lesson arm receives
    *lesson_extras* (default ``{"lessons": [lesson_text]}``).

    Returns a dict with per-arm aggregates, ``deltas`` (lesson minus
    control, so negative repair/token/time deltas are improvements),
    ``per_task`` details, the effective ``thresholds``, and a
    ``recommendation``: ``promote`` requires no first-pass regression
    (per ``min_first_pass_delta``), no added regressions (per
    ``max_regression_delta``), and at least one strict improvement;
    anything else is ``reject`` (harmful AND neutral lessons fail).
    """
    if not callable(task_fn):
        raise TypeError("task_fn must be callable, got %s"
                        % type(task_fn).__name__)
    tasks = list(tasks)
    if not tasks:
        raise ValueError("ab_replay() requires a non-empty task list")
    if not lesson_text or not isinstance(lesson_text, str):
        raise ValueError("ab_replay() requires a non-empty 'lesson_text'")
    effective = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        for key, value in thresholds.items():
            if key not in effective:
                raise ValueError("unknown threshold %r (expected: %s)"
                                 % (key, sorted(effective)))
            effective[key] = value
    control = dict(control_extras) if control_extras else {}
    lesson_ctx = (dict(lesson_extras) if lesson_extras is not None
                  else {"lessons": [lesson_text]})

    control_results = []
    lesson_results = []
    per_task = []
    for task in tasks:
        control_result = _normalize_result(task_fn(task, control), task)
        lesson_result = _normalize_result(task_fn(task, lesson_ctx), task)
        control_results.append(control_result)
        lesson_results.append(lesson_result)
        per_task.append({"task": task, "control": control_result,
                         "lesson": lesson_result})

    control_agg = _aggregate(control_results)
    lesson_agg = _aggregate(lesson_results)
    first_pass_delta = (lesson_agg["first_pass_rate"]
                        - control_agg["first_pass_rate"])
    repair_delta = lesson_agg["mean_repairs"] - control_agg["mean_repairs"]
    token_delta = lesson_agg["mean_tokens"] - control_agg["mean_tokens"]
    time_delta = (lesson_agg["mean_time_ms"] - control_agg["mean_time_ms"])
    regression_delta = (lesson_agg["total_regressions"]
                        - control_agg["total_regressions"])
    deltas = {
        "first_pass_delta": first_pass_delta,
        "repair_delta": repair_delta,
        "token_delta": token_delta,
        "time_delta": time_delta,
        "regression_delta": regression_delta,
    }

    improved = (first_pass_delta > 0 or repair_delta < 0
                or token_delta < 0 or time_delta < 0)
    if (first_pass_delta >= effective["min_first_pass_delta"]
            and regression_delta <= effective["max_regression_delta"]
            and improved):
        recommendation = PROMOTE
    else:
        recommendation = REJECT

    return {
        "n_tasks": len(tasks),
        "lesson_text": lesson_text,
        "control": control_agg,
        "lesson": lesson_agg,
        "deltas": deltas,
        "per_task": per_task,
        "thresholds": effective,
        "recommendation": recommendation,
    }
