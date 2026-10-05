"""Mutation-storm controls (issues.md #29 remainder). Stdlib only.

:mod:`repair` owns single-loop oscillation: the same failure signature
twice in one repair loop freezes the target and escalates. This module
owns the WIDER storm surface that :mod:`repair` cannot see:

* target freeze after N consecutive failures (across repair loops and
  tasks — a target that fails every time it is touched),
* cross-task cycle detection (one failure signature spanning many
  distinct tasks — a systemic loop, not a local bug),
* persisted escalation records (JSONL sink plus in-memory log).

Interop (no import either way): :func:`_signature_of` accepts the same
``{"property", "expected", "actual"}`` summary shape that
:func:`repair.summarize_failure` produces, canonicalized exactly like
:func:`repair.failure_signature` (``"%r|%r|%r"``), so repair summaries
feed this tracker directly. Single-loop oscillation stays in
:mod:`repair` — it is verified there, not duplicated here.
"""

from __future__ import annotations

import json
import time
import uuid

__all__ = [
    "DEFAULT_MAX_CONSECUTIVE_FAILURES",
    "DEFAULT_CROSS_TASK_THRESHOLD",
    "TargetFrozenError",
    "StormTracker",
    "load_escalations",
]

#: Consecutive failures on one target before it freezes.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

#: Distinct tasks sharing one failure signature before a cross-task
#: cycle is declared.
DEFAULT_CROSS_TASK_THRESHOLD = 3


class TargetFrozenError(Exception):
    """Raised by :meth:`StormTracker.check` for a frozen target."""

    def __init__(self, target, record):
        super().__init__(
            "target %r is frozen: %s" % (target, record.get("reason"))
        )
        self.target = target
        self.record = record


def _signature_of(signature):
    """Canonicalize a signature: string or repair-summary dict."""
    if isinstance(signature, str):
        if not signature:
            raise ValueError("signature must be a non-empty string or dict")
        return signature
    if isinstance(signature, dict):
        prop = signature.get("property")
        expected = signature.get("expected")
        actual = signature.get("actual")
        return "%r|%r|%r" % (prop, expected, actual)
    raise TypeError(
        "signature must be a str or repair-summary dict, got %s"
        % type(signature).__name__
    )


def load_escalations(path):
    """Read escalation records back from a JSONL sink file."""
    records = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


class StormTracker:
    """Cross-loop storm state: freezes, cycles, escalation records."""

    def __init__(self, max_consecutive_failures=None,
                 cross_task_threshold=None, sink=None):
        max_consecutive_failures = (
            DEFAULT_MAX_CONSECUTIVE_FAILURES
            if max_consecutive_failures is None
            else max_consecutive_failures
        )
        cross_task_threshold = (
            DEFAULT_CROSS_TASK_THRESHOLD
            if cross_task_threshold is None
            else cross_task_threshold
        )
        if max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be >= 1")
        if cross_task_threshold < 2:
            raise ValueError("cross_task_threshold must be >= 2")
        self.max_consecutive_failures = max_consecutive_failures
        self.cross_task_threshold = cross_task_threshold
        self.sink = sink
        #: target -> {"consecutive": int, "frozen": record-or-None}.
        self._targets = {}
        #: signature -> set of task ids that produced it.
        self._signature_tasks = {}
        #: signatures already escalated as cycles (escalate once per
        #: signature, not once per new task).
        self._cycled = set()
        #: escalation records, oldest first.
        self.escalations = []

    # -- recording -------------------------------------------------

    def _state(self, target):
        return self._targets.setdefault(
            target, {"consecutive": 0, "frozen": None}
        )

    def record_failure(self, target, task_id, signature, evidence=None):
        """Record one failure; maybe freeze and/or escalate.

        Returns a status dict ``{"target", "consecutive", "frozen",
        "cycle"}`` where ``frozen`` is the freeze record (or None) and
        ``cycle`` is the cross-task cycle record (or None).
        """
        if not target:
            raise ValueError("target is required")
        if task_id is None or task_id == "":
            raise ValueError("task_id is required")
        canonical = _signature_of(signature)
        state = self._state(target)
        state["consecutive"] += 1
        tasks = self._signature_tasks.setdefault(canonical, set())
        tasks.add(task_id)
        frozen = state["frozen"]
        if frozen is None and (
            state["consecutive"] >= self.max_consecutive_failures
        ):
            frozen = self.escalate(
                target,
                "consecutive_failures",
                {
                    "consecutive": state["consecutive"],
                    "threshold": self.max_consecutive_failures,
                    "task_id": task_id,
                    "signature": canonical,
                    "detail": evidence,
                },
            )
            state["frozen"] = frozen
        cycle = None
        if (
            len(tasks) >= self.cross_task_threshold
            and canonical not in self._cycled
        ):
            self._cycled.add(canonical)
            cycle = self.escalate(
                target,
                "cross_task_cycle",
                {
                    "signature": canonical,
                    "tasks": sorted(tasks, key=repr),
                    "task_count": len(tasks),
                    "threshold": self.cross_task_threshold,
                },
            )
            # A systemic cycle freezes the reporting target too (unless
            # a freeze record already stands): stop the loop, then
            # diagnose.
            if frozen is None:
                frozen = cycle
                state["frozen"] = cycle
        return {
            "target": target,
            "consecutive": state["consecutive"],
            "frozen": frozen,
            "cycle": cycle,
        }

    def record_success(self, target, task_id=None):
        """Record one success: resets the consecutive-failure counter.

        A freeze is STICKY: success alone does not unfreeze (a frozen
        target should not have been attempted at all). Use
        :meth:`unfreeze` with an explicit reason.
        """
        if not target:
            raise ValueError("target is required")
        self._state(target)["consecutive"] = 0

    # -- freezes ----------------------------------------------------

    def is_frozen(self, target):
        """True when TARGET is currently frozen."""
        state = self._targets.get(target)
        return bool(state and state["frozen"])

    def check(self, target):
        """Return None when TARGET is clear; raise :class:`TargetFrozenError`
        when it is frozen. Call before attempting work on a target."""
        state = self._targets.get(target)
        if state and state["frozen"]:
            raise TargetFrozenError(target, state["frozen"])
        return None

    def unfreeze(self, target, reason):
        """Lift a freeze with an explicit reason; resets the counter.

        Records an ``"unfrozen"`` escalation entry for the audit trail.
        Raises KeyError when the target is not frozen.
        """
        if not reason:
            raise ValueError("unfreeze reason is required")
        state = self._targets.get(target)
        if not state or not state["frozen"]:
            raise KeyError("target %r is not frozen" % (target,))
        state["frozen"] = None
        state["consecutive"] = 0
        return self.escalate(
            target, "unfrozen", {"reason": reason}
        )

    # -- escalation records ------------------------------------------

    def escalate(self, target, reason, evidence=None):
        """Append and return one escalation record.

        Records are ``{"id", "timestamp", "target", "reason",
        "evidence", "frozen"}``; ``frozen`` is True for freeze-causing
        reasons. When a sink path was configured, the record is also
        appended to it as one JSON line (the sink write never fails the
        call: OSError propagates — a lost escalation record must be
        loud, not silent).
        """
        if not target:
            raise ValueError("target is required")
        if not reason:
            raise ValueError("reason is required")
        record = {
            "id": uuid.uuid4().hex,
            "timestamp": time.time(),
            "target": target,
            "reason": reason,
            "evidence": evidence or {},
            "frozen": reason
            in ("consecutive_failures", "cross_task_cycle"),
        }
        self.escalations.append(record)
        if self.sink is not None:
            with open(self.sink, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return record
