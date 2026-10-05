"""In-memory capability retrieval v0 (plan.md sections 12-13).

Provides a small in-memory index of capability descriptors plus:

- :func:`find_capabilities`: rank indexed capabilities for a goal by
  keyword overlap + input/output type match + effect compatibility +
  historical reuse. Returns at most ``k`` (default 8) matches.
- :func:`compose_plan`: chain existing capabilities whose types connect
  (output of one satisfies input of the next) to satisfy a goal that no
  single capability covers. Callers SHOULD attempt composition before
  reporting "no match" (plan.md section 13).

Goal shape (dict, or a plain string treated as intent text)::

    {
        "text": "parse CSV rows into records",  # intent / description
        "input_types": ["csv-text"],            # types the goal starts from
        "output_types": ["record"],             # types the goal must produce
        "family": "parsing",                    # optional task family
        "allowed_effects": [...],               # optional effect allowlist
        "forbidden_effects": [...],             # optional effect denylist
    }

Constraints shape (dict, all keys optional)::

    {
        "allowed_effects": [...],  # None/omitted means "any effect allowed"
        "forbidden_effects": [...],
        "family": "parsing",       # restrict to one task family
    }

KNOWN LIMITATION: v0 ranks by keyword token overlap, not semantic
similarity. There are no embeddings yet, so paraphrased intents with no
shared tokens will not match ("semantic similarity" in plan.md section 12
is a stub). Type, effect, and reuse signals compensate in part; a future
version should add an embedding or synonym back end behind the same API.

Semantic retrieval (additive; closes the "semantic similarity" gap above):

- :func:`build_semantic_index`: fit an embedding backend over the
  capability corpus and store one vector per capability id.
- :func:`find_capabilities_semantic`: the same ranking as
  :func:`find_capabilities` plus an embedding-cosine term
  (``score + _W_SEMANTIC * cosine``).

Backends live in embeddings.py behind the ``EmbeddingBackend`` protocol
(``embed(texts) -> vectors``); the default TF-IDF backend is offline and
deterministic, and hosted embedding models can implement the same
protocol later without changing this API.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "with",
    "into", "from", "by", "is", "are", "be", "as", "at", "it", "its",
})

# Score weights. Type and effect signals deliberately outweigh raw keyword
# overlap so a capability that fits the caller's contract outranks one that
# merely shares vocabulary.
_W_KEYWORD = 1.0
_W_INPUT_TYPE = 3.0
_W_OUTPUT_TYPE = 3.0
_W_FAMILY = 1.0
_W_EFFECT = 2.0
_W_REUSE = 1.5

# Semantic fusion weight (used only by find_capabilities_semantic).
# Embedding cosine lives in [0, 1], so a perfect paraphrase contributes
# _W_SEMANTIC points — on par with one input/output type match (3.0) plus
# keyword overlap (1.0): enough to rescue a reworded intent that keyword
# overlap misses, not enough to override type/effect fit on its own.
_W_SEMANTIC = 4.0


def tokenize(text: str):
    """Lowercase alphanumeric tokens minus stopwords."""
    if not text:
        return set()
    return {t for t in _TOKEN_RE.findall(text.lower())} - _STOPWORDS


class Capability:
    """One immutable capability descriptor (metadata subset of plan.md sec 8)."""

    __slots__ = ("id", "intent", "input_types", "output_types", "effects",
                 "family", "version", "success_count", "use_count")

    def __init__(self, id, intent="", input_types=(), output_types=(),
                 effects=(), family="", version=1, success_count=0,
                 use_count=0):
        self.id = id
        self.intent = intent or ""
        self.input_types = tuple(input_types or ())
        self.output_types = tuple(output_types or ())
        self.effects = tuple(effects or ())
        self.family = family or ""
        self.version = version
        self.success_count = success_count
        self.use_count = use_count

    def success_rate(self) -> float:
        if self.use_count <= 0:
            return 0.0
        return self.success_count / self.use_count

    def __repr__(self):
        return "Capability(id=%r, version=%r)" % (self.id, self.version)


@dataclass
class _IndexEntry:
    capability: Capability
    tokens: frozenset = field(default_factory=frozenset)


class CapabilityIndex:
    """Small in-memory capability store keyed by capability id."""

    def __init__(self, capabilities=()):
        self._entries: dict = {}
        for cap in capabilities:
            self.add(cap)

    def add(self, capability: Capability) -> Capability:
        """Register (or replace) a capability; returns it."""
        if not isinstance(capability, Capability):
            raise TypeError("CapabilityIndex holds Capability objects, got %r"
                            % type(capability).__name__)
        if not capability.id:
            raise ValueError("capability id must be non-empty")
        entry = _IndexEntry(capability=capability,
                            tokens=frozenset(tokenize(capability.intent)))
        self._entries[capability.id] = entry
        return capability

    def get(self, capability_id: str):
        entry = self._entries.get(capability_id)
        return entry.capability if entry else None

    def __len__(self):
        return len(self._entries)

    def __iter__(self):
        for entry in self._entries.values():
            yield entry.capability

    def entries(self):
        return list(self._entries.values())

    def find_capabilities(self, goal, constraints=None, k=8):
        """Rank this index's capabilities for ``goal``; top ``k`` first.

        See the module-level :func:`find_capabilities` for the goal and
        constraints shapes. Returns ``(capability, score)`` tuples,
        highest score first; ``[]`` when nothing matches.
        """
        return find_capabilities(goal, constraints, k, _index=self)


def _normalize_goal(goal) -> dict:
    if goal is None:
        return {}
    if isinstance(goal, str):
        return {"text": goal}
    if isinstance(goal, dict):
        return dict(goal)
    raise TypeError("goal must be a dict, str, or None")


def _normalize_constraints(constraints) -> dict:
    if constraints is None:
        return {}
    if isinstance(constraints, dict):
        return dict(constraints)
    raise TypeError("constraints must be a dict or None")


def _effect_allowed(cap: Capability, allowed, forbidden) -> bool:
    effects = set(cap.effects)
    if forbidden and effects & set(forbidden):
        return False
    if allowed is not None and not effects <= set(allowed):
        return False
    return True


def _score_entry(entry: _IndexEntry, goal: dict, constraints: dict):
    """Score one capability. Returns None when hard-excluded."""
    cap = entry.capability

    family = constraints.get("family", goal.get("family", None))
    if family and cap.family != family:
        return None

    allowed = constraints.get("allowed_effects", goal.get("allowed_effects"))
    forbidden = constraints.get("forbidden_effects",
                                goal.get("forbidden_effects"))
    if not _effect_allowed(cap, allowed, forbidden):
        return None

    goal_text = goal.get("text", goal.get("intent", ""))
    goal_tokens = tokenize(goal_text) if goal_text else set()
    if goal_tokens and entry.tokens:
        overlap = len(goal_tokens & entry.tokens)
        keyword = overlap / len(goal_tokens | entry.tokens)
    else:
        keyword = 0.0

    want_in = set(goal.get("input_types") or ())
    want_out = set(goal.get("output_types") or ())
    have_in = set(cap.input_types)
    have_out = set(cap.output_types)
    input_match = len(want_in & have_in) / len(want_in) if want_in else 0.0
    output_match = len(want_out & have_out) / len(want_out) if want_out else 0.0

    family_match = 1.0 if (family and cap.family == family) else 0.0

    # Effect compatibility: pure capabilities (no effects) fit anywhere and
    # score a small bonus; effectful ones score for staying within the
    # allowlist (already enforced above as a hard filter).
    if not cap.effects:
        effect_score = 1.0
    elif allowed is None:
        effect_score = 0.5
    else:
        effect_score = 1.0

    reuse = cap.success_rate() * math.log1p(cap.use_count) / math.log1p(10)
    reuse = min(reuse, 1.0)

    score = (_W_KEYWORD * keyword
             + _W_INPUT_TYPE * input_match
             + _W_OUTPUT_TYPE * output_match
             + _W_FAMILY * family_match
             + _W_EFFECT * effect_score
             + _W_REUSE * reuse)
    return score


def find_capabilities(goal, constraints=None, k=8, _index=None):
    """Rank indexed capabilities for ``goal``; return top ``k`` matches.

    ``goal`` is a goal dict (see module docstring) or a plain intent
    string; ``constraints`` is an optional constraints dict. The index
    is read from ``constraints["index"]`` when present, else from
    ``goal["index"]`` (``CapabilityIndex.find_capabilities`` passes its
    own index automatically). Returns a list of ``(capability, score)``
    tuples, highest score first. Capabilities that violate the family or
    effect constraints, or that score zero on every signal, are excluded,
    so an empty index — or one with nothing relevant — yields a clean
    empty list ("no match").
    """
    goal = _normalize_goal(goal)
    constraints = _normalize_constraints(constraints)
    index = _index
    if index is None:
        index = constraints.get("index", goal.get("index"))
    if index is None:
        raise ValueError("no capability index supplied "
                         "(use CapabilityIndex.find_capabilities or pass "
                         "index=... in goal or constraints)")
    if k is None or k <= 0:
        return []

    scored = []
    for entry in index.entries():
        score = _score_entry(entry, goal, constraints)
        if score is None or score <= 0:
            continue
        scored.append((entry.capability, score))
    scored.sort(key=lambda pair: (-pair[1], pair[0].id))
    return scored[:k]


def _types_connect(out_types, in_types) -> bool:
    """True when a producer's outputs can feed a consumer's inputs.

    An empty consumer input list means "needs nothing" (a source step);
    an empty producer output list never connects.
    """
    if not in_types:
        return True
    if not out_types:
        return False
    return bool(set(out_types) & set(in_types))


def compose_plan(goal, index, max_depth=4):
    """Chain existing capabilities whose types connect to satisfy ``goal``.

    Breadth-first search over the type graph: start from the goal's
    ``input_types`` and extend chains while each step's outputs connect to
    the next step's inputs, stopping at the first chain whose final
    outputs cover the goal's ``output_types``. Returns the chain as a
    list of :class:`Capability` (shortest chain found), or ``None`` when
    no chain connects — the caller should only report "no match" after
    this returns ``None``. Effect/family constraints in the goal dict are
    honored as hard filters during the search.
    """
    goal = _normalize_goal(goal)
    if index is None or len(index) == 0:
        return None
    want_out = set(goal.get("output_types") or ())
    if not want_out:
        return None
    have = set(goal.get("input_types") or ())
    allowed = goal.get("allowed_effects")
    forbidden = goal.get("forbidden_effects")
    family = goal.get("family")

    candidates = []
    for cap in index:
        if family and cap.family != family:
            continue
        if not _effect_allowed(cap, allowed, forbidden):
            continue
        candidates.append(cap)
    candidates.sort(key=lambda c: (-c.success_rate(), c.id))

    # Direct hit: one capability covering the goal outputs (and runnable
    # from the goal inputs, or needing nothing).
    for cap in candidates:
        if want_out <= set(cap.output_types) and (
                not cap.input_types or set(cap.input_types) <= have):
            return [cap]

    # BFS over (available-types, chain) states.
    start = frozenset(have)
    queue = [(start, [])]
    visited = {start}
    while queue:
        available, chain = queue.pop(0)
        if len(chain) >= max_depth:
            continue
        for cap in candidates:
            if cap in chain:
                continue
            if cap.input_types and not (set(cap.input_types) <= set(available)):
                continue
            if chain and not _types_connect(chain[-1].output_types,
                                            cap.input_types):
                continue
            new_chain = chain + [cap]
            produced = set(available) | set(cap.output_types)
            if want_out <= produced:
                return new_chain
            key = frozenset(produced)
            if key not in visited:
                visited.add(key)
                queue.append((key, new_chain))
    return None


def _semantic_document(cap: Capability) -> str:
    """Text embedded for one capability: intent + family + type names."""
    parts = [cap.intent or "", cap.family or ""]
    parts.extend(cap.input_types)
    parts.extend(cap.output_types)
    return " ".join(p for p in parts if p)


def build_semantic_index(capabilities, backend):
    """Fit ``backend`` on the capability corpus; return a vector index.

    ``capabilities`` is a :class:`CapabilityIndex` or an iterable of
    :class:`Capability`. The backend is fitted (TF-IDF: IDF weights) on
    one document per capability (see :func:`_semantic_document`) and each
    document vector is stored under the capability id in a
    ``VectorIndex`` (see embeddings.py). Fitting mutates the backend, so
    pass a prebuilt index via ``find_capabilities_semantic``'s
    ``semantic_index`` argument to reuse it across queries.
    """
    from embeddings import VectorIndex
    if isinstance(capabilities, CapabilityIndex):
        caps = list(capabilities)
    else:
        caps = list(capabilities or [])
    docs = [(cap.id, _semantic_document(cap)) for cap in caps]
    backend.fit([doc for _, doc in docs])
    index = VectorIndex(backend)
    index.add_many(docs)
    return index


def find_capabilities_semantic(goal, index, backend, k=8, constraints=None,
                               semantic_index=None):
    """Rank capabilities fusing base signals with embedding similarity.

    Weighted sum per capability: ``base_score + _W_SEMANTIC * cosine``,
    where ``base_score`` is the standard keyword + input/output type +
    family + effect + reuse score and ``cosine`` is the embedding cosine
    between the goal text and the capability document (see
    :func:`build_semantic_index`). Hard family/effect filters and the
    ``score <= 0`` exclusion match :func:`find_capabilities`, and results
    are ``(capability, fused_score)`` tuples, highest first, top ``k``.

    ``index`` is a :class:`CapabilityIndex` (or None to read it from
    ``goal``/``constraints``, as :func:`find_capabilities` does) and
    ``backend`` is an ``EmbeddingBackend``. When ``semantic_index`` (a
    ``VectorIndex`` from :func:`build_semantic_index`) is omitted it is
    built on the fly, which refits the backend per call.
    """
    goal = _normalize_goal(goal)
    constraints = _normalize_constraints(constraints)
    if index is None:
        index = constraints.get("index", goal.get("index"))
    if index is None:
        raise ValueError("no capability index supplied "
                         "(pass index=... or index=... in goal or constraints)")
    if k is None or k <= 0:
        return []
    if semantic_index is None:
        semantic_index = build_semantic_index(index, backend)

    goal_text = goal.get("text", goal.get("intent", "")) or ""
    cosines = dict(semantic_index.top_k(goal_text, k=None)) if goal_text else {}

    scored = []
    for entry in index.entries():
        base = _score_entry(entry, goal, constraints)
        if base is None or base <= 0:
            continue
        fused = base + _W_SEMANTIC * cosines.get(entry.capability.id, 0.0)
        scored.append((entry.capability, fused))
    scored.sort(key=lambda pair: (-pair[1], pair[0].id))
    return scored[:k]
