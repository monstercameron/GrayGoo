"""Pluggable embedding backends + offline TF-IDF baseline (plan.md section 12).

This module provides semantic-intent retrieval without any downloads or
live API calls:

- :class:`EmbeddingBackend`: the protocol every backend implements:
  ``embed(texts) -> vectors``. A vector is either a sparse ``dict``
  mapping feature -> weight or a dense sequence of floats.
- :class:`TfIdfBackend`: deterministic pure-Python TF-IDF baseline.
  Fits IDF weights over a corpus, embeds texts as L2-normalized sparse
  vectors. Word unigrams plus character 3/4-grams give partial credit to
  morphological variants (``record``/``records``, ``parse``/``parsing``).
  Stdlib only, no downloads, fully deterministic.
- :class:`VectorIndex`: stores one vector per key plus ``top_k(query)``
  cosine search.
- :func:`cosine_similarity`: cosine over sparse or dense vectors.

Hosted embeddings later: a dense model (e.g. a Cerebras/Qwen embedding
endpoint) can implement the same protocol — ``embed(list[str])`` returning
one dense vector per text — and plug into :class:`VectorIndex` and
``retrieve.find_capabilities_semantic`` unchanged. Example::

    class HostedBackend:
        def embed(self, texts):
            return [self._remote_embed(t) for t in texts]  # dense lists

Out-of-vocabulary query features (never seen during ``fit``) are ignored,
matching ``sklearn`` ``TfidfVectorizer`` behavior. An unfitted backend or
an empty corpus therefore yields zero vectors and ``top_k`` returns
zero-score (never partial-match) results instead of raising.
"""

from __future__ import annotations

import math
import re
from typing import Protocol


_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "with",
    "into", "from", "by", "is", "are", "be", "as", "at", "it", "its",
})


def _word_tokens(text: str):
    """Lowercase alphanumeric tokens minus stopwords (deterministic order)."""
    if not text:
        return []
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def _features(text: str):
    """Word unigrams (``w:``) + char 3/4-grams (``c:``) for one text."""
    feats = []
    for tok in _word_tokens(text):
        feats.append("w:" + tok)
        for n in (3, 4):
            if len(tok) >= n:
                for i in range(len(tok) - n + 1):
                    feats.append("c:" + tok[i:i + n])
    return feats


class EmbeddingBackend(Protocol):
    """Protocol for embedding backends: ``embed(texts) -> vectors``.

    ``texts`` is a list of strings; the return is one vector per text in
    the same order. Each vector is either a sparse ``dict``
    (feature -> weight, as produced by :class:`TfIdfBackend`) or a dense
    sequence of floats (as a future hosted backend would return).
    """

    def embed(self, texts):
        """Embed ``texts``; return one vector per text, in order."""
        ...  # pragma: no cover - protocol stub


class TfIdfBackend(EmbeddingBackend):
    """Deterministic offline TF-IDF embedding baseline (stdlib only).

    Usage::

        backend = TfIdfBackend().fit(corpus_texts)
        vectors = backend.embed(["query text"])

    Term weight is sublinear TF (``1 + log(tf)``) times smoothed IDF
    (``log((1 + N) / (1 + df)) + 1``); vectors are L2-normalized, so the
    dot product equals cosine similarity. Keyword stuffing (repeating one
    term) is damped twice: sublinear TF caps repetition gains and a term
    shared across documents gets low IDF — a paraphrase sharing rare
    content terms outranks a decoy repeating common ones.
    """

    def __init__(self):
        self._idf: dict = {}
        self._n_docs = 0
        self._fitted = False

    @property
    def fitted(self) -> bool:
        """True once :meth:`fit` has run (even on an empty corpus)."""
        return self._fitted

    @property
    def n_docs(self) -> int:
        """Number of documents the IDF weights were fitted on."""
        return self._n_docs

    def fit(self, corpus):
        """Fit IDF weights over ``corpus`` (a list of texts); returns self.

        Fitting on an empty corpus is allowed: every later feature is
        out-of-vocabulary and embeds to the zero vector.
        """
        docs = list(corpus or [])
        self._n_docs = len(docs)
        doc_freq: dict = {}
        for doc in docs:
            for feat in set(_features(doc or "")):
                doc_freq[feat] = doc_freq.get(feat, 0) + 1
        n = self._n_docs
        self._idf = {term: math.log((1 + n) / (1 + df)) + 1.0
                     for term, df in doc_freq.items()}
        self._fitted = True
        return self

    def embed(self, texts):
        """Embed ``texts`` as L2-normalized sparse TF-IDF dicts.

        Accepts a list of strings (or a single string, wrapped). Terms
        unseen during :meth:`fit` are ignored; texts with no known terms
        (or any text when unfitted / fitted on nothing) embed to ``{}``.
        Feature iteration is sorted, so output is fully deterministic.
        """
        if isinstance(texts, str):
            texts = [texts]
        vectors = []
        for text in texts:
            counts: dict = {}
            for feat in _features(text or ""):
                if feat in self._idf:
                    counts[feat] = counts.get(feat, 0) + 1
            vec = {}
            for feat in sorted(counts):
                vec[feat] = (1.0 + math.log(counts[feat])) * self._idf[feat]
            norm = math.sqrt(sum(w * w for w in vec.values()))
            if norm > 0:
                vec = {feat: w / norm for feat, w in vec.items()}
            else:
                vec = {}
            vectors.append(vec)
        return vectors


def cosine_similarity(a, b) -> float:
    """Cosine similarity between two vectors (sparse dicts or dense seqs).

    Zero vectors score 0.0. Mixed sparse/dense pairs raise ``TypeError``;
    dense pairs of unequal length raise ``ValueError``.
    """
    if isinstance(a, dict) and isinstance(b, dict):
        if len(a) > len(b):
            a, b = b, a
        dot = sum(w * b.get(feat, 0.0) for feat, w in a.items())
        norm_a = math.sqrt(sum(w * w for w in a.values()))
        norm_b = math.sqrt(sum(w * w for w in b.values()))
    elif isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            raise ValueError("dense vectors must have equal length, got %d and %d"
                             % (len(a), len(b)))
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
    else:
        raise TypeError("cosine_similarity needs two sparse dicts or two dense "
                        "sequences, got %s and %s"
                        % (type(a).__name__, type(b).__name__))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class VectorIndex:
    """One stored vector per key plus ``top_k`` cosine search.

    Works with any :class:`EmbeddingBackend` implementation (sparse TF-IDF
    dicts or hosted dense vectors). Ranking is deterministic: ties break
    by ``str(key)`` ascending.
    """

    def __init__(self, backend):
        if not hasattr(backend, "embed"):
            raise TypeError("backend must implement embed(texts), got %r"
                            % type(backend).__name__)
        self._backend = backend
        self._vectors: dict = {}

    def add(self, key, text):
        """Embed ``text`` and store it under ``key`` (replaces existing)."""
        self._vectors[key] = self._backend.embed([text or ""])[0]
        return key

    def add_many(self, items):
        """Add an iterable of ``(key, text)`` pairs."""
        for key, text in items:
            self.add(key, text)

    def get_vector(self, key):
        """Return the stored vector for ``key`` (None when absent)."""
        return self._vectors.get(key)

    def __len__(self):
        return len(self._vectors)

    def __contains__(self, key):
        return key in self._vectors

    def top_k(self, query, k=8):
        """Rank stored vectors for ``query`` text; top ``k`` first.

        Returns ``(key, cosine)`` tuples, highest cosine first, ties by
        ``str(key)``. ``k=None`` returns all entries; ``k <= 0`` returns
        ``[]``. An empty index (or empty query) yields ``[]`` (or all-zero
        scores ranked by key) — never an exception.
        """
        if k is not None and k <= 0:
            return []
        query_vec = self._backend.embed([query or ""])[0]
        ranked = [(key, cosine_similarity(query_vec, vec))
                  for key, vec in self._vectors.items()]
        ranked.sort(key=lambda pair: (-pair[1], str(pair[0])))
        if k is None:
            return ranked
        return ranked[:k]
