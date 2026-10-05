"""Lesson consolidation: merges, principles, playbooks (learning L5).

Implements memory.md sections 18-19 (merge overlapping lessons into
principles), 21-22 (task-family playbooks promoted toward Lisp
workflow specs), and 35 (phase L5), plus the todos.md "Consolidate
lessons into principles and playbooks" items.

Stdlib + lessons + mine only.
"""

import hashlib

import lessons
import mine

#: Default overlap score at/above which two active lessons merge.
DEFAULT_MERGE_THRESHOLD = 0.3

#: Minimum confidence for a lesson to enter a compiled playbook.
MIN_PLAYBOOK_CONFIDENCE = 0.7

#: Overlap weights: statement similarity, shared failure class, tag overlap.
_STATEMENT_WEIGHT = 0.5
_FAILURE_WEIGHT = 0.25
_TAG_WEIGHT = 0.25

PRINCIPLE_KEYS = ("id", "statement", "source_lessons", "scope", "confidence")
"""Exact keys of a generalized PRINCIPLE record."""


def lesson_overlap(first, second):
    """Return the overlap score of two lessons in [0, 1].

    Weighted combination of statement Jaccard similarity (via
    :func:`mine.statement_similarity`), shared failure-class membership,
    and tag Jaccard similarity. Lessons sharing a failure class still
    need some tag/statement evidence to reach
    :data:`DEFAULT_MERGE_THRESHOLD`; near-identical statements merge on
    wording alone.
    """
    similarity = mine.statement_similarity(first.get("statement", ""),
                                           second.get("statement", ""))
    mine_classes = set(first.get("failure_classes", []))
    theirs_classes = set(second.get("failure_classes", []))
    failure = 1.0 if mine_classes & theirs_classes else 0.0
    mine_tags = {t.lower() for t in first.get("tags", [])}
    theirs_tags = {t.lower() for t in second.get("tags", [])}
    union = mine_tags | theirs_tags
    tag = len(mine_tags & theirs_tags) / len(union) if union else 0.0
    return (_STATEMENT_WEIGHT * similarity + _FAILURE_WEIGHT * failure
            + _TAG_WEIGHT * tag)


def _check_threshold(threshold):
    if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not 0.0 <= threshold <= 1.0):
        raise ValueError("threshold must be a number in [0, 1], got %r"
                         % (threshold,))


def merge_lessons(store, threshold=DEFAULT_MERGE_THRESHOLD):
    """Cluster overlapping active lessons; merge each cluster in place.

    Active lessons scoring >= *threshold* via :func:`lesson_overlap`
    are grouped with union-find. Each multi-lesson cluster keeps one
    survivor (highest confidence, lowest id breaks ties) which absorbs
    the union of evidence, counterexamples, tags, and failure classes;
    the narrower duplicates are retired to status ``"generalized"``
    (memory.md section 19). Singletons are untouched.

    Returns merge records ``{"survivor_id", "retired_ids", "size"}``
    sorted by survivor id; empty when nothing overlaps.
    """
    _check_threshold(threshold)
    active = sorted((lesson for lesson in store
                     if lesson.get("status") == "active"),
                    key=lambda item: item["id"])
    parent = {lesson["id"]: lesson["id"] for lesson in active}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for pos, first in enumerate(active):
        for second in active[pos + 1:]:
            if lesson_overlap(first, second) >= threshold:
                parent[find(first["id"])] = find(second["id"])
    groups = {}
    for lesson in active:
        groups.setdefault(find(lesson["id"]), []).append(lesson)
    merges = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda item: (-item["confidence"], item["id"]))
        survivor = members[0]
        for key in ("evidence", "counterexamples"):
            union = list(survivor.get(key, []))
            for member in members[1:]:
                for entry in member.get(key, []):
                    if entry not in union:
                        union.append(entry)
            survivor[key] = union
        survivor["tags"] = sorted({tag for member in members
                                   for tag in member.get("tags", [])})
        survivor["failure_classes"] = sorted(
            {name for member in members
             for name in member.get("failure_classes", [])})
        retired_ids = []
        for member in members[1:]:
            member["status"] = "generalized"
            retired_ids.append(member["id"])
        merges.append({"survivor_id": survivor["id"],
                       "retired_ids": sorted(retired_ids),
                       "size": len(members)})
    merges.sort(key=lambda record: record["survivor_id"])
    return merges


def generalize_to_principle(cluster, principle_id=None, statement=None):
    """Generalize a merged lesson cluster into a PRINCIPLE record.

    *cluster* is a non-empty iterable of lesson dicts (as validated by
    :func:`lessons.validate_lesson`). Returns exactly ``{"id",
    "statement", "source_lessons", "scope", "confidence"}`` where
    ``scope`` carries the survivor's applicability bounds plus the
    union of failure classes/tags, and ``confidence`` is the mean
    member confidence. *principle_id* defaults to a deterministic
    ``P-<hex>`` derived from the sorted source ids (pass an explicit
    id for stable names such as ``P12``); *statement* defaults to the
    survivor's statement and is the hook for a future model rephrasing
    step, mirroring :func:`mine.propose_lesson_candidate`.
    """
    members = list(cluster or [])
    if not members:
        raise ValueError("cannot generalize an empty cluster")
    normalized = [lessons.validate_lesson(dict(member))
                  for member in members]
    normalized.sort(key=lambda item: (-item["confidence"], item["id"]))
    survivor = normalized[0]
    source_ids = sorted(item["id"] for item in normalized)
    if principle_id is None:
        digest = hashlib.sha256(
            "|".join(source_ids).encode("utf-8")).hexdigest()[:6]
        principle_id = "P-" + digest
    if not principle_id or not isinstance(principle_id, str):
        raise ValueError("principle id must be a non-empty string")
    if statement is None:
        statement = survivor["statement"]
    if not statement or not isinstance(statement, str):
        raise ValueError("principle statement must be a non-empty string")
    confidence = round(sum(item["confidence"] for item in normalized)
                       / len(normalized), 4)
    scope = {
        "when": survivor["applies_when"]["when"],
        "when_not": survivor["applies_when"]["when_not"],
        "failure_classes": sorted(
            {name for item in normalized
             for name in item["failure_classes"]}),
        "tags": sorted({tag for item in normalized
                        for tag in item["tags"]}),
    }
    return {"id": principle_id, "statement": statement,
            "source_lessons": source_ids, "scope": scope,
            "confidence": confidence}


def validate_principle(principle):
    """Validate a PRINCIPLE record; return it unchanged.

    Requires the :data:`PRINCIPLE_KEYS` keys with a non-empty string
    id/statement, a non-empty sorted list of source lesson ids, a scope
    dict with ``when``/``when_not`` strings, and a confidence in
    [0, 1]. Raises :exc:`TypeError` for a non-dict and :exc:`ValueError`
    for any violation.
    """
    if not isinstance(principle, dict):
        raise TypeError("principle must be a dict, got %s"
                        % type(principle).__name__)
    missing = [key for key in PRINCIPLE_KEYS if key not in principle]
    if missing:
        raise ValueError("principle missing keys: %s"
                         % ", ".join(missing))
    if (not principle["id"] or not isinstance(principle["id"], str)):
        raise ValueError("principle 'id' must be a non-empty string")
    if (not principle["statement"]
            or not isinstance(principle["statement"], str)):
        raise ValueError("principle 'statement' must be a non-empty string")
    sources = principle["source_lessons"]
    if (not isinstance(sources, list) or not sources
            or not all(isinstance(item, str) and item for item in sources)):
        raise ValueError("principle 'source_lessons' must be a non-empty "
                         "list of ids")
    scope = principle["scope"]
    if not isinstance(scope, dict):
        raise ValueError("principle 'scope' must be a dict")
    for key in ("when", "when_not"):
        if not scope.get(key) or not isinstance(scope[key], str):
            raise ValueError("principle scope %r must be a non-empty "
                             "string" % key)
    confidence = principle["confidence"]
    if (isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= confidence <= 1.0):
        raise ValueError("principle 'confidence' must be a number in "
                         "[0, 1], got %r" % (confidence,))
    return principle


def _normalize_tag(value):
    return value.strip().lower().replace("_", "-")


def compile_playbook(store, family, min_confidence=MIN_PLAYBOOK_CONFIDENCE):
    """Compile active high-confidence lessons for *family* into a playbook.

    A lesson enters the playbook when it is active, has confidence >=
    *min_confidence*, and carries *family* as a tag (case-insensitive;
    memory.md section 21). Steps order by descending confidence with
    ascending id breaks, numbered from 1.

    Returns ``{"title", "steps"}`` with steps of ``{"order",
    "lesson_id", "instruction"}``.
    """
    if not family or not isinstance(family, str):
        raise ValueError("family must be a non-empty string")
    if (isinstance(min_confidence, bool)
            or not isinstance(min_confidence, (int, float))
            or not 0.0 <= min_confidence <= 1.0):
        raise ValueError("min_confidence must be a number in [0, 1], got "
                         "%r" % (min_confidence,))
    wanted = _normalize_tag(family)
    ranked = []
    for lesson in store:
        if lesson.get("status") != "active":
            continue
        if lesson.get("confidence", 0.0) < min_confidence:
            continue
        tags = [_normalize_tag(tag) for tag in lesson.get("tags", [])]
        if wanted not in tags:
            continue
        ranked.append(lesson)
    ranked.sort(key=lambda item: (-item["confidence"], item["id"]))
    steps = [{"order": order, "lesson_id": lesson["id"],
              "instruction": lesson["statement"]}
             for order, lesson in enumerate(ranked, start=1)]
    return {"title": "PLAYBOOK: %s" % family, "steps": steps}


def _lisp_string(text):
    escaped = (text.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r")
               .replace("\t", "\\t"))
    return '"%s"' % escaped


def _lisp_keyword(text):
    slug = "".join(char if char.isalnum() else "-"
                   for char in text.strip().lower()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return ":" + (slug or "general")


def playbook_to_workflow_spec(playbook):
    """Emit a playbook as a Lisp ``run-repair-playbook`` workflow spec.

    Returns a structured, non-executable s-expression string per
    memory.md section 22: ``(run-repair-playbook :family :<slug> :title
    "..." :steps ((:order N :lesson "<id>" :instruction "...") ...))``.
    The family slug derives from the playbook title's ``"PLAYBOOK:
    <family>"`` suffix. Raises :exc:`TypeError` for a non-dict playbook
    and :exc:`ValueError` for malformed titles/steps.
    """
    if not isinstance(playbook, dict):
        raise TypeError("playbook must be a dict, got %s"
                        % type(playbook).__name__)
    title = playbook.get("title")
    steps = playbook.get("steps")
    if not title or not isinstance(title, str):
        raise ValueError("playbook 'title' must be a non-empty string")
    if not isinstance(steps, list):
        raise ValueError("playbook 'steps' must be a list")
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("each playbook step must be a dict")
        order = step.get("order")
        if (isinstance(order, bool) or not isinstance(order, int)
                or order < 1):
            raise ValueError("step 'order' must be a positive int")
        for key in ("lesson_id", "instruction"):
            if not step.get(key) or not isinstance(step[key], str):
                raise ValueError("step %r must be a non-empty string"
                                 % key)
    if ":" in title:
        family = title.split(":", 1)[1].strip() or "general"
    else:
        family = title.strip() or "general"
    lines = ["(run-repair-playbook",
             "  :family %s" % _lisp_keyword(family),
             "  :title %s" % _lisp_string(title)]
    if not steps:
        lines.append("  :steps nil)")
    else:
        lines.append("  :steps (")
        for position, step in enumerate(steps):
            cell = ("(:order %d :lesson %s :instruction %s)"
                    % (step["order"], _lisp_string(step["lesson_id"]),
                       _lisp_string(step["instruction"])))
            if position == len(steps) - 1:
                lines.append("    " + cell + "))")
            else:
                lines.append("    " + cell)
    return "\n".join(lines)
