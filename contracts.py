"""Task success contracts + green-stop gate.

Implements plan.md section 18 (Green-Stop) and the "Implement green-stop"
items from todos.md:

- Define task success contracts (``TaskContract``: success predicates +
  required evidence keys).
- Stop model interaction immediately when the contract is satisfied
  (``halt_on_green``).
- Prevent optional refactors after success / track unnecessary
  post-success model actions as failures (``PostSuccessTracker``).

Completion is determined by the harness, never by the model: the gate
returns True exactly when the contract is satisfied.

Verdict shape (shared with the rehearsal pipeline and the repair loop):

.. code-block:: python

    {"passed": bool, "evidence": {...}, "failures": [...]}  # pipeline
    {"verdict": "pass" | "fail", "evidence": {...}}         # evaluator

Predicates receive the whole verdict mapping so they can inspect either
shape. Required evidence keys are looked up in ``verdict["evidence"]``.

Stdlib only.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple


# A success predicate inspects a verdict mapping and returns True when the
# aspect of success it guards is satisfied.
Predicate = Callable[[Mapping[str, Any]], bool]


@dataclass
class TaskContract:
    """Success contract for one task.

    ``predicates`` maps a predicate name to a callable taking the verdict
    mapping and returning True when satisfied. Accepts either a mapping or
    a sequence of ``(name, callable)`` pairs.

    ``required_evidence`` names keys that MUST be present in
    ``verdict["evidence"]`` for the contract to be satisfied.
    """

    name: str
    predicates: Any = field(default_factory=dict)
    required_evidence: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if isinstance(self.predicates, Mapping):
            items = list(self.predicates.items())
        else:
            items = [(name, fn) for name, fn in self.predicates]
        for name, fn in items:
            if not isinstance(name, str) or not name:
                raise ValueError("predicate names must be non-empty strings")
            if not callable(fn):
                raise ValueError("predicate %r is not callable" % (name,))
        object.__setattr__(self, "predicates", tuple(items))
        object.__setattr__(self, "required_evidence",
                           tuple(self.required_evidence))


def _evidence_of(verdict: Mapping[str, Any]) -> Mapping[str, Any]:
    evidence = verdict.get("evidence", {})
    return evidence if isinstance(evidence, Mapping) else {}


def check_contract(contract: TaskContract,
                   verdict: Mapping[str, Any]) -> Dict[str, Any]:
    """Evaluate ``contract`` against ``verdict``.

    Returns exactly ``{"satisfied", "missing", "evidence"}`` where:

    - ``satisfied`` is True iff every predicate holds and every required
      evidence key is present;
    - ``missing`` lists unmet predicate names plus ``"evidence:<key>"``
      entries for absent evidence keys;
    - ``evidence`` maps each present required evidence key to its value.

    A predicate that raises is treated as unmet (a broken check can never
    silently pass the gate).
    """
    if not isinstance(verdict, Mapping):
        raise TypeError("verdict must be a mapping")
    missing: List[str] = []
    for name, predicate in contract.predicates:
        try:
            holds = predicate(verdict)
        except Exception:
            holds = False
        if not holds:
            missing.append(name)
    evidence = _evidence_of(verdict)
    present: Dict[str, Any] = {}
    for key in contract.required_evidence:
        if key in evidence:
            present[key] = evidence[key]
        else:
            missing.append("evidence:" + key)
    return {"satisfied": not missing, "missing": missing, "evidence": present}


def halt_on_green(contract: TaskContract,
                  verdict: Mapping[str, Any]) -> bool:
    """Green-stop gate: True exactly when the contract is satisfied.

    Once this returns True the runtime MUST stop the agent — no further
    model interaction, no optional refactors. Completion is determined by
    the harness (this function), never by the model.
    """
    return bool(check_contract(contract, verdict)["satisfied"])


class PostSuccessTracker:
    """Flag any model/pipeline action taken after green.

    Usage: call :meth:`mark_green` once the harness declares success
    (``halt_on_green`` returned True), then route every subsequent
    model/pipeline action through :meth:`record_action`. Actions recorded
    after green each produce a violation record; actions before green
    return None.
    """

    def __init__(self) -> None:
        self._green = False
        self._green_contract: Optional[str] = None
        self._actions = 0
        self._violations: List[Dict[str, Any]] = []

    @property
    def green(self) -> bool:
        """True once success has been declared."""
        return self._green

    @property
    def action_count(self) -> int:
        """Total actions recorded (pre- and post-green)."""
        return self._actions

    @property
    def violations(self) -> Tuple[Dict[str, Any], ...]:
        """Violation records for post-success actions, oldest first."""
        return tuple(self._violations)

    @property
    def violation_count(self) -> int:
        """Number of post-success actions flagged."""
        return len(self._violations)

    def mark_green(self, contract_name: Optional[str] = None) -> None:
        """Declare success; all later recorded actions are violations."""
        self._green = True
        self._green_contract = contract_name

    def record_action(self, action: str,
                      detail: Any = None) -> Optional[Dict[str, Any]]:
        """Record one model/pipeline action.

        Returns a violation record
        ``{"type", "seq", "action", "detail", "contract"}`` when green was
        already declared, else None.
        """
        if not isinstance(action, str) or not action:
            raise ValueError("action must be a non-empty string")
        self._actions += 1
        if not self._green:
            return None
        violation = {
            "type": "post_success_action",
            "seq": len(self._violations) + 1,
            "action": action,
            "detail": detail,
            "contract": self._green_contract,
        }
        self._violations.append(violation)
        return violation
