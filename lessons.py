"""Lesson schema, store, seeds, and retrieval (learning L2).

Implements memory.md sections 1, 3, 7-8, 13-15, 20 and 35 (phase L2),
plus the todos.md "Seed manual lessons (learning L2)" items.

Stdlib only.
"""

import json

LESSON_CLASSES = (
    "failure",
    "repair",
    "design",
    "runtime-specific",
    "model-behavior",
)
"""Five lesson classes (memory.md section 1)."""

LESSON_CLASS_SET = frozenset(LESSON_CLASSES)

LESSON_STATUSES = (
    "candidate",
    "active",
    "rejected",
    "deprecated",
    "generalized",
)
"""Lesson lifecycle states (memory.md section 19)."""

LESSON_STATUS_SET = frozenset(LESSON_STATUSES)

LESSON_KINDS = ("fact", "heuristic")
"""Facts are verified; heuristics need ongoing evidence (memory.md 20)."""

LESSON_KIND_SET = frozenset(LESSON_KINDS)

#: Hard cap on lessons returned per retrieval (memory.md section 14).
MAX_LESSONS_PER_CALL = 5


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_lesson(lesson):
    """Validate a lesson dict against the schema; return a normalized copy.

    Required: ``id`` (non-empty str), ``class`` (one of
    :data:`LESSON_CLASSES`), ``statement`` (non-empty str),
    ``applies_when`` (dict with non-empty ``when`` and ``when_not``
    strings), ``confidence`` (number in [0, 1]).

    Optional (defaults filled in): ``evidence`` (list),
    ``counterexamples`` (list), ``impact`` (dict of numbers),
    ``created_generation`` (int >= 0), ``last_validated`` (int >= 0 or
    None), ``usage`` (dict with ``retrievals``/``successes``/``failures``
    counts), ``status`` (lifecycle state), ``kind``
    (``fact``/``heuristic``), ``tags`` (list of str),
    ``failure_classes`` (list of str).

    Raises :exc:`TypeError` for a non-dict lesson and :exc:`ValueError`
    for any schema violation.
    """
    if not isinstance(lesson, dict):
        raise TypeError("lesson must be a dict, got %s"
                        % type(lesson).__name__)
    normalized = dict(lesson)

    lesson_id = lesson.get("id")
    if not lesson_id or not isinstance(lesson_id, str):
        raise ValueError("lesson requires a non-empty string 'id'")
    normalized["id"] = lesson_id

    lesson_class = lesson.get("class")
    if lesson_class not in LESSON_CLASS_SET:
        raise ValueError("lesson %r: 'class' must be one of %s, got %r"
                         % (lesson_id, list(LESSON_CLASSES), lesson_class))
    normalized["class"] = lesson_class

    statement = lesson.get("statement")
    if not statement or not isinstance(statement, str):
        raise ValueError("lesson %r: 'statement' must be a non-empty "
                         "string" % lesson_id)

    applies = lesson.get("applies_when")
    if not isinstance(applies, dict):
        raise ValueError("lesson %r: 'applies_when' must be a dict with "
                         "'when'/'when_not'" % lesson_id)
    when = applies.get("when")
    when_not = applies.get("when_not")
    if not when or not isinstance(when, str):
        raise ValueError("lesson %r: 'applies_when.when' must be a "
                         "non-empty string" % lesson_id)
    if not when_not or not isinstance(when_not, str):
        raise ValueError("lesson %r: 'applies_when.when_not' must be a "
                         "non-empty string" % lesson_id)
    normalized["applies_when"] = {"when": when, "when_not": when_not}

    confidence = lesson.get("confidence")
    if not _is_number(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError("lesson %r: 'confidence' must be a number in "
                         "[0, 1], got %r" % (lesson_id, confidence))
    normalized["confidence"] = float(confidence)

    for key in ("evidence", "counterexamples"):
        value = lesson.get(key, [])
        if not isinstance(value, list):
            raise ValueError("lesson %r: %r must be a list" % (lesson_id, key))
        normalized[key] = list(value)

    impact = lesson.get("impact", {})
    if not isinstance(impact, dict):
        raise ValueError("lesson %r: 'impact' must be a dict" % lesson_id)
    for metric, delta in impact.items():
        if not _is_number(delta):
            raise ValueError("lesson %r: impact %r must be numeric, got %r"
                             % (lesson_id, metric, delta))
    normalized["impact"] = dict(impact)

    created = lesson.get("created_generation", 0)
    if isinstance(created, bool) or not isinstance(created, int) \
            or created < 0:
        raise ValueError("lesson %r: 'created_generation' must be an int "
                         ">= 0" % lesson_id)
    normalized["created_generation"] = created

    validated = lesson.get("last_validated")
    if validated is not None and (isinstance(validated, bool)
                                  or not isinstance(validated, int)
                                  or validated < 0):
        raise ValueError("lesson %r: 'last_validated' must be an int >= 0 "
                         "or None" % lesson_id)
    normalized["last_validated"] = validated

    usage = lesson.get("usage", {})
    if not isinstance(usage, dict):
        raise ValueError("lesson %r: 'usage' must be a dict" % lesson_id)
    normalized_usage = {}
    for key in ("retrievals", "successes", "failures"):
        count = usage.get(key, 0)
        if isinstance(count, bool) or not isinstance(count, int) \
                or count < 0:
            raise ValueError("lesson %r: usage %r must be an int >= 0"
                             % (lesson_id, key))
        normalized_usage[key] = count
    normalized["usage"] = normalized_usage

    status = lesson.get("status", "candidate")
    if status not in LESSON_STATUS_SET:
        raise ValueError("lesson %r: 'status' must be one of %s, got %r"
                         % (lesson_id, list(LESSON_STATUSES), status))
    normalized["status"] = status

    kind = lesson.get("kind", "heuristic")
    if kind not in LESSON_KIND_SET:
        raise ValueError("lesson %r: 'kind' must be one of %s, got %r"
                         % (lesson_id, list(LESSON_KINDS), kind))
    normalized["kind"] = kind

    for key in ("tags", "failure_classes"):
        value = lesson.get(key, [])
        if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value):
            raise ValueError("lesson %r: %r must be a list of strings"
                             % (lesson_id, key))
        normalized[key] = list(value)

    return normalized


def make_lesson(lesson_id, lesson_class, statement, when, when_not,
                evidence=None, counterexamples=None, confidence=0.5,
                impact=None, created_generation=0, last_validated=None,
                usage=None, status="candidate", kind="heuristic", tags=None,
                failure_classes=None):
    """Build and validate one lesson dict from keyword-style arguments."""
    return validate_lesson({
        "id": lesson_id,
        "class": lesson_class,
        "statement": statement,
        "applies_when": {"when": when, "when_not": when_not},
        "evidence": evidence or [],
        "counterexamples": counterexamples or [],
        "confidence": confidence,
        "impact": impact or {},
        "created_generation": created_generation,
        "last_validated": last_validated,
        "usage": usage or {},
        "status": status,
        "kind": kind,
        "tags": tags or [],
        "failure_classes": failure_classes or [],
    })


class LessonStore:
    """JSON-backed persistent store for lessons."""

    def __init__(self, path=None, lessons=None):
        self.path = path
        self._lessons = {}
        for lesson in lessons or []:
            self.add(lesson)

    def __len__(self):
        return len(self._lessons)

    def __iter__(self):
        return iter(self._lessons.values())

    def add(self, lesson):
        """Validate and store one lesson; duplicate ids are rejected."""
        normalized = validate_lesson(lesson)
        if normalized["id"] in self._lessons:
            raise ValueError("duplicate lesson id %r" % normalized["id"])
        self._lessons[normalized["id"]] = normalized
        return normalized

    def get(self, lesson_id):
        """Return the lesson with *lesson_id*, or None."""
        return self._lessons.get(lesson_id)

    def list(self, status=None, lesson_class=None):
        """List lessons, optionally filtered by status and/or class."""
        lessons = list(self._lessons.values())
        if status is not None:
            lessons = [l for l in lessons if l["status"] == status]
        if lesson_class is not None:
            lessons = [l for l in lessons if l["class"] == lesson_class]
        return lessons

    def record_usage(self, lesson_id, success=None):
        """Record one retrieval; *success* True/False/None updates counters."""
        lesson = self._lessons.get(lesson_id)
        if lesson is None:
            raise KeyError("unknown lesson id %r" % lesson_id)
        lesson["usage"]["retrievals"] += 1
        if success is True:
            lesson["usage"]["successes"] += 1
        elif success is False:
            lesson["usage"]["failures"] += 1
        return lesson["usage"]

    def add_counterexample(self, lesson_id, counterexample):
        """Append one counterexample record to a lesson (memory.md 17)."""
        lesson = self._lessons.get(lesson_id)
        if lesson is None:
            raise KeyError("unknown lesson id %r" % lesson_id)
        lesson["counterexamples"].append(counterexample)
        return lesson["counterexamples"]

    def decay_confidence(self, factor):
        """Multiply every active/candidate confidence by *factor*.

        Used after runtime/model upgrades (todos.md L4: decay and
        revalidate). *factor* must be in [0, 1].
        """
        if not _is_number(factor) or not 0.0 <= factor <= 1.0:
            raise ValueError("decay factor must be a number in [0, 1], "
                             "got %r" % (factor,))
        for lesson in self._lessons.values():
            if lesson["status"] in ("active", "candidate"):
                lesson["confidence"] = round(lesson["confidence"] * factor, 4)
        return self

    def save(self, path=None):
        """Write all lessons as JSON to *path* (default: store path)."""
        target = path or self.path
        if not target:
            raise ValueError("LessonStore.save(): no path given")
        with open(target, "w", encoding="utf-8") as handle:
            json.dump({"lessons": list(self._lessons.values())}, handle,
                      indent=2, sort_keys=True)
        self.path = target
        return target

    @classmethod
    def load(cls, path):
        """Load a store from a JSON file written by :meth:`save`."""
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        lessons = data.get("lessons", [])
        if not isinstance(lessons, list):
            raise ValueError("lesson file %r: 'lessons' must be a list"
                             % path)
        return cls(path=path, lessons=lessons)


def seed_10_20():
    """Return 13 manually seeded Family A lessons (todos.md L2).

    Covers all five lesson classes with narrow applicability bounds
    (memory.md section 7) and mixed fact/heuristic kinds (section 20).
    """
    return [
        make_lesson(
            "csv-field-escaping-first", "repair",
            "When a CSV fix involves embedded newlines or quotes, inspect "
            "and repair field escaping before changing row splitting.",
            when="repairing CSV/TSV parse failures where fields may "
                 "contain embedded newlines, quotes, or delimiters",
            when_not="record-count mismatches on clean single-line "
                     "records with no quoting involved",
            evidence=[{"task_id": "family-a-014",
                       "note": "row-split fix failed twice; escaping fix "
                               "passed first try"}],
            confidence=0.82,
            impact={"first_pass_success": 0.14, "tokens": -430},
            created_generation=3, last_validated=5, status="active",
            tags=["csv", "parsing", "escaping", "family-a"],
            failure_classes=["wrong-output", "edge-case"],
        ),
        make_lesson(
            "rfc4180-embedded-newlines", "failure",
            "RFC 4180 permits line breaks inside double-quoted fields, so "
            "a newline does not always end a CSV record.",
            when="parsing CSV/TSV-family inputs documented to follow "
                 "RFC 4180 quoting rules",
            when_not="line-oriented formats documented as one record "
                     "per line with no quoting",
            evidence=[{"task_id": "family-a-006",
                       "note": "naive line split broke 4 quoted fixtures"}],
            confidence=0.95,
            impact={"first_pass_success": 0.06, "tokens": -120},
            created_generation=1, last_validated=5, status="active",
            kind="fact",
            tags=["csv", "rfc4180", "quoting", "family-a"],
            failure_classes=["wrong-output"],
        ),
        make_lesson(
            "cursor-cycle-detection", "failure",
            "Pagination implementations consuming opaque cursors must "
            "detect repeated cursors and terminate instead of looping.",
            when="writing or replacing capability code that consumes "
                 "opaque cursor pagination",
            when_not="offset/limit pagination where progress is a "
                     "monotonic integer",
            evidence=[{"task_id": "task-182", "note": "infinite loop on "
                       "repeated cursor"},
                      {"task_id": "task-211", "note": "same failure, "
                       "different provider"}],
            confidence=0.91,
            impact={"first_pass_success": 0.14, "tokens": -430},
            created_generation=2, last_validated=5, status="active",
            tags=["cursor", "pagination", "network", "termination"],
            failure_classes=["timeout", "state-corruption"],
        ),
        make_lesson(
            "chunk-boundary-split-tokens", "failure",
            "Streaming parsers frequently fail at chunk boundaries; "
            "always include split-token test cases with a token divided "
            "across two chunks.",
            when="building or repairing streaming/incremental parsers "
                 "that consume input in pieces",
            when_not="whole-input batch parsers that never observe "
                     "partial input",
            evidence=[{"task_id": "family-a-021",
                       "note": "unicode escape split across chunks"}],
            confidence=0.87,
            impact={"first_pass_success": 0.11, "tokens": -260},
            created_generation=2, last_validated=4, status="active",
            tags=["streaming", "parser", "chunk-boundary", "family-a"],
            failure_classes=["edge-case", "wrong-output"],
        ),
        make_lesson(
            "differential-old-new-first", "repair",
            "For pure transformations, run old-vs-new differential tests "
            "before attempting a repair; the first divergence localizes "
            "the bug.",
            when="repairing pure functions where a previous "
                 "implementation is available for comparison",
            when_not="effectful code, or greenfield synthesis with no "
                     "prior implementation",
            evidence=[{"task_id": "family-a-009",
                       "note": "differential run found off-by-one in "
                               "minutes"}],
            confidence=0.78,
            impact={"first_pass_success": 0.09, "tokens": -510},
            created_generation=3, last_validated=5, status="active",
            tags=["differential-testing", "repair", "pure-functions"],
            failure_classes=["wrong-output"],
        ),
        make_lesson(
            "smallest-repro-first", "repair",
            "Reproduce with the smallest failing input before repairing "
            "a parser; minimized cases cut repair attempts.",
            when="parser or transformation failures observed on large "
                 "or generated inputs",
            when_not="failures that only manifest at scale, such as "
                     "performance or memory faults",
            evidence=[{"task_id": "family-a-017",
                       "note": "20 KB repro minimized to 3 lines; fix "
                               "was one predicate"}],
            confidence=0.74,
            impact={"first_pass_success": 0.08, "tokens": -380},
            created_generation=4, last_validated=5, status="active",
            tags=["debugging", "repro", "parsers", "family-a"],
            failure_classes=["edge-case"],
        ),
        make_lesson(
            "preserve-public-interface", "design",
            "Do not change public function signatures during a local "
            "repair; adapt internals to the existing contract.",
            when="local repairs of capabilities that already have "
                 "callers depending on their interface",
            when_not="tasks that explicitly request an interface or "
                     "contract change",
            evidence=[{"task_id": "family-a-011",
                       "note": "arity change broke 6 callers; internal "
                               "fix passed"}],
            confidence=0.83,
            impact={"first_pass_success": 0.10, "tokens": -290},
            created_generation=3, last_validated=4, status="active",
            tags=["interface", "repair", "scope", "contracts"],
            failure_classes=["type/contract"],
        ),
        make_lesson(
            "composition-before-synthesis", "design",
            "Prefer composing existing capabilities over generating a "
            "new monolithic workflow.",
            when="a task decomposes into steps already covered by "
                 "tested capabilities",
            when_not="latency-critical hot paths where composition "
                     "adds measurable overhead",
            evidence=[{"task_id": "family-a-019",
                       "note": "3-capability composition passed; "
                               "monolith failed twice"}],
            counterexamples=[{"task_id": "family-a-031",
                              "note": "composition added 4x latency on "
                                      "a hot path; monolith won"}],
            confidence=0.71,
            impact={"first_pass_success": 0.12, "tokens": -640},
            created_generation=4, last_validated=5, status="active",
            tags=["composition", "reuse", "capabilities"],
            failure_classes=["negative-transfer"],
        ),
        make_lesson(
            "immutable-intermediates-rollback", "design",
            "For speculative parsing transformations where rollback "
            "matters, prefer immutable intermediate structures unless "
            "allocation cost violates the task budget.",
            when="speculative parsing with backtracking or rollback "
                 "over intermediate results",
            when_not="tight allocation budgets or hot loops where "
                     "copying is measured too slow",
            evidence=[{"task_id": "family-a-023",
                       "note": "mutable buffer corrupted on backtrack; "
                               "persistent vector fixed it"}],
            confidence=0.68,
            impact={"first_pass_success": 0.07, "tokens": -150},
            created_generation=4, last_validated=4, status="active",
            tags=["immutable", "parsing", "rollback", "family-a"],
            failure_classes=["state-corruption"],
        ),
        make_lesson(
            "sbcl-dispatch-cells", "runtime-specific",
            "Do not assume redefining an SBCL function updates "
            "previously inlined callers; route mutable capability calls "
            "through dispatch cells.",
            when="redefining SBCL functions that already have compiled "
                 "callers in the image",
            when_not="fresh definitions with no existing callers, or "
                     "local macros expanded at compile time",
            evidence=[{"task_id": "task-239",
                       "note": "stale inlined caller survived "
                               "redefinition"}],
            confidence=0.88,
            impact={"first_pass_success": 0.05, "tokens": -90},
            created_generation=2, last_validated=5, status="active",
            tags=["sbcl", "dispatch", "redefinition", "lisp"],
            failure_classes=["stale-generation"],
        ),
        make_lesson(
            "single-fence-output", "model-behavior",
            "Return exactly one fenced code block containing the "
            "replacement form and no prose outside it.",
            when="synthesis tasks requiring structured S-expression "
                 "output for automated extraction",
            when_not="multi-artifact tasks that explicitly request "
                     "several separate blocks",
            evidence=[{"task_id": "family-a-002",
                       "note": "prose outside the fence broke the "
                               "extractor twice"}],
            confidence=0.80,
            impact={"first_pass_success": 0.13, "tokens": -210},
            created_generation=1, last_validated=4, status="active",
            tags=["output-format", "s-expression", "fences"],
            failure_classes=["syntax", "tool-misuse"],
        ),
        make_lesson(
            "green-stop", "model-behavior",
            "Once the task contract is satisfied, stop: do not refactor "
            "or touch unrelated behavior.",
            when="any task immediately after the success contract "
                 "verifies green",
            when_not="follow-up tasks that explicitly request cleanup "
                     "or refactoring",
            evidence=[{"task_id": "family-a-013",
                       "note": "post-green refactor reintroduced a "
                               "failure"}],
            confidence=0.86,
            impact={"first_pass_success": 0.04, "tokens": -720},
            created_generation=1, last_validated=5, status="active",
            tags=["green-stop", "refactor", "scope"],
            failure_classes=["over-refactor"],
        ),
        make_lesson(
            "modify-only-x", "model-behavior",
            "After a passing state, constrain edits with an explicit "
            "'modify only X' instruction to prevent over-refactoring.",
            when="repair iterations on an already-passing candidate "
                 "that needs a narrow change",
            when_not="initial synthesis with no working baseline yet",
            evidence=[{"task_id": "family-a-027",
                       "note": "scoped instruction held; unscoped "
                               "repair regressed"}],
            confidence=0.66,
            impact={"first_pass_success": 0.05, "tokens": -340},
            created_generation=5, last_validated=5, status="active",
            tags=["scope", "refactor", "prompt"],
            failure_classes=["over-refactor", "test-overfit"],
        ),
    ]


class LessonRegistry:
    """Ranked lesson retrieval for the context compiler."""

    def __init__(self, lessons=None, store=None):
        self._lessons = {}
        if store is not None:
            for lesson in store:
                self.add(lesson)
        for lesson in lessons or []:
            self.add(lesson)

    def add(self, lesson):
        """Validate and register one lesson (duplicate ids rejected)."""
        normalized = validate_lesson(lesson)
        if normalized["id"] in self._lessons:
            raise ValueError("duplicate lesson id %r" % normalized["id"])
        self._lessons[normalized["id"]] = normalized
        return normalized

    def get(self, lesson_id):
        """Return the lesson with *lesson_id*, or None."""
        return self._lessons.get(lesson_id)

    @staticmethod
    def _score(lesson, tags, failure_class):
        # Failure-class match dominates (memory.md section 13), then tag
        # overlap, then confidence and measured first-pass benefit.
        score = 0.0
        if failure_class and failure_class in lesson["failure_classes"]:
            score += 100.0
        wanted = {t.strip().lower() for t in tags if t.strip()}
        have = {t.strip().lower() for t in lesson["tags"]}
        score += 10.0 * len(wanted & have)
        score += 10.0 * lesson["confidence"]
        score += 50.0 * max(
            0.0, lesson["impact"].get("first_pass_success", 0.0))
        return score

    def retrieve_for_task(self, tags, failure_class=None, k=5):
        """Return up to *k* active lessons ranked for a task.

        *tags* is an iterable of topic strings; *failure_class* (when
        given) ranks exact failure-class matches first. At most
        :data:`MAX_LESSONS_PER_CALL` lessons are returned (memory.md
        section 14).
        """
        if tags is None:
            tags = []
        if isinstance(tags, str):
            tags = [tags]
        tags = list(tags)
        try:
            limit = int(k)
        except (TypeError, ValueError):
            raise ValueError("k must be an integer, got %r" % (k,))
        limit = max(0, min(limit, MAX_LESSONS_PER_CALL))
        if limit == 0:
            return []
        scored = []
        for lesson in self._lessons.values():
            if lesson["status"] != "active":
                continue
            scored.append((self._score(lesson, tags, failure_class),
                           lesson["id"], lesson))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [lesson for _, _, lesson in scored[:limit]]
