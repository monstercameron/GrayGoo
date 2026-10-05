"""Capability consolidation + skill forgetting (plan.md sections 31-33).

Periodic maintenance over patch/capability/skill records:

- duplicate + overlapping-intent detection via token-overlap similarity,
- unused-skill detection (no reuse within a window),
- merge / generalize proposals rehearsed through an injected pipeline,
- retirement (deprecated marking) and expiry sweeps,
- evidence-gated renewal, and a before/after retrieval-quality harness.

Thresholds (all tunable per call; constants document the defaults)::

    DUPLICATE_THRESHOLD = 0.60  # Jaccard token similarity -> merge candidates
    OVERLAP_THRESHOLD   = 0.25  # Jaccard token similarity -> generalize family
    UNUSED_WINDOW_DAYS  = 30.0  # no reuse within this window -> unused
    DEFAULT_TTL_DAYS    = 7.0   # assumed TTL when a record carries none

Similarity is Jaccard similarity over lowercase alphanumeric tokens
(minus a small stopword set), so 1.0 means identical wording and 0.0
means no shared vocabulary. Short near-duplicate intents (e.g.
"parse CSV rows into records" vs "parse CSV rows into record
objects", Jaccard 0.5...0.7) cluster at ``DUPLICATE_THRESHOLD`` while
merely topical pairs (e.g. CSV parsing vs JSON parsing, ~0.3) only
reach ``OVERLAP_THRESHOLD``.

Records are duck-typed dicts (or objects with the same attributes)::

    id:      "id" | "patch_id" | "capability_id" | "skill_id" | "name"
    intent:  "intent" | "text" | "description" | "goal"
             (patches without intent fall back to family/tags text)
    status:  "status" ("deprecated" | "retired" | "expired" = inactive)
    created: "created_at" (epoch seconds); TTL from "ttl_days" / "expires_at"
    reuses:  "reuses" | "reuse_outcomes" | "outcomes" | "reuse_history"
             entries carry "recorded_at" plus "helped" / "success"

Stores are duck-typed too: anything with ``list_patches`` behaves like
``patches.PatchStore`` (this module never imports ``patches``,
``retrieve``, or ``pipeline``; the rehearsal pipeline is always an
injected callable so tests can use fakes). Stdlib only.
"""

import re
import time

DUPLICATE_THRESHOLD = 0.60
OVERLAP_THRESHOLD = 0.25
UNUSED_WINDOW_DAYS = 30.0
DEFAULT_TTL_DAYS = 7.0
SECONDS_PER_DAY = 86400.0

RETIRED_STATUSES = frozenset({"retired", "deprecated", "expired"})

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "with",
    "into", "from", "by", "is", "are", "be", "as", "at", "it", "its",
})

_ID_KEYS = ("id", "patch_id", "capability_id", "skill_id", "name")
_INTENT_KEYS = ("intent", "text", "description", "goal")
_REUSE_KEYS = ("reuses", "reuse_outcomes", "outcomes", "reuse_history",
               "transfer_evidence", "evidence")
_REUSE_TS_KEYS = ("last_reused_at", "last_used_at", "last_use_at",
                  "last_reuse_at", "last_used", "last_reuse")


def _now():
    return time.time()


# -- record accessors (duck-typed) -----------------------------------

def _field(record, keys, default=None):
    if isinstance(record, dict):
        for key in keys:
            if record.get(key) not in (None, ""):
                return record[key]
        return default
    for key in keys:
        value = getattr(record, key, None)
        if value not in (None, ""):
            return value
    return default


def record_id(record):
    """Best-effort id for a dict- or object-style record (may be None)."""
    return _field(record, _ID_KEYS)


def record_intent(record):
    """Best-effort intent text for a record ("" when absent)."""
    text = _field(record, _INTENT_KEYS, "")
    if text:
        return str(text)
    if isinstance(record, dict):
        # Patches often carry no intent; fall back to family/tags text so
        # same-family patches still cluster as overlapping.
        family = record.get("task_family") or record.get("family") or ""
        tags = record.get("tags") or []
        blob = " ".join([str(family)] + [str(t) for t in tags]).strip()
        if blob:
            return blob
        candidate = record.get("candidate")
        if isinstance(candidate, str):
            return candidate
        if isinstance(candidate, dict):
            for key in ("intent", "text", "target", "goal"):
                if candidate.get(key):
                    return str(candidate[key])
    return ""


def record_status(record):
    """Lowercased status string ("live" when the record carries none)."""
    status = _field(record, ("status",), "")
    return str(status).lower() if status else "live"


def is_retired(record):
    """True when a record is marked retired/deprecated/expired."""
    return record_status(record) in RETIRED_STATUSES


def tokenize(text):
    """Lowercase alphanumeric tokens minus stopwords."""
    if not text:
        return set()
    return set(_TOKEN_RE.findall(str(text).lower())) - _STOPWORDS


def intent_similarity(a, b):
    """Jaccard token similarity of two intent texts/records (0.0-1.0).

    Accepts raw strings or records (intent text is extracted). Two empty
    texts score 0.0 — vacuous records must not cluster as duplicates.
    """
    if not isinstance(a, str):
        a = record_intent(a)
    if not isinstance(b, str):
        b = record_intent(b)
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


similarity = intent_similarity
token_overlap = intent_similarity


# -- duplicate / overlap detection -----------------------------------

def _as_record_list(records_or_store):
    if isinstance(records_or_store, dict):
        return list(records_or_store.values())
    if hasattr(records_or_store, "list_patches"):
        try:
            return list(records_or_store.list_patches(include_expired=True))
        except TypeError:
            return list(records_or_store.list_patches())
    if hasattr(records_or_store, "entries"):
        return [e.capability if hasattr(e, "capability") else e
                for e in records_or_store.entries()]
    if hasattr(records_or_store, "__iter__"):
        return list(records_or_store)
    raise TypeError("expected a record list, id->record dict, patch store, "
                    "or capability index")


def cluster_intents(records, threshold=DUPLICATE_THRESHOLD,
                    exclude_retired=True):
    """Group records whose pairwise intent similarity reaches ``threshold``.

    Single-linkage clustering (union-find): any pair scoring >= threshold
    joins a cluster. Returns clusters of >= 2 records only, each sorted by
    id, ordered by first member id for determinism.
    """
    pool = _as_record_list(records)
    if exclude_retired:
        pool = [r for r in pool if not is_retired(r)]
    parent = list(range(len(pool)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            if intent_similarity(pool[i], pool[j]) >= threshold:
                union(i, j)
    groups = {}
    for i, record in enumerate(pool):
        groups.setdefault(find(i), []).append(record)
    clusters = [sorted(g, key=lambda r: str(record_id(r)))
                for g in groups.values() if len(g) >= 2]
    clusters.sort(key=lambda g: str(record_id(g[0])))
    return clusters


def find_duplicates(records, threshold=DUPLICATE_THRESHOLD, **kwargs):
    """Clusters of near-duplicate intents (merge candidates)."""
    return cluster_intents(records, threshold=threshold, **kwargs)


def find_overlapping(records, threshold=OVERLAP_THRESHOLD, **kwargs):
    """Clusters of overlapping intents (generalize candidates)."""
    return cluster_intents(records, threshold=threshold, **kwargs)


detect_duplicates = find_duplicates
detect_overlapping = find_overlapping
find_duplicate_clusters = find_duplicates
find_overlap_clusters = find_overlapping


# -- unused-skill detection ------------------------------------------

def _reuse_timestamps(record, reuses=None, store=None):
    stamps = []
    if isinstance(reuses, dict):
        rid = record_id(record)
        candidates = reuses.get(rid, []) if rid in reuses else []
    elif reuses is not None:
        candidates = reuses
    else:
        candidates = _field(record, _REUSE_KEYS, []) or []
    if isinstance(candidates, dict):
        candidates = [candidates]
    for entry in candidates or []:
        if isinstance(entry, (int, float)):
            stamps.append(float(entry))
        elif isinstance(entry, dict):
            for key in ("recorded_at", "reused_at", "used_at", "at",
                        "timestamp", "created_at"):
                if entry.get(key) is not None:
                    try:
                        stamps.append(float(entry[key]))
                    except (TypeError, ValueError):
                        pass
                    break
    for key in _REUSE_TS_KEYS:
        value = record.get(key) if isinstance(record, dict) \
            else getattr(record, key, None)
        if value is not None:
            try:
                stamps.append(float(value))
            except (TypeError, ValueError):
                pass
    if store is not None and hasattr(store, "get_reuses"):
        rid = record_id(record)
        try:
            extra = store.get_reuses(rid) if rid is not None \
                else store.get_reuses()
        except TypeError:
            extra = []
        for entry in extra or []:
            if isinstance(entry, dict) and entry.get("recorded_at") is not None:
                try:
                    stamps.append(float(entry["recorded_at"]))
                except (TypeError, ValueError):
                    pass
    return stamps


def find_unused(skills, window_days=UNUSED_WINDOW_DAYS, now=None,
                reuses=None, exclude_retired=True):
    """Skills with no recorded reuse inside the last ``window_days``.

    ``skills`` is a record list, id->record dict, or patch-like store
    (reuse outcomes are then pulled via ``store.get_reuses``).
    Never-reused skills count as unused. Returns the unused records
    sorted by id.
    """
    if now is None:
        now = _now()
    cutoff = now - float(window_days) * SECONDS_PER_DAY
    store = skills if hasattr(skills, "get_reuses") else None
    pool = _as_record_list(skills)
    unused = []
    for record in pool:
        if exclude_retired and is_retired(record):
            continue
        stamps = _reuse_timestamps(record, reuses=reuses, store=store)
        if not any(s >= cutoff for s in stamps):
            unused.append(record)
    unused.sort(key=lambda r: str(record_id(r)))
    return unused


detect_unused = find_unused
detect_unused_skills = find_unused
find_unused_skills = find_unused


# -- merge / generalize proposals ------------------------------------

def _union_intent(members):
    seen, sentences = set(), []
    for record in members:
        for chunk in str(record_intent(record)).replace("\n", " ").split(". "):
            sentence = chunk.strip().rstrip(".")
            if sentence and sentence not in seen:
                seen.add(sentence)
                sentences.append(sentence)
    return ". ".join(sentences)


def _min_pairwise_similarity(members):
    if len(members) < 2:
        return 1.0
    worst = 1.0
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            worst = min(worst, intent_similarity(members[i], members[j]))
    return worst


def propose_merge(candidates):
    """Propose merging near-duplicate ``candidates`` (>= 2 records).

    Returns ``{"kind": "merge", "members": [ids], "rationale": str,
    "merged_intent": str}``. Raises ``ValueError`` for fewer than two
    candidates.
    """
    members = _as_record_list(candidates) if not isinstance(candidates, list) \
        else list(candidates)
    members = [m for m in members if not isinstance(m, str)]
    if len(members) < 2:
        raise ValueError("propose_merge needs >= 2 candidate records, got %d"
                         % len(members))
    ids = [record_id(m) for m in members]
    worst = _min_pairwise_similarity(members)
    return {
        "kind": "merge",
        "members": ids,
        "rationale": ("merge %d near-duplicate capabilities %s "
                      "(min pairwise intent similarity %.2f >= %.2f)"
                      % (len(members), ids, worst, DUPLICATE_THRESHOLD)),
        "merged_intent": _union_intent(members),
    }


def propose_generalize(family):
    """Propose generalizing an overlapping ``family`` into one procedure.

    ``family`` is a member-record list or a ``{"family": name, "members":
    [...]}`` (also accepts "skills"/"capabilities"/"candidates" for the
    member list). Returns ``{"kind": "generalize", "members": [ids],
    "rationale": str, "merged_intent": str}``.
    """
    name = ""
    member_keys = ("members", "skills", "capabilities", "candidates",
                   "records")
    if isinstance(family, dict) and any(
            isinstance(family.get(k), (list, tuple)) for k in member_keys):
        for key in member_keys:
            if isinstance(family.get(key), (list, tuple)):
                members = list(family[key])
                break
        name = str(family.get("family") or family.get("name") or "")
    else:
        members = _as_record_list(family) if not isinstance(family, list) \
            else list(family)
    members = [m for m in members if not isinstance(m, str)]
    if len(members) < 2:
        raise ValueError("propose_generalize needs >= 2 family members, "
                         "got %d" % len(members))
    ids = [record_id(m) for m in members]
    if not name:
        families = {str(_field(m, ("task_family", "family"), "") or "")
                    for m in members}
        families.discard("")
        name = sorted(families)[0] if len(families) == 1 else \
            "%d-member family" % len(members)
    worst = _min_pairwise_similarity(members)
    return {
        "kind": "generalize",
        "members": ids,
        "family": name,
        "rationale": ("generalize overlapping family %r (%d members %s; "
                      "min pairwise intent similarity %.2f) into one "
                      "broader procedure" % (name, len(members), ids, worst)),
        "merged_intent": "Generalized procedure covering %s: %s"
                         % (name, _union_intent(members)),
    }


# -- rehearsal (injected pipeline only) -------------------------------

def _verdict_passed(verdict):
    if isinstance(verdict, bool):
        return verdict
    if verdict is None:
        return False
    if isinstance(verdict, dict):
        for key in ("passed", "pass", "success", "ok", "approved"):
            if key in verdict:
                return bool(verdict[key])
        text = str(verdict.get("verdict", "")).lower()
        if text:
            if text in ("pass", "passed", "ok", "success", "accept",
                        "accepted", "approve", "approved"):
                return True
            if text in ("fail", "failed", "reject", "rejected", "error"):
                return False
            return bool(verdict.get("verdict"))
        return False
    if isinstance(verdict, str):
        text = verdict.strip().lower()
        if text in ("pass", "passed", "ok", "success", "true", "accept",
                    "accepted", "approved"):
            return True
        if text in ("fail", "failed", "reject", "rejected", "false", "error",
                    "no", ""):
            return False
    return bool(verdict)


def rehearse_consolidation(proposal, *, pipeline_fn):
    """Rehearse consolidation proposal(s) through an injected pipeline.

    ``pipeline_fn`` is called EXACTLY ONCE per proposal with a merged
    candidate ``{"intent", "kind", "members", "rationale",
    "source": "consolidation"}`` (this module never imports the real
    pipeline). A single proposal dict returns one result dict
    ``{"proposal", "candidate", "verdict", "passed"}``; a list of
    proposals returns a same-order list of results.
    """
    if callable(getattr(pipeline_fn, "run", None)) and \
            not callable(pipeline_fn):
        runner = pipeline_fn.run
    else:
        runner = pipeline_fn
    if not callable(runner):
        raise TypeError("pipeline_fn must be callable")
    single = isinstance(proposal, dict)
    proposals = [proposal] if single else list(proposal)
    results = []
    for item in proposals:
        candidate = {
            "intent": item.get("merged_intent", ""),
            "kind": item.get("kind", "merge"),
            "members": list(item.get("members", [])),
            "rationale": item.get("rationale", ""),
            "source": "consolidation",
        }
        verdict = runner(candidate)
        results.append({"proposal": item, "candidate": candidate,
                        "verdict": verdict, "passed": _verdict_passed(verdict)})
    return results[0] if single else results


# -- retirement ------------------------------------------------------

def _mark_status(record, status):
    if isinstance(record, dict):
        record["status"] = status
        return True
    try:
        setattr(record, "status", status)
        return True
    except (AttributeError, TypeError):
        return False


def retire(target, ids=None, *, status="deprecated"):
    """Mark capabilities/skills ``target`` as retired (default "deprecated").

    ``target`` is a record list, an id->record dict, a patch-like store
    (persisted via ``save_patch`` when available), or a bare id list
    (nothing to mark; ids are just reported). ``ids`` optionally
    restricts which members are retired — required when ``target`` is a
    store (retiring a whole store by accident must be explicit).
    Records that cannot carry a status (e.g. slotted objects) still have
    their ids reported so callers can exclude them from retrieval.
    Returns ``{"retired": [ids], "count": n, "status": status}``.
    """
    want = set(ids) if ids is not None else None
    if hasattr(target, "list_patches") and not isinstance(target, (list,
                                                                  tuple,
                                                                  dict)):
        if want is None:
            raise ValueError("retire(store) requires ids")
        try:
            pool = list(target.list_patches(include_expired=True))
        except TypeError:
            pool = list(target.list_patches())
        persist = hasattr(target, "_write_patch") or hasattr(target,
                                                             "save_patch")
    elif isinstance(target, dict):
        if target and all(isinstance(v, dict) for v in target.values()):
            pool = list(target.values())  # id -> record map
        else:
            pool = [target]  # single record
        persist = False
    else:
        pool = list(target)
        persist = False
    retired = []
    for record in pool:
        if isinstance(record, str):
            if want is None or record in want:
                retired.append(record)
            continue
        rid = record_id(record)
        if want is not None and rid not in want:
            continue
        _mark_status(record, status)
        retired.append(rid)
    retired = sorted(retired, key=str)
    if persist:
        for record in pool:
            if record_id(record) in set(retired):
                if hasattr(target, "_write_patch"):
                    target._write_patch(record)
                else:
                    target.save_patch(record.get("task_id", ""),
                                      record.get("candidate"),
                                      patch_id=record.get("patch_id"),
                                      created_at=record.get("created_at"),
                                      ttl_days=record.get("ttl_days",
                                                          DEFAULT_TTL_DAYS),
                                      status=status)
    return {"retired": retired, "count": len(retired), "status": status}


def retrieval_candidates(records_or_index, exclude=None,
                         include_retired=False):
    """Records eligible for retrieval: retired/expired ones removed.

    Accepts a record list, id->record dict, patch-like store, or
    capability index. ``exclude`` adds explicit ids to drop.
    """
    pool = _as_record_list(records_or_index)
    banned = set(exclude or [])
    out = []
    for record in pool:
        rid = record_id(record)
        if rid in banned:
            continue
        if not include_retired and is_retired(record):
            continue
        out.append(record)
    return out


live_candidates = retrieval_candidates


# -- expiry sweep ----------------------------------------------------

def _is_expired_like(record, now):
    if is_retired(record) and record_status(record) == "expired":
        return True
    if isinstance(record, dict):
        expires_at = record.get("expires_at")
    else:
        expires_at = getattr(record, "expires_at", None)
    if expires_at is not None:
        try:
            return float(expires_at) < now
        except (TypeError, ValueError):
            return True
    created = _field(record, ("created_at",), None)
    if created is None:
        return False  # no expiry info: keep (conservative for skills)
    try:
        created = float(created)
    except (TypeError, ValueError):
        return True
    ttl = _field(record, ("ttl_days", "ttl"), DEFAULT_TTL_DAYS)
    try:
        ttl = float(ttl)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_DAYS
    return (created + ttl * SECONDS_PER_DAY) < now


def sweep_expired(store, now=None):
    """Delete/drop expired records; report ``{"swept", "remaining"}`` counts.

    Patch-like stores (with ``sweep``) are delegated to and the counts
    normalized; record lists are filtered in place; id->record dicts
    have expired keys removed. Only records with positive expiry
    evidence (``expires_at`` past, or ``created_at`` + TTL past) are
    swept — records without any expiry info are kept.
    """
    if now is None:
        now = _now()
    if hasattr(store, "sweep") and not isinstance(store, (list, tuple, dict)):
        try:
            result = store.sweep(now=now)
        except TypeError:
            result = store.sweep()
        if isinstance(result, dict):
            swept = result.get("swept", result.get("deleted",
                                                   result.get("expired", 0)))
            remaining = result.get("remaining", result.get("kept", 0))
            return {"swept": int(swept), "remaining": int(remaining)}
        try:
            return {"swept": int(result), "remaining": 0}
        except (TypeError, ValueError):
            return {"swept": 0, "remaining": 0}
    if isinstance(store, dict):
        expired_ids = [k for k, v in store.items()
                       if _is_expired_like(v, now)]
        for key in expired_ids:
            del store[key]
        return {"swept": len(expired_ids), "remaining": len(store),
                "expired_ids": sorted(expired_ids, key=str)}
    records = list(store)
    survivors = [r for r in records if not _is_expired_like(r, now)]
    swept = len(records) - len(survivors)
    if isinstance(store, list):
        store[:] = survivors
    return {"swept": swept, "remaining": len(survivors),
            "expired_ids": sorted((record_id(r) for r in records
                                   if _is_expired_like(r, now)), key=str)}


# -- evidence-gated renewal ------------------------------------------

def _helped_outcomes(evidence):
    if evidence is None:
        return []
    if isinstance(evidence, dict):
        evidence = [evidence]
    helped = []
    for entry in evidence:
        if isinstance(entry, dict):
            for key in ("helped", "success", "succeeded"):
                if key in entry:
                    if entry[key]:
                        helped.append(entry)
                    break
    return helped


def _record_evidence(record, evidence, store):
    if evidence is not None and not isinstance(evidence, str):
        return evidence
    if isinstance(evidence, str) and store is not None \
            and hasattr(store, "get_reuses"):
        try:
            return store.get_reuses(evidence)
        except TypeError:
            return []
    if store is not None and hasattr(store, "get_reuses"):
        rid = record_id(record) if not isinstance(record, str) else record
        if rid is not None:
            try:
                return store.get_reuses(rid)
            except TypeError:
                pass
    if isinstance(record, dict):
        for key in _REUSE_KEYS:
            if record.get(key):
                return record[key]
    return []


def renew_on_evidence(target, evidence=None, *, now=None):
    """Renew a skill's TTL only with positive reuse evidence.

    ``target`` is a record dict or a patch-like store (then ``evidence``
    is the skill/patch id and outcomes come from
    ``store.get_reuses``); otherwise ``evidence`` is an outcome (list)
    or None to read embedded reuse history. Renewal is GRANTED iff at
    least one outcome shows helped/success truthy — empty or
    unhelpful evidence REFUSES (returns False, no mutation). On grant
    the record's TTL window restarts (``created_at`` = now,
    ``renewed_at`` stamped, ``renewals`` incremented; persisted for
    stores). Returns True/False.
    """
    if now is None:
        now = _now()
    store = (target if hasattr(target, "get_reuses")
             and not isinstance(target, dict) else None)
    if store is not None:
        if not isinstance(evidence, str):
            raise ValueError("renew_on_evidence(store) needs a skill id")
        record = None
        if hasattr(store, "list_patches"):
            try:
                pool = store.list_patches(include_expired=True)
            except TypeError:
                pool = store.list_patches()
            for candidate in pool:
                if record_id(candidate) == evidence:
                    record = candidate
                    break
        outcomes = _record_evidence(evidence, evidence, store)
    else:
        record = target
        outcomes = _record_evidence(record, evidence, None)
    if not _helped_outcomes(outcomes):
        return False
    if isinstance(record, dict):
        record["created_at"] = float(now)
        record["renewed_at"] = float(now)
        try:
            record["renewals"] = int(record.get("renewals", 0)) + 1
        except (TypeError, ValueError):
            record["renewals"] = 1
        if store is not None:
            if hasattr(store, "_write_patch"):
                store._write_patch(record)
            elif hasattr(store, "save_patch") and record.get("patch_id"):
                store.save_patch(record.get("task_id", ""),
                                 record.get("candidate"),
                                 patch_id=record.get("patch_id"),
                                 created_at=record.get("created_at"),
                                 ttl_days=record.get("ttl_days",
                                                     DEFAULT_TTL_DAYS),
                                 status=record.get("status", "patch"))
    return True


renew_skill = renew_on_evidence


# -- retrieval-quality harness ---------------------------------------

def _ranked_ids(ranked):
    ids = []
    for entry in ranked or []:
        if isinstance(entry, str):
            ids.append(entry)
        elif isinstance(entry, (list, tuple)) and entry:
            first = entry[0]
            ids.append(first if isinstance(first, str) else record_id(first))
        else:
            ids.append(record_id(entry))
    return ids


def default_ranker(query, index):
    """Rank ``index`` records by intent similarity to ``query`` text.

    ``query`` is a string or a goal-ish dict ("text" | "query" | "goal"
    | "intent"). Ties break by id for determinism. Returns ids.
    """
    if isinstance(query, dict):
        text = str(query.get("text", query.get("query",
                                               query.get("goal",
                                                         query.get("intent",
                                                                   "")))))
    else:
        text = str(query)
    scored = [(intent_similarity(text, r), str(record_id(r)), record_id(r))
              for r in _as_record_list(index)]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [rid for _, _, rid in scored]


def _query_text_and_relevant(query):
    if isinstance(query, (list, tuple)):
        text, relevant = query[0], query[1] if len(query) > 1 else []
    elif isinstance(query, dict):
        text = query.get("text", query.get("query", query.get("goal",
                                                              query.get(
                                                                  "intent",
                                                                  ""))))
        relevant = query.get("relevant", query.get("relevant_ids",
                                                   query.get("expected", [])))
    else:
        text, relevant = query, []
    if isinstance(relevant, str):
        relevant = [relevant]
    return text, list(relevant or [])


def _mrr_hit_rate(ranked_ids, relevant, k):
    relevant = set(relevant)
    for rank, rid in enumerate(ranked_ids[:k], start=1):
        if rid in relevant:
            return 1.0 / rank, 1.0
    return 0.0, 0.0


def retrieval_quality_before_after(before, after=None, queries=None, *,
                                   retrieve_fn=None, k=5):
    """Rank-quality delta of a consolidation: MRR and hit-rate @ ``k``.

    Two forms: ``(before_index, after_index, queries)`` compares two
    explicit indexes, while ``(index, queries)`` runs the forgetting
    experiment — ``before`` is the full index and ``after`` is the same
    index minus retired/expired records. ``queries`` are dicts with
    query text ("text" | "query" | "goal" | "intent") plus relevance
    judgments ("relevant" | "relevant_ids" | "expected"), or
    ``(text, relevant_ids)`` tuples. ``retrieve_fn(query, index)``
    returns ranked ids (default: :func:`default_ranker`). Returns
    ``{"before": {"mrr", "hit_rate", "n"}, "after": {...},
    "delta_mrr", "delta_hit_rate", "delta", "k"}`` where positive
    deltas mean the consolidation improved retrieval.
    """
    if queries is None:
        if after is None:
            raise ValueError("retrieval_quality_before_after needs queries")
        queries, after = after, None
    if after is None:
        after_pool = retrieval_candidates(before)
    else:
        after_pool = after
    rank = retrieve_fn or default_ranker
    before_mrr = before_hit = after_mrr = after_hit = 0.0
    n = 0
    for query in queries or []:
        text, relevant = _query_text_and_relevant(query)
        if not relevant:
            continue
        n += 1
        mrr, hit = _mrr_hit_rate(_ranked_ids(rank(query, before)), relevant,
                                 k)
        before_mrr += mrr
        before_hit += hit
        mrr, hit = _mrr_hit_rate(_ranked_ids(rank(query, after_pool)),
                                 relevant, k)
        after_mrr += mrr
        after_hit += hit
    before_mrr = before_mrr / n if n else 0.0
    before_hit = before_hit / n if n else 0.0
    after_mrr = after_mrr / n if n else 0.0
    after_hit = after_hit / n if n else 0.0
    return {
        "before": {"mrr": before_mrr, "hit_rate": before_hit, "n": n},
        "after": {"mrr": after_mrr, "hit_rate": after_hit, "n": n},
        "delta_mrr": after_mrr - before_mrr,
        "delta_hit_rate": after_hit - before_hit,
        "delta": after_mrr - before_mrr,
        "k": k,
    }
