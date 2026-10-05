"""Semantic procedural memory (plan.md sections 11, 32).

Dual memory: executable code answers "what to run"; a skill family
stores the semantic half -- intent, applicability, abstract procedure,
contracts, known failure modes, links to executable implementations,
and empirical evidence. See also documents/learning-memory.md.

Skill family record shape::

    {
        "family_id": str,
        "intent": str,                       # what this family achieves
        "applicability": {
            "when": [str],                   # task tags this applies to
            "when_not": [str],               # tags that disqualify it
        },
        "abstract_procedure": [str],         # ordered steps, code-free
        "contracts": [str],                  # must-hold properties
        "known_failure_modes": [str],        # observed failure classes
        "implementation_ids": [str],         # links to patch/capability ids
        "evidence": {
            "reuse_count": int,
            "success_count": int,
            "failure_count": int,
            "success_rate": float | None,
        },
        "created_at": float,
        "updated_at": float,
    }

Storage: plain JSON files under ``directory`` (stdlib only).
Outcome events are appended to a JSONL log; counts roll up into the
family record's ``evidence``.

The comparison harness (:func:`compare_conditions`) is a HARNESS ONLY:
it runs a task list under caller-supplied runners (e.g. code-only vs
semantic+code retrieval) and reports the delta table schema. Real
numbers require live runs (todos.md "Run baseline D: dual memory");
the bundled fake runners only prove the harness works.
"""

import hashlib
import json
import os
import time


def _now():
    return time.time()


def _family_filename(family_id):
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_"
                   for c in str(family_id))
    digest = hashlib.sha256(str(family_id).encode("utf-8")).hexdigest()[:12]
    return "%s-%s.json" % (safe[:48] or "family", digest)


def _empty_evidence():
    return {"reuse_count": 0, "success_count": 0, "failure_count": 0,
            "success_rate": None}


def make_family(family_id, intent, when=None, when_not=None,
                abstract_procedure=None, contracts=None,
                known_failure_modes=None, implementation_ids=None,
                evidence=None, created_at=None, updated_at=None, **extra):
    """Build a validated skill-family record dict (not yet persisted)."""
    if not family_id or not str(family_id).strip():
        raise ValueError("family_id is required")
    if not intent or not str(intent).strip():
        raise ValueError("intent is required")
    now = _now()
    family = {
        "family_id": str(family_id),
        "intent": str(intent),
        "applicability": {
            "when": [str(t) for t in (when or [])],
            "when_not": [str(t) for t in (when_not or [])],
        },
        "abstract_procedure": [str(s) for s in (abstract_procedure or [])],
        "contracts": [str(c) for c in (contracts or [])],
        "known_failure_modes": [str(f) for f in
                                (known_failure_modes or [])],
        "implementation_ids": [str(i) for i in (implementation_ids or [])],
        "evidence": dict(evidence) if evidence is not None
        else _empty_evidence(),
        "created_at": float(created_at) if created_at is not None else now,
        "updated_at": float(updated_at) if updated_at is not None else now,
    }
    for key in ("reuse_count", "success_count", "failure_count"):
        family["evidence"].setdefault(key, 0)
    family["evidence"].setdefault("success_rate", None)
    for key, value in extra.items():
        family[key] = value
    return family


def match_applicability(family, task_tags):
    """Score how well ``family`` applies to a task with ``task_tags``.

    ``family`` may be a family record or its ``applicability`` dict.
    Returns a float: positive means applicable (higher is better),
    0.0 means no match, negative means explicitly disqualified by a
    ``when_not`` tag.

    Scoring: +1 per ``when`` tag present in ``task_tags``, -2 per
    ``when_not`` tag present. A family with a non-empty ``when`` list
    and zero overlap scores 0.0 (no match); any ``when_not`` overlap
    forces the score negative.
    """
    if isinstance(family, dict) and "applicability" in family:
        app = family.get("applicability") or {}
    elif isinstance(family, dict):
        app = family
    else:
        raise TypeError("family must be a family record or "
                        "applicability dict")
    when = set(app.get("when") or [])
    when_not = set(app.get("when_not") or [])
    tags = set(task_tags or [])
    if when_not & tags:
        return -2.0 * len(when_not & tags)
    if when and not (when & tags):
        return 0.0
    if not when and not (when_not & tags):
        # No constraints at all: weak universal match.
        return 0.5
    return float(len(when & tags))


class SkillStore:
    """JSON-file backed store of skill families + outcome evidence."""

    def __init__(self, directory):
        self.directory = os.path.abspath(directory)
        os.makedirs(self.directory, exist_ok=True)
        self._families_dir = os.path.join(self.directory, "families")
        os.makedirs(self._families_dir, exist_ok=True)
        self._outcomes_path = os.path.join(self.directory, "outcomes.jsonl")

    # -- internal helpers ------------------------------------------
    def _family_path(self, family_id):
        return os.path.join(self._families_dir,
                            _family_filename(family_id))

    def _write_family(self, family):
        family = dict(family)
        family["updated_at"] = _now()
        path = self._family_path(family["family_id"])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(family, fh, sort_keys=True, default=str)
        os.replace(tmp, path)
        return family

    def _read_family_file(self, path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _iter_family_files(self):
        try:
            names = sorted(os.listdir(self._families_dir))
        except OSError:
            return
        for name in names:
            if name.endswith(".json"):
                yield os.path.join(self._families_dir, name)

    # -- CRUD --------------------------------------------------------
    def save_family(self, family_id=None, intent=None, family=None, **kwargs):
        """Create or replace a skill family; returns the stored record.

        Accepts a full record (``save_family(family={...})``), a record
        as first positional arg, or keyword fields
        (``save_family("id", "intent", when=[...])``).
        """
        if isinstance(family_id, dict) and family is None:
            family = family_id
            family_id = None
        if family is not None:
            record = make_family(
                family.get("family_id", family_id),
                family.get("intent", intent or ""),
                when=(family.get("applicability") or {}).get("when"),
                when_not=(family.get("applicability") or {}).get("when_not"),
                abstract_procedure=family.get("abstract_procedure"),
                contracts=family.get("contracts"),
                known_failure_modes=family.get("known_failure_modes"),
                implementation_ids=family.get("implementation_ids"),
                evidence=family.get("evidence"),
                created_at=family.get("created_at"),
                **{k: v for k, v in family.items()
                   if k not in ("family_id", "intent", "applicability",
                                "abstract_procedure", "contracts",
                                "known_failure_modes", "implementation_ids",
                                "evidence", "created_at", "updated_at")})
        else:
            record = make_family(family_id, intent, **kwargs)
        # Preserve original created_at on overwrite.
        existing = self.get_family(record["family_id"])
        if existing is not None:
            record["created_at"] = existing.get("created_at",
                                                record["created_at"])
        return self._write_family(record)

    store_family = save_family
    add_family = save_family
    create_family = save_family
    save = save_family
    put = save_family

    def get_family(self, family_id):
        """Return the family record, or None when missing."""
        path = self._family_path(family_id)
        if not os.path.exists(path):
            for cand in self._iter_family_files():
                data = self._read_family_file(cand)
                if data and data.get("family_id") == family_id:
                    return data
            return None
        return self._read_family_file(path)

    get = get_family
    load_family = get_family

    def list_families(self):
        """Return all stored families, sorted by family_id."""
        out = []
        for path in self._iter_family_files():
            data = self._read_family_file(path)
            if data is not None:
                out.append(data)
        out.sort(key=lambda f: f.get("family_id", ""))
        return out

    list_all = list_families

    def delete_family(self, family_id):
        """Delete a family; returns True when something was removed."""
        removed = False
        path = self._family_path(family_id)
        if os.path.exists(path):
            try:
                os.remove(path)
                removed = True
            except OSError:
                pass
        for cand in list(self._iter_family_files()):
            data = self._read_family_file(cand)
            if data and data.get("family_id") == family_id:
                try:
                    os.remove(cand)
                    removed = True
                except OSError:
                    pass
        return removed

    delete = delete_family
    remove_family = delete_family

    # -- retrieval ---------------------------------------------------
    def find_for_task(self, task_tags=None, limit=None, **kwargs):
        """Rank families by :func:`match_applicability` (positive only).

        Accepts ``find_for_task(["csv", ...])``,
        ``find_for_task(task={"tags": [...]})``, or
        ``find_for_task(tags=[...])``.
        """
        if isinstance(task_tags, dict):
            task_tags = task_tags.get("tags",
                                      task_tags.get("task_tags"))
        if task_tags is None:
            task_tags = kwargs.get("tags", kwargs.get("task_tags"))
        if isinstance(task_tags, str):
            task_tags = [task_tags]
        scored = []
        for family in self.list_families():
            score = match_applicability(family, task_tags)
            if score > 0:
                scored.append((score, family))
        scored.sort(key=lambda t: t[0], reverse=True)
        families = [f for _, f in scored]
        if limit is not None and limit >= 0:
            families = families[:limit]
        return families

    find_families_for_task = find_for_task
    retrieve = find_for_task

    # -- implementation links ----------------------------------------
    def link_implementation(self, family_id, impl_id):
        """Link a patch/capability id to a family (deduped).

        Returns the updated family record. Raises KeyError when the
        family does not exist.
        """
        family = self.get_family(family_id)
        if family is None:
            raise KeyError("unknown skill family: %r" % (family_id,))
        impl_id = str(impl_id)
        if impl_id not in family.get("implementation_ids", []):
            family.setdefault("implementation_ids", []).append(impl_id)
        return self._write_family(family)

    link = link_implementation
    add_implementation = link_implementation

    def unlink_implementation(self, family_id, impl_id):
        """Remove an implementation link; returns the updated record."""
        family = self.get_family(family_id)
        if family is None:
            raise KeyError("unknown skill family: %r" % (family_id,))
        ids = family.get("implementation_ids", [])
        if str(impl_id) in ids:
            ids.remove(str(impl_id))
        return self._write_family(family)

    unlink = unlink_implementation

    # -- outcomes / evidence ------------------------------------------
    def record_outcome(self, family_id, task_id, worked=False, **extra):
        """Record one reuse outcome and roll counts into ``evidence``.

        ``worked`` accepts ``success``/``succeeded`` aliases via kwargs.
        Returns the stored outcome record. Raises KeyError when the
        family does not exist.
        """
        if isinstance(family_id, dict):
            blob = dict(family_id)
            family_id = blob.pop("family_id", None)
            task_id = blob.pop("task_id", task_id)
            worked = blob.pop("worked", blob.pop(
                "success", blob.pop("succeeded", worked)))
            extra = dict(blob, **extra)
        if "success" in extra:
            worked = extra.pop("success")
        if "succeeded" in extra:
            worked = extra.pop("succeeded")
        family = self.get_family(family_id)
        if family is None:
            raise KeyError("unknown skill family: %r" % (family_id,))
        outcome = {
            "family_id": family_id,
            "task_id": task_id,
            "worked": bool(worked),
            "recorded_at": _now(),
        }
        outcome.update(extra)
        with open(self._outcomes_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(outcome, sort_keys=True, default=str) + "\n")
        evidence = family.setdefault("evidence", _empty_evidence())
        evidence["reuse_count"] = evidence.get("reuse_count", 0) + 1
        if outcome["worked"]:
            evidence["success_count"] = evidence.get("success_count", 0) + 1
        else:
            evidence["failure_count"] = evidence.get("failure_count", 0) + 1
        total = evidence["reuse_count"]
        evidence["success_rate"] = (evidence["success_count"] / total
                                    if total else None)
        self._write_family(family)
        return outcome

    log_outcome = record_outcome
    record_reuse = record_outcome

    def get_outcomes(self, family_id=None):
        """Return recorded outcomes (all, or filtered by family)."""
        outcomes = []
        if not os.path.exists(self._outcomes_path):
            return outcomes
        with open(self._outcomes_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if family_id is not None and row.get("family_id") != family_id:
                    continue
                outcomes.append(row)
        return outcomes

    list_outcomes = get_outcomes


# -- seed families ----------------------------------------------------
# Honest, narrow applicability grounded in Family A reality
# (benchmarks/family-a: dates, csv, records, logs) and plan.md section 11.
SEED_FAMILIES = [
    {
        "family_id": "family-a-csv-parsing",
        "intent": "Parse comma-separated rows into records, honoring "
                  "quoted fields, doubled quotes, and embedded newlines.",
        "when": ["csv", "parsing", "family-a"],
        "when_not": ["semicolon-delimited", "tsv", "fixed-width",
                     "binary", "streaming"],
        "abstract_procedure": [
            "Split input into logical rows respecting quoted newlines.",
            "Split each row on commas outside quotes.",
            "Unescape doubled quotes inside quoted fields.",
            "Trim only unquoted surrounding whitespace.",
            "Reject ragged rows instead of guessing alignment.",
        ],
        "contracts": [
            "Output record count equals logical row count.",
            "Quoted fields preserve embedded newlines verbatim.",
            "A doubled quote inside a quoted field yields one quote.",
        ],
        "known_failure_modes": [
            "ragged-rows",
            "unclosed-quote",
            "mixed-line-endings",
            "null-bytes",
        ],
    },
    {
        "family_id": "resilient-cursor-pagination",
        "intent": "Walk a cursor-paginated REST collection to completion "
                  "with bounded retries (plan.md section 11 pattern).",
        "when": ["rest", "cursor-pagination", "idempotent-get"],
        "when_not": ["offset-pagination", "non-idempotent",
                     "realtime-stream", "graphql"],
        "abstract_procedure": [
            "Request page with current cursor (null cursor starts).",
            "Accumulate returned items.",
            "Inspect next cursor; terminate on null cursor.",
            "Abort on repeated cursor (pagination cycle).",
            "Retry 429/503 with backoff, bounded attempts.",
            "Refresh expired auth token once, then fail loudly.",
        ],
        "contracts": [
            "Terminates: null cursor or repeated cursor always ends walk.",
            "No duplicate accumulation across retries of one page.",
            "Never retries non-idempotent operations.",
        ],
        "known_failure_modes": [
            "pagination-cycle",
            "rate-limit",
            "expired-auth-token",
        ],
    },
    {
        "family_id": "family-a-date-normalization",
        "intent": "Normalize explicit calendar dates (ISO-8601, RFC-2822) "
                  "to a canonical UTC representation.",
        "when": ["dates", "normalization", "family-a"],
        "when_not": ["relative-dates", "weekday-arithmetic",
                     "ambiguous-tz-abbrev", "non-gregorian"],
        "abstract_procedure": [
            "Detect format: ISO-8601 vs RFC-2822 vs other.",
            "Parse with explicit offset; assume UTC only when absent.",
            "Reject ambiguous timezone abbreviations.",
            "Emit canonical UTC ISO-8601.",
        ],
        "contracts": [
            "Round-trip preserves the instant in time.",
            "Missing offset is recorded as assumed-UTC, not silent.",
        ],
        "known_failure_modes": [
            "ambiguous-tz-abbrev",
            "missing-offset",
            "leap-second",
            "two-digit-year",
        ],
    },
]


def seed_default_families(store):
    """Create the three seed families unless they already exist.

    Returns the list of family records (existing ones untouched).
    """
    seeded = []
    for spec in SEED_FAMILIES:
        existing = store.get_family(spec["family_id"])
        if existing is not None:
            seeded.append(existing)
            continue
        seeded.append(store.save_family(
            spec["family_id"], spec["intent"],
            when=spec["when"], when_not=spec["when_not"],
            abstract_procedure=spec["abstract_procedure"],
            contracts=spec["contracts"],
            known_failure_modes=spec["known_failure_modes"]))
    return seeded


# -- comparison harness -------------------------------------------------
# HARNESS ONLY. Real code-only vs semantic+code numbers require live
# runs (todos.md "Run baseline D: dual memory"). These helpers run
# caller-supplied runners over a task list and shape the delta table;
# they prove the harness, not the hypothesis.

def make_fake_runner(results, default=None):
    """Build a fake runner callable for harness smoke tests.

    ``results`` maps task id -> result dict
    (``success``/``tokens``/``latency_ms``/``calls``). ``default`` is
    returned for unmapped tasks.
    """
    default = {"success": False, "tokens": 0, "latency_ms": 0.0,
               "calls": 0} if default is None else dict(default)

    def run(task):
        task_id = task.get("id", task.get("task_id"))
        row = dict(results.get(task_id, default))
        return {
            "success": bool(row.get("success", False)),
            "tokens": int(row.get("tokens", 0)),
            "latency_ms": float(row.get("latency_ms", 0.0)),
            "calls": int(row.get("calls", 0)),
        }

    return run


def _task_id(task):
    if isinstance(task, dict):
        return task.get("id", task.get("task_id"))
    return str(task)


def compare_conditions(tasks, memory_modes, runners=None,
                       baseline=None):
    """Run ``tasks`` under each memory-mode runner; report delta schema.

    ``memory_modes`` is an ordered list of mode names (e.g.
    ``["code-only", "semantic+code"]``) with callables in ``runners``,
    or directly a dict of name -> runner callable. Each runner maps a
    task dict to ``{"success", "tokens", "latency_ms", "calls"}``.

    Returns::

        {
          "per_task": [{"task_id", "mode", "success", "tokens",
                        "latency_ms", "calls"}],
          "summary": {mode: {"tasks", "successes", "success_rate",
                             "tokens_total", "tokens_per_task",
                             "latency_ms_total", "latency_ms_per_task",
                             "calls_total", "calls_per_task"}},
          "delta": [{"mode", "baseline", "tasks",
                     "success_rate_delta",
                     "tokens_per_task_delta",
                     "latency_ms_per_task_delta",
                     "calls_per_task_delta"}],
        }

    Deltas are mode-minus-baseline (negative token/latency deltas mean
    savings). The baseline defaults to ``"code-only"`` when present,
    else the first mode. This proves the harness shape only; live
    numbers remain an open experiment.
    """
    if isinstance(memory_modes, dict) and runners is None:
        runners = memory_modes
        memory_modes = list(runners.keys())
    if runners is None:
        raise ValueError("runners is required when memory_modes is a list")
    modes = list(memory_modes)
    missing = [m for m in modes if m not in runners]
    if missing:
        raise ValueError("no runner for mode(s): %s"
                         % ", ".join(missing))
    tasks = list(tasks or [])
    if baseline is None:
        baseline = "code-only" if "code-only" in modes else modes[0]

    per_task = []
    summary = {}
    for mode in modes:
        runner = runners[mode]
        successes = 0
        tokens_total = 0
        latency_total = 0.0
        calls_total = 0
        for task in tasks:
            row = runner(task) or {}
            success = bool(row.get("success", False))
            tokens = int(row.get("tokens", 0) or 0)
            latency = float(row.get("latency_ms", 0.0) or 0.0)
            calls = int(row.get("calls", 0) or 0)
            successes += 1 if success else 0
            tokens_total += tokens
            latency_total += latency
            calls_total += calls
            per_task.append({"task_id": _task_id(task), "mode": mode,
                             "success": success, "tokens": tokens,
                             "latency_ms": latency, "calls": calls})
        n = len(tasks)
        summary[mode] = {
            "tasks": n,
            "successes": successes,
            "success_rate": (successes / n) if n else 0.0,
            "tokens_total": tokens_total,
            "tokens_per_task": (tokens_total / n) if n else 0.0,
            "latency_ms_total": latency_total,
            "latency_ms_per_task": (latency_total / n) if n else 0.0,
            "calls_total": calls_total,
            "calls_per_task": (calls_total / n) if n else 0.0,
        }

    base = summary[baseline]
    delta = []
    for mode in modes:
        if mode == baseline:
            continue
        cur = summary[mode]
        delta.append({
            "mode": mode,
            "baseline": baseline,
            "tasks": cur["tasks"],
            "success_rate_delta": cur["success_rate"]
            - base["success_rate"],
            "tokens_per_task_delta": cur["tokens_per_task"]
            - base["tokens_per_task"],
            "latency_ms_per_task_delta": cur["latency_ms_per_task"]
            - base["latency_ms_per_task"],
            "calls_per_task_delta": cur["calls_per_task"]
            - base["calls_per_task"],
        })
    return {"per_task": per_task, "summary": summary, "delta": delta}
