"""Task-outcome taxonomy for the reuse thesis (session goal 2026-10-05).

Every task in the canonical A/B/C/D comparison ends in exactly one of:

    REUSE   solved by executing ONE learned capability, zero model calls
    COMPOSE solved by executing a CHAIN of learned capabilities, zero calls
    ADAPT   a capability was retrieved but the model was still needed
    NOVEL   no applicable capability; solved from scratch (or failed trying)

Desired trends on unseen tasks::

    REUSE up, COMPOSE up, NOVEL down,
    model calls/task down, tokens/task down,
    held-out success flat or up.

A growing capability count alongside flat REUSE/COMPOSE is a warning:
learning should compress many tasks into fewer abstractions, not
memorize one function per task (see :func:`compression`).
"""

from __future__ import annotations

REUSE = "REUSE"
COMPOSE = "COMPOSE"
ADAPT = "ADAPT"
NOVEL = "NOVEL"

OUTCOMES = (REUSE, COMPOSE, ADAPT, NOVEL)


def classify(model_calls: int, executed: int, retrieved: bool) -> str:
    """Classify one task outcome from its execution record.

    ``model_calls`` is the number of LLM calls spent on the task,
    ``executed`` the widest single-check execution (1 = one capability,
    >1 = a composition; totals across checks do NOT count -- two
    single-capability checks are still REUSE), ``retrieved`` whether
    any capability text was injected into a model prompt.
    """
    if model_calls <= 0 and executed == 1:
        return REUSE
    if model_calls <= 0 and executed > 1:
        return COMPOSE
    if retrieved:
        return ADAPT
    return NOVEL


def aggregate(records: list) -> dict:
    """Aggregate per-task records into outcome counts + inference means.

    Each record needs ``outcome`` plus ``calls``, ``tokens``, ``passed``.
    Returns counts per outcome, means per task, success rate, and n.
    """
    counts = {name: 0 for name in OUTCOMES}
    calls = 0
    tokens = 0
    passed = 0
    for record in records:
        outcome = record.get("outcome", NOVEL)
        if outcome in counts:
            counts[outcome] += 1
        else:
            counts[NOVEL] += 1
        calls += record.get("calls", 0) or 0
        tokens += record.get("tokens", 0) or 0
        passed += 1 if record.get("passed") else 0
    n = len(records)
    return {
        "n": n,
        "outcomes": counts,
        "reuse_rate": (counts[REUSE] + counts[COMPOSE]) / n if n else 0.0,
        "calls_per_task": calls / n if n else 0.0,
        "tokens_per_task": tokens / n if n else 0.0,
        "success_rate": passed / n if n else 0.0,
    }


def compression(tasks_solved: int, capability_count: int) -> dict:
    """Tasks-per-capability compression signal.

    Healthy learning drives this ratio UP (fewer, broader abstractions).
    A ratio near 1.0 with a growing library is memorization, not learning.
    """
    if capability_count <= 0:
        return {"tasks_per_capability": None,
                "warning": tasks_solved > 0}
    ratio = tasks_solved / capability_count
    return {"tasks_per_capability": ratio,
            "warning": bool(ratio < 1.5 and capability_count > 4)}
