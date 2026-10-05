"""Transfer-based promotion (plan.md sections 29-31).

A patch becomes a skill only after demonstrating reuse on related
tasks. This module records per-patch reuse outcomes and evaluates the
transfer gate from plan.md section 30::

    minimum independent reuse: 3
    negative transfer: 0 severe regressions
    held-out success: >= baseline
    resource regression: within configured budget

:func:`promote_or_hold` (and the
:meth:`TransferTracker.promote_or_hold` method) returns::

    {"decision": "promote" | "hold" | "retire", "reasons": [...]}

plus a ``metrics`` dict with reuse counts, success/token/latency
deltas, and negative-transfer counts.

Outcome record shape (all keys optional except identity)::

    {
        "patch_id": str,
        "task_id": str,            # independence unit
        "helped": bool,            # or "success"/"succeeded"
        "tokens_saved": float,     # + means saved
        "latency_saved_ms": float, # + means saved
        "severe": bool,            # severe regression flag
        "held_out": bool,          # held-out task flag
        "success": bool,           # alias of helped
    }

Stdlib only.
"""

import json
import os
import time

DEFAULT_MIN_REUSES = 3

DECISION_PROMOTE = "promote"
DECISION_HOLD = "hold"
DECISION_RETIRE = "retire"


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "t")
    return bool(value)


def _as_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _helped_of(outcome):
    for key in ("helped", "success", "succeeded"):
        if key in outcome:
            return _as_bool(outcome[key])
    return False


def _severe_of(outcome):
    for key in ("severe", "severe_regression", "severe_negative_transfer"):
        if key in outcome and _as_bool(outcome[key]):
            return True
    return False


def _held_out_of(outcome):
    for key in ("held_out", "heldout", "held-out", "unseen"):
        if key in outcome and _as_bool(outcome[key]):
            return True
    return False


def _tokens_saved_of(outcome):
    for key in ("tokens_saved", "token_saved", "token_delta", "tokens_delta"):
        if key in outcome:
            return _as_float(outcome[key])
    # Fallback: baseline - actual when deltas are not precomputed.
    if "tokens_baseline" in outcome or "tokens_actual" in outcome:
        base = _as_float(outcome.get("tokens_baseline"))
        actual = _as_float(outcome.get("tokens_actual"))
        if outcome.get("tokens_baseline") is not None or \
                outcome.get("tokens_actual") is not None:
            return base - actual
    return 0.0


def _latency_saved_of(outcome):
    for key in ("latency_saved_ms", "latency_saved", "latency_delta_ms",
                "latency_delta"):
        if key in outcome:
            return _as_float(outcome[key])
    if "latency_baseline_ms" in outcome or "latency_actual_ms" in outcome:
        base = _as_float(outcome.get("latency_baseline_ms"))
        actual = _as_float(outcome.get("latency_actual_ms"))
        return base - actual
    return 0.0


def summarize_outcomes(outcomes, baseline_success=0.0):
    """Compute transfer metrics from a list of outcome dicts."""
    outcomes = list(outcomes or [])
    task_ids = set()
    helped = 0
    severe = 0
    negative = 0
    tokens_total = 0.0
    latency_total = 0.0
    held_out_total = 0
    held_out_helped = 0
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        task_id = outcome.get("task_id")
        if task_id is not None:
            task_ids.add(str(task_id))
        ok = _helped_of(outcome)
        if ok:
            helped += 1
        else:
            negative += 1
        if _severe_of(outcome):
            severe += 1
        tokens_total += _tokens_saved_of(outcome)
        latency_total += _latency_saved_of(outcome)
        if _held_out_of(outcome):
            held_out_total += 1
            if ok:
                held_out_helped += 1
    total = len(outcomes)
    # Independent reuses: distinct task ids when present, else row count.
    independent = len(task_ids) if task_ids else total
    success_rate = (helped / total) if total else 0.0
    baseline = _as_float(baseline_success, 0.0)
    held_out_rate = (held_out_helped / held_out_total) \
        if held_out_total else None
    return {
        "reuse_count": total,
        "independent_reuses": independent,
        "success_count": helped,
        "failure_count": total - helped,
        "success_rate": success_rate,
        "success_delta": success_rate - baseline,
        "baseline_success": baseline,
        "held_out_total": held_out_total,
        "held_out_successes": held_out_helped,
        "held_out_rate": held_out_rate,
        "held_out_delta": (held_out_rate - baseline)
        if held_out_rate is not None else None,
        "tokens_saved_total": tokens_total,
        "token_delta": tokens_total,
        "latency_saved_ms_total": latency_total,
        "latency_delta_ms": latency_total,
        "negative_transfer_count": negative,
        "negative_transfers": negative,
        "severe_regressions": severe,
        "severe_regression_count": severe,
    }


# Backwards/forwards-compatible alias.
compute_deltas = summarize_outcomes


def _decide(metrics, min_reuses=DEFAULT_MIN_REUSES, baseline_success=0.0,
            max_token_regression=0.0, max_latency_regression_ms=0.0,
            allow_held_out_fallback=True):
    reasons = []
    decision = DECISION_PROMOTE

    severe = metrics["severe_regressions"]
    if severe > 0:
        return {"decision": DECISION_RETIRE,
                "reasons": ["%d severe regression(s): "
                            "plan.md section 30 requires 0" % severe],
                "metrics": metrics}

    independent = metrics["independent_reuses"]
    if independent < min_reuses:
        reasons.append("insufficient evidence: %d independent reuse(s), "
                       "need %d" % (independent, min_reuses))
        decision = DECISION_HOLD

    # Held-out success must meet baseline. When no outcome is flagged
    # held-out, fall back to the overall success rate so plain
    # helped/not-helped logs still exercise the gate.
    held_out_rate = metrics["held_out_rate"]
    baseline = _as_float(baseline_success, 0.0)
    if held_out_rate is None and allow_held_out_fallback:
        held_out_rate = metrics["success_rate"] if metrics["reuse_count"] \
            else 0.0
    if held_out_rate is not None and held_out_rate < baseline:
        reasons.append("held-out success %.3f below baseline %.3f"
                       % (held_out_rate, baseline))
        decision = DECISION_HOLD

    token_budget = _as_float(max_token_regression, 0.0)
    latency_budget = _as_float(max_latency_regression_ms, 0.0)
    if metrics["tokens_saved_total"] < -token_budget:
        reasons.append("token regression %.1f exceeds budget %.1f"
                       % (-metrics["tokens_saved_total"], token_budget))
        decision = DECISION_HOLD
    if metrics["latency_saved_ms_total"] < -latency_budget:
        reasons.append("latency regression %.1fms exceeds budget %.1fms"
                       % (-metrics["latency_saved_ms_total"],
                          latency_budget))
        decision = DECISION_HOLD

    if metrics["negative_transfer_count"] > 0 and decision == DECISION_PROMOTE:
        # Non-severe negative transfer alone does not block promotion
        # once the other gates pass, but record it for the caller.
        reasons.append("%d non-severe negative transfer(s) noted"
                       % metrics["negative_transfer_count"])

    if decision == DECISION_PROMOTE:
        reasons.append("transfer gate passed: %d independent reuses, "
                       "0 severe regressions" % independent)
    return {"decision": decision, "reasons": reasons, "metrics": metrics}


def promote_or_hold(patch_id, outcomes=None, baseline_success=0.0,
                    min_reuses=DEFAULT_MIN_REUSES,
                    max_token_regression=0.0,
                    max_latency_regression_ms=0.0, **kwargs):
    """Evaluate the transfer gate for one patch.

    ``patch_id`` may be a patch id string, or an outcomes list when the
    caller has no id handy (``promote_or_hold(outcomes)``). ``outcomes``
    is a list of outcome dicts; when omitted and ``patch_id`` names a
    patch tracked by the module-level default tracker, those outcomes
    are used (normally empty).
    """
    # Flexible calling: promote_or_hold(outcomes) or
    # promote_or_hold(patch_id, outcomes).
    if isinstance(patch_id, (list, tuple)) and outcomes is None:
        outcomes = list(patch_id)
        patch_id = None
    if outcomes is None:
        outcomes = []
    if isinstance(outcomes, dict):
        outcomes = [outcomes]
    baseline = kwargs.pop("baseline", baseline_success)
    min_reuses = kwargs.pop("min_independent_reuses", min_reuses)
    metrics = summarize_outcomes(outcomes, baseline_success=baseline)
    result = _decide(metrics, min_reuses=min_reuses,
                     baseline_success=baseline,
                     max_token_regression=max_token_regression,
                     max_latency_regression_ms=max_latency_regression_ms)
    result["patch_id"] = patch_id
    return result


class TransferTracker:
    """In-memory (optionally persisted) per-patch reuse outcome log."""

    def __init__(self, directory=None, min_reuses=DEFAULT_MIN_REUSES,
                 baseline_success=0.0, max_token_regression=0.0,
                 max_latency_regression_ms=0.0):
        self.min_reuses = min_reuses
        self.baseline_success = baseline_success
        self.max_token_regression = max_token_regression
        self.max_latency_regression_ms = max_latency_regression_ms
        self._outcomes = {}
        self._directory = None
        self._store_path = None
        if directory is not None:
            self._directory = os.path.abspath(directory)
            os.makedirs(self._directory, exist_ok=True)
            self._store_path = os.path.join(self._directory,
                                            "transfer_outcomes.json")
            self._load()

    # -- persistence ------------------------------------------------
    def _load(self):
        if not self._store_path or not os.path.exists(self._store_path):
            return
        try:
            with open(self._store_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            for patch_id, rows in data.items():
                if isinstance(rows, list):
                    self._outcomes[str(patch_id)] = [
                        r for r in rows if isinstance(r, dict)]

    def _save(self):
        if not self._store_path:
            return
        tmp = self._store_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._outcomes, fh, sort_keys=True, default=str)
        os.replace(tmp, self._store_path)

    # -- recording ---------------------------------------------------
    def record_outcome(self, patch_id, task_id=None, helped=None,
                       success=None, tokens_saved=0,
                       latency_saved_ms=0.0, severe=False,
                       held_out=False, **extra):
        """Record one reuse outcome for ``patch_id``; returns the row."""
        if isinstance(patch_id, dict):
            blob = dict(patch_id)
            patch_id = blob.pop("patch_id", None)
            task_id = blob.pop("task_id", task_id)
            helped = blob.pop("helped", blob.pop("success", helped))
            tokens_saved = blob.pop("tokens_saved", tokens_saved)
            latency_saved_ms = blob.pop("latency_saved_ms",
                                        latency_saved_ms)
            severe = blob.pop("severe", blob.pop("severe_regression",
                                                 severe))
            held_out = blob.pop("held_out", held_out)
            extra = dict(blob, **extra)
        if helped is None and success is not None:
            helped = success
        if helped is None:
            helped = extra.pop("succeeded", False)
        outcome = {
            "patch_id": patch_id,
            "task_id": task_id,
            "helped": bool(helped),
            "tokens_saved": tokens_saved,
            "latency_saved_ms": latency_saved_ms,
            "severe": bool(severe),
            "held_out": bool(held_out),
            "recorded_at": time.time(),
        }
        outcome.update(extra)
        # Keep helped/success aliases consistent for readers.
        outcome["success"] = outcome["helped"]
        key = str(patch_id)
        self._outcomes.setdefault(key, []).append(outcome)
        self._save()
        return outcome

    record_reuse = record_outcome
    log_outcome = record_outcome
    log_reuse = record_outcome
    add_outcome = record_outcome

    def get_outcomes(self, patch_id):
        """Return recorded outcomes for ``patch_id`` (list, possibly empty)."""
        return list(self._outcomes.get(str(patch_id), []))

    get_reuses = get_outcomes
    list_outcomes = get_outcomes

    # -- metrics ------------------------------------------------------
    def summarize(self, patch_id, outcomes=None, baseline_success=None):
        """Compute transfer metrics for ``patch_id``."""
        if outcomes is None:
            outcomes = self.get_outcomes(patch_id)
        if baseline_success is None:
            baseline_success = self.baseline_success
        return summarize_outcomes(outcomes,
                                  baseline_success=baseline_success)

    compute_deltas = summarize
    stats = summarize
    metrics = summarize

    # -- gate ----------------------------------------------------------
    def promote_or_hold(self, patch_id, outcomes=None, baseline_success=None,
                        min_reuses=None, max_token_regression=None,
                        max_latency_regression_ms=None, **kwargs):
        """Evaluate the transfer gate for ``patch_id``.

        When ``outcomes`` is omitted, the tracker's recorded outcomes
        for ``patch_id`` are used. Accepts ``promote_or_hold(outcomes)``
        (list as first arg) as well.
        """
        if isinstance(patch_id, (list, tuple)) and outcomes is None:
            outcomes = list(patch_id)
            patch_id = kwargs.pop("patch_id", None)
        if outcomes is None:
            outcomes = self.get_outcomes(patch_id)
        if isinstance(outcomes, dict):
            outcomes = [outcomes]
        if baseline_success is None:
            baseline_success = kwargs.pop("baseline",
                                          self.baseline_success)
        if min_reuses is None:
            min_reuses = kwargs.pop("min_independent_reuses",
                                    self.min_reuses)
        if max_token_regression is None:
            max_token_regression = self.max_token_regression
        if max_latency_regression_ms is None:
            max_latency_regression_ms = self.max_latency_regression_ms
        metrics = summarize_outcomes(outcomes,
                                     baseline_success=baseline_success)
        result = _decide(metrics, min_reuses=min_reuses,
                         baseline_success=baseline_success,
                         max_token_regression=max_token_regression,
                         max_latency_regression_ms=max_latency_regression_ms)
        result["patch_id"] = patch_id
        return result

    evaluate = promote_or_hold
    evaluate_promotion = promote_or_hold
    check_transfer_gate = promote_or_hold
