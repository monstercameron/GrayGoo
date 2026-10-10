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

    def record_counterexample(self, lesson_id, task_id, note):
        """Append {"task_id", "note} to a stored lesson (memory.md 17)."""
        lesson = self._lessons.get(lesson_id)
        if lesson is None:
            raise KeyError("unknown lesson id %r" % lesson_id)
        if not task_id or not isinstance(task_id, str):
            raise ValueError("task_id must be a non-empty string, got %r"
                             % (task_id,))
        if not note or not isinstance(note, str):
            raise ValueError("note must be a non-empty string, got %r"
                             % (note,))
        lesson["counterexamples"].append({"task_id": task_id, "note": note})
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
    """Return 14 manually seeded Family A lessons (todos.md L2).

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
        make_lesson(
            "log-field-structure-first", "repair",
            "When parsing log lines, match structural fields — quoted "
            "strings, bracket groups, exact field positions — instead of "
            "splitting on whitespace or matching substrings.",
            when="parsing log or line-oriented inputs where fields may "
                 "contain spaces, quotes, or variable-width text",
            when_not="clean single-token-per-field records with no "
                     "quoting or embedded spaces",
            evidence=[{"task_id": "A-EXP-13",
                       "note": "word-split broke the quoted request "
                               "field; structural regex passed"},
                      {"task_id": "A-EXP-17",
                       "note": "substring match caught message text; "
                               "exact field match passed"}],
            confidence=0.8,
            created_generation=6, last_validated=6, status="active",
            tags=["logs", "parsing", "fields", "family-a"],
            failure_classes=["wrong-output", "edge-case"],
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

    def __len__(self):
        return len(self._lessons)

    def __iter__(self):
        """Iterate the registered lessons (what :func:`precision` aggregates)."""
        return iter(self._lessons.values())

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

    def record_usage(self, lesson_id, success=None):
        """Record one retrieval; *success* True/False/None updates counters.

        Mirrors :meth:`LessonStore.record_usage` for the in-memory
        registry (issues.md #89: per-task lesson outcomes in the
        lesson-key experiment).
        """
        lesson = self._lessons.get(lesson_id)
        if lesson is None:
            raise KeyError("unknown lesson id %r" % lesson_id)
        lesson["usage"]["retrievals"] += 1
        if success is True:
            lesson["usage"]["successes"] += 1
        elif success is False:
            lesson["usage"]["failures"] += 1
        return lesson["usage"]

    def record_counterexample(self, lesson_id, task_id, note):
        """Append {"task_id", "note} to a registered lesson (memory.md 17).

        Mirrors :meth:`LessonStore.record_counterexample` for the
        in-memory registry (issues.md #89).
        """
        lesson = self._lessons.get(lesson_id)
        if lesson is None:
            raise KeyError("unknown lesson id %r" % lesson_id)
        if not task_id or not isinstance(task_id, str):
            raise ValueError("task_id must be a non-empty string, got %r"
                             % (task_id,))
        if not note or not isinstance(note, str):
            raise ValueError("note must be a non-empty string, got %r"
                             % (note,))
        lesson["counterexamples"].append({"task_id": task_id, "note": note})
        return lesson["counterexamples"]


#: Factor applied by :func:`decay_confidence` when the runtime or model
#: version differs from the lesson's last-validated versions.
DECAY_FACTOR = 0.8

#: Counterexample count that triggers applicability narrowing in
#: :func:`refine_or_deprecate`.
COUNTEREXAMPLE_REFINE_THRESHOLD = 1

#: Counterexample count that triggers deprecation in
#: :func:`refine_or_deprecate`.
COUNTEREXAMPLE_DEPRECATE_THRESHOLD = 3


def _stored_versions(lesson):
    """Return (runtime, model) versions recorded on *lesson* (or Nones)."""
    runtime = lesson.get("runtime_version")
    if runtime is None:
        runtime = lesson.get("last_validated_runtime")
    model = lesson.get("model_version")
    if model is None:
        model = lesson.get("last_validated_model")
    validated = lesson.get("validated_versions")
    if isinstance(validated, dict):
        if runtime is None:
            runtime = validated.get("runtime",
                                    validated.get("runtime_version"))
        if model is None:
            model = validated.get("model",
                                  validated.get("model_version"))
    return runtime, model


def decay_confidence(lesson, *, runtime_version, model_version):
    """Decay *lesson* confidence when versions changed (memory.md 16).

    Compares *runtime_version*/*model_version* against the versions
    recorded on the lesson (``runtime_version``/``model_version``,
    with ``last_validated_runtime``/``last_validated_model`` and
    ``validated_versions`` accepted as aliases). First call on a
    lesson with no recorded versions establishes the baseline with
    no decay. A mismatch multiplies confidence by :data:`DECAY_FACTOR`
    (rounded to 4 decimals, like :meth:`LessonStore.decay_confidence`),
    records the new versions, and sets ``needs_revalidation`` True.
    A match leaves confidence untouched and preserves any pending
    ``needs_revalidation`` flag (missing flag defaults to False).

    Mutates *lesson* in place and returns it.
    """
    if not isinstance(lesson, dict):
        raise TypeError("lesson must be a dict, got %s"
                        % type(lesson).__name__)
    confidence = lesson.get("confidence")
    if not _is_number(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError("lesson confidence must be a number in [0, 1], "
                         "got %r" % (confidence,))
    for name, value in (("runtime_version", runtime_version),
                        ("model_version", model_version)):
        if not value or not isinstance(value, str):
            raise ValueError("%s must be a non-empty string, got %r"
                             % (name, value))
    stored_runtime, stored_model = _stored_versions(lesson)
    has_any = (
        "runtime_version" in lesson or "model_version" in lesson
        or "last_validated_runtime" in lesson
        or "last_validated_model" in lesson
        or isinstance(lesson.get("validated_versions"), dict)
    )
    if not has_any:
        lesson["runtime_version"] = runtime_version
        lesson["model_version"] = model_version
        lesson.setdefault("needs_revalidation", False)
        return lesson
    if stored_runtime == runtime_version and stored_model == model_version:
        lesson.setdefault("runtime_version", runtime_version)
        lesson.setdefault("model_version", model_version)
        lesson.setdefault("needs_revalidation", False)
        return lesson
    lesson["confidence"] = round(float(confidence) * DECAY_FACTOR, 4)
    lesson["runtime_version"] = runtime_version
    lesson["model_version"] = model_version
    if "last_validated_runtime" in lesson:
        lesson["last_validated_runtime"] = runtime_version
    if "last_validated_model" in lesson:
        lesson["last_validated_model"] = model_version
    validated = lesson.get("validated_versions")
    if isinstance(validated, dict):
        validated["runtime"] = runtime_version
        validated["model"] = model_version
    lesson["needs_revalidation"] = True
    return lesson


def record_counterexample(lesson_id, task_id, note):
    """Record a counterexample (memory.md 17).

    Dual form: when *lesson_id* is a lesson dict, append
    ``{"task_id": task_id, "note": note}`` to its ``counterexamples``
    list (mutating in place) and return that list. When *lesson_id*
    is a lesson-id string, no store is available at module level, so
    return the standalone record ``{"lesson_id": ..., "task_id": ...,
    "note": ...}`` for the caller to persist (see
    :meth:`LessonStore.record_counterexample` for the storing form).
    """
    if not task_id or not isinstance(task_id, str):
        raise ValueError("task_id must be a non-empty string, got %r"
                         % (task_id,))
    if not note or not isinstance(note, str):
        raise ValueError("note must be a non-empty string, got %r"
                         % (note,))
    if isinstance(lesson_id, dict):
        counterexamples = lesson_id.get("counterexamples")
        if counterexamples is None:
            counterexamples = lesson_id["counterexamples"] = []
        if not isinstance(counterexamples, list):
            raise ValueError("lesson 'counterexamples' must be a list")
        counterexamples.append({"task_id": task_id, "note": note})
        return counterexamples
    if not lesson_id or not isinstance(lesson_id, str):
        raise TypeError("lesson_id must be a lesson dict or a non-empty "
                        "lesson-id string, got %r" % (lesson_id,))
    return {"lesson_id": lesson_id, "task_id": task_id, "note": note}


def refine_or_deprecate(lesson):
    """Refine or deprecate *lesson* from its counterexamples (memory.md 17).

    With at least :data:`COUNTEREXAMPLE_DEPRECATE_THRESHOLD`
    counterexamples, set ``status`` to ``"deprecated"``. With at
    least :data:`COUNTEREXAMPLE_REFINE_THRESHOLD` (but below the
    deprecate threshold), narrow applicability by appending the
    latest counterexample as an exception to both ``applies_when``
    ``when`` and ``when_not`` (idempotent: re-running without a new
    counterexample changes nothing). Below the refine threshold the
    lesson is returned unchanged. Mutates in place and returns it.
    """
    if not isinstance(lesson, dict):
        raise TypeError("lesson must be a dict, got %s"
                        % type(lesson).__name__)
    counterexamples = lesson.get("counterexamples", [])
    if not isinstance(counterexamples, list):
        raise ValueError("lesson 'counterexamples' must be a list")
    count = len(counterexamples)
    if count >= COUNTEREXAMPLE_DEPRECATE_THRESHOLD:
        lesson["status"] = "deprecated"
        return lesson
    if count < COUNTEREXAMPLE_REFINE_THRESHOLD:
        return lesson
    applies = lesson.get("applies_when")
    if not isinstance(applies, dict):
        raise ValueError("lesson requires 'applies_when' dict to refine")
    when = applies.get("when")
    when_not = applies.get("when_not")
    if not when or not isinstance(when, str):
        raise ValueError("lesson 'applies_when.when' must be a non-empty "
                         "string to refine")
    if not when_not or not isinstance(when_not, str):
        raise ValueError("lesson 'applies_when.when_not' must be a "
                         "non-empty string to refine")
    latest = counterexamples[-1]
    if isinstance(latest, dict):
        note = str(latest.get("note", "")).strip()
        task = str(latest.get("task_id", "")).strip()
    else:
        note = str(latest).strip()
        task = ""
    detail = note or task or "reported failure"
    if task and note and task not in note:
        detail = "%s (cf. %s)" % (note, task)
    when_suffix = " (except: %s)" % detail
    if when_suffix not in when:
        when = when.rstrip() + when_suffix
    not_suffix = "; except where %s" % detail
    if not_suffix not in when_not:
        when_not = when_not.rstrip() + not_suffix
    lesson["applies_when"] = {"when": when, "when_not": when_not}
    return lesson


def precision(source):
    """Aggregate retrieval precision over lesson usage (issues.md #97).

    *source* is a :class:`LessonStore`, :class:`LessonRegistry`, or an
    iterable of lesson dicts. Returns ``{"retrievals", "successes",
    "failures", "precision", "per_lesson"}`` where ``precision`` is
    successes/retrievals rounded to 4 decimals, or None when nothing
    was ever retrieved. ``per_lesson`` maps lesson id to its own
    (retrievals, successes, precision-or-None) triple. Lessons without
    a ``usage`` dict count as never retrieved.
    """
    if isinstance(source, (LessonStore, LessonRegistry)):
        items = list(source)
    else:
        items = list(source)
    total_r = total_s = total_f = 0
    per_lesson = {}
    for lesson in items:
        usage = lesson.get("usage") or {}
        got = int(usage.get("retrievals", 0) or 0)
        won = int(usage.get("successes", 0) or 0)
        lost = int(usage.get("failures", 0) or 0)
        total_r += got
        total_s += won
        total_f += lost
        per_lesson[lesson.get("id", "?")] = {
            "retrievals": got,
            "successes": won,
            "precision": (round(won / got, 4) if got else None),
        }
    return {
        "retrievals": total_r,
        "successes": total_s,
        "failures": total_f,
        "precision": (round(total_s / total_r, 4) if total_r else None),
        "per_lesson": per_lesson,
    }
