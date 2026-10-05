"""Lesson mining: cluster experience, propose candidates (learning L3).

Implements memory.md sections 4-5 and 35 (phase L3), plus the
todos.md "Mine lessons automatically (learning L3)" items.

The mining loop clusters recurring failures (and successful repairs)
from ledger events, builds evidenced candidate lessons, and
deduplicates near-identical candidates. Candidate statements are
deterministic templates; a future step may ask the model to rephrase
them (the ``statement`` override on :func:`propose_lesson_candidate`
is the hook).

Stdlib only.
"""

import json
import string

#: Jaccard similarity at or above which two candidates merge.
DEFAULT_DEDUP_THRESHOLD = 0.8


def _payload_dict(event):
    payload = event.get("payload", {})
    if isinstance(payload, dict):
        return payload
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _failure_class_of(payload):
    failure = payload.get("failure")
    if isinstance(failure, dict) and failure.get("class"):
        return failure["class"]
    if isinstance(failure, str) and failure:
        return failure
    for key in ("failure_class", "class"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def cluster_failures(trajectory_events):
    """Group failure-bearing events by (failure class, family).

    *trajectory_events* is an iterable of ledger event dicts (as
    returned by the ledger getters). Events without a recognizable
    failure class are skipped. Returns cluster dicts with
    ``failure_class``, ``family``, ``events``, ``event_ids``,
    ``task_ids``, and ``count``, sorted by descending count then
    class/family for determinism.
    """
    groups = {}
    for event in trajectory_events:
        payload = _payload_dict(event)
        failure_class = _failure_class_of(payload)
        if not failure_class:
            continue
        family = payload.get("family") or "unknown"
        key = (failure_class, family)
        cluster = groups.setdefault(key, {
            "failure_class": failure_class,
            "family": family,
            "events": [],
            "event_ids": [],
            "task_ids": [],
        })
        cluster["events"].append(event)
        cluster["event_ids"].append(event.get("event_id"))
        task_id = event.get("task_id")
        if task_id not in cluster["task_ids"]:
            cluster["task_ids"].append(task_id)
    clusters = []
    for cluster in groups.values():
        cluster["task_ids"] = sorted(t for t in cluster["task_ids"]
                                     if t is not None)
        cluster["count"] = len(cluster["events"])
        clusters.append(cluster)
    clusters.sort(key=lambda c: (-c["count"], c["failure_class"], c["family"]))
    return clusters


def cluster_repairs(events):
    """Group repair-outcome events by (repair kind, failure class).

    Returns cluster dicts with ``repair_kind``, ``failure_class``,
    ``successes``, ``failures``, ``event_ids``, ``task_ids``, and
    ``count``. Events without a repair kind are skipped.
    """
    groups = {}
    for event in events:
        payload = _payload_dict(event)
        kind = payload.get("repair_kind") or payload.get("repair")
        if isinstance(kind, dict):
            kind = kind.get("kind")
        if not kind or not isinstance(kind, str):
            continue
        failure_class = _failure_class_of(payload)
        key = (kind, failure_class)
        cluster = groups.setdefault(key, {
            "repair_kind": kind,
            "failure_class": failure_class,
            "successes": 0,
            "failures": 0,
            "event_ids": [],
            "task_ids": [],
        })
        if payload.get("success"):
            cluster["successes"] += 1
        else:
            cluster["failures"] += 1
        cluster["event_ids"].append(event.get("event_id"))
        task_id = event.get("task_id")
        if task_id is not None and task_id not in cluster["task_ids"]:
            cluster["task_ids"].append(task_id)
    clusters = []
    for cluster in groups.values():
        cluster["task_ids"] = sorted(cluster["task_ids"])
        cluster["count"] = cluster["successes"] + cluster["failures"]
        clusters.append(cluster)
    clusters.sort(key=lambda c: (-c["count"], c["repair_kind"],
                                 c["failure_class"] or ""))
    return clusters


def propose_lesson_candidate(cluster, statement=None,
                             lesson_class="failure"):
    """Build an evidenced candidate-lesson dict from one cluster.

    *cluster* is a :func:`cluster_failures` (or :func:`cluster_repairs`)
    entry. Every candidate carries its source evidence (event ids,
    task ids, occurrence count) per todos.md L3. *statement* overrides
    the default template (the hook for a future model phrasing step).
    """
    if not isinstance(cluster, dict):
        raise TypeError("cluster must be a dict, got %s"
                        % type(cluster).__name__)
    count = cluster.get("count", len(cluster.get("events", [])))
    if count is not None and count <= 0:
        raise ValueError("cannot propose a lesson from an empty cluster")
    failure_class = cluster.get("failure_class")
    family = cluster.get("family")
    task_ids = list(cluster.get("task_ids", []))
    event_ids = list(cluster.get("event_ids", []))
    if statement is None:
        statement = (
            "Recurring %s failures in %s tasks (%d occurrence(s) across "
            "%d task(s)): add a targeted check for this failure mode "
            "before synthesis."
            % (failure_class or "unknown",
               ("family '%s'" % family) if family else "unknown family",
               count or 0, len(task_ids)))
    if not statement or not isinstance(statement, str):
        raise ValueError("candidate statement must be a non-empty string")
    when = "tasks in family '%s' showing failure class '%s'" % (
        family or "unknown", failure_class or "unknown")
    return {
        "statement": statement,
        "lesson_class": lesson_class,
        "failure_class": failure_class,
        "family": family,
        "evidence": {
            "event_ids": event_ids,
            "task_ids": task_ids,
            "count": count or 0,
        },
        "applies_when": {
            "when": when,
            "when_not": "tasks outside that family or with unrelated "
                        "failure classes",
        },
        "confidence": min(0.85, 0.5 + 0.05 * (count or 0)),
        "status": "candidate",
    }


def _tokens(text):
    cleaned = text.lower()
    cleaned = cleaned.translate(str.maketrans("", "", string.punctuation))
    return set(cleaned.split())


def statement_similarity(first, second):
    """Jaccard similarity of two statements' normalized token sets."""
    a, b = _tokens(first), _tokens(second)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _merge_evidence(kept, other):
    kept_ev = kept.get("evidence")
    other_ev = other.get("evidence")
    if isinstance(kept_ev, dict) and isinstance(other_ev, dict):
        merged = dict(kept_ev)
        for key in ("event_ids", "task_ids"):
            seen = list(merged.get(key, []))
            for item in other_ev.get(key, []):
                if item not in seen:
                    seen.append(item)
            merged[key] = seen
        if isinstance(merged.get("count"), (int, float)) and isinstance(
                other_ev.get("count"), (int, float)):
            merged["count"] = merged["count"] + other_ev["count"]
        kept["evidence"] = merged
    elif isinstance(kept_ev, list) and isinstance(other_ev, list):
        kept["evidence"] = kept_ev + [e for e in other_ev
                                      if e not in kept_ev]
    return kept


def deduplicate(candidates, threshold=DEFAULT_DEDUP_THRESHOLD):
    """Merge near-identical candidates by statement similarity.

    Candidates whose statements score >= *threshold* via
    :func:`statement_similarity` merge into the first-seen candidate
    with unioned evidence. Returns a new list; inputs are not mutated.
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be in [0, 1], got %r" % (threshold,))
    deduped = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or "statement" not in candidate:
            raise ValueError("each candidate must be a dict with a "
                             "'statement' key")
        merged = False
        for kept in deduped:
            if statement_similarity(kept["statement"],
                                    candidate["statement"]) >= threshold:
                _merge_evidence(kept, candidate)
                merged = True
                break
        if not merged:
            clone = dict(candidate)
            evidence = candidate.get("evidence")
            if isinstance(evidence, dict):
                clone_evidence = {}
                for key, value in evidence.items():
                    clone_evidence[key] = (list(value)
                                           if isinstance(value, list)
                                           else value)
                clone["evidence"] = clone_evidence
            elif isinstance(evidence, list):
                clone["evidence"] = list(evidence)
            deduped.append(clone)
    return deduped
