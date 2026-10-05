"""Textual memory for Baseline B (Family A, text memory only).

Stores (task prompt -> successful output) pairs from Phase 1 exposure
successes as plain text, and retrieves the top-k entries for Phase 2
tasks by keyword overlap. Retrieval is fully offline (no model calls).

Entry shape::

    {
        "task_id": str,     # e.g. "A-EXP-05"
        "prompt": str,      # task instruction (system message)
        "category": str,    # dates | csv | records | logs
        "input": str,       # successful check input
        "output": str,      # successful check output (verified pass)
        "check": int,       # check index within the task
    }

Retrieval score: number of shared keywords between the query task's
prompt and the entry's prompt, plus a same-category bonus. A retrieval
"hit" means genuine keyword overlap > 0 (before the category bonus);
same-category entries with zero overlap are still returned (they are
related by family) but are NOT counted as hits.
"""

import json
import os
import re

STOPWORDS = frozenset(
    """
    a an the and or of to in on for with each line input output only
    json csv per one two all any are must that this from rows row
    values value data field fields object array string strings order
    same preserved mapping map only result results
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text):
    """Lowercase alphanumeric tokens, stopwords and short tokens removed."""
    if not isinstance(text, str):
        return []
    return [
        tok
        for tok in _TOKEN_RE.findall(text.lower())
        if len(tok) >= 3 and tok not in STOPWORDS
    ]


def keywords(text):
    """Keyword set for overlap scoring."""
    return set(tokenize(text))


CATEGORY_BONUS = 2


def score_entry(query_keywords, query_category, entry):
    """Return (overlap, score) for one memory entry.

    ``overlap`` is the raw shared-keyword count (hit signal);
    ``score`` adds the same-category bonus (ranking signal).
    """
    entry_keys = keywords(
        "%s %s" % (entry.get("prompt", ""), entry.get("category", ""))
    )
    overlap = len(query_keywords & entry_keys)
    bonus = CATEGORY_BONUS if entry.get("category") == query_category else 0
    return overlap, overlap + bonus


class TextMemory:
    """Ordered store of successful Phase 1 (prompt -> output) pairs."""

    def __init__(self):
        self.entries = []

    def __len__(self):
        return len(self.entries)

    def add_success(self, task, check_index, check_input, output):
        """Store one verified-successful check output. Returns the entry."""
        entry = {
            "task_id": task.get("id", "?"),
            "prompt": task.get("prompt", ""),
            "category": task.get("category", ""),
            "input": check_input,
            "output": output,
            "check": int(check_index),
        }
        self.entries.append(entry)
        return entry

    def retrieve(self, task, top_k=2):
        """Top-k entries for ``task`` ranked by keyword overlap.

        Returns a list of ``{"entry":..., "overlap":n, "score":n}`` sorted
        by score (desc), then overlap (desc), then insertion order.
        Entries with score <= 0 are excluded (no relation at all).
        """
        query_keys = keywords(
            "%s %s" % (task.get("prompt", ""), task.get("category", ""))
        )
        query_category = task.get("category", "")
        scored = []
        for pos, entry in enumerate(self.entries):
            overlap, score = score_entry(query_keys, query_category, entry)
            if score <= 0:
                continue
            scored.append((score, overlap, -pos, entry))
        scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
        return [
            {"entry": entry, "overlap": overlap, "score": score}
            for score, overlap, _, entry in scored[: max(0, top_k)]
        ]

    def format_block(self, retrieved, max_chars_per_example=320):
        """Format retrieved entries as a tiny prompt-injection block.

        Empty retrieval yields "" (caller then solves from scratch).
        Each example is truncated so Phase 2 contexts stay tiny.
        """
        if not retrieved:
            return ""
        lines = ["Related solved examples (adapt, do not copy blindly):"]
        for i, item in enumerate(retrieved, 1):
            entry = item["entry"]
            header = "Example %d (%s, %s):" % (
                i,
                entry.get("task_id", "?"),
                entry.get("category", "?"),
            )
            text = "%s\ninput: %s\noutput: %s" % (
                header,
                entry.get("input", ""),
                entry.get("output", ""),
            )
            if len(text) > max_chars_per_example:
                text = text[: max_chars_per_example - 1] + "…"
            lines.append(text)
        return "\n".join(lines) + "\n"

    # -- persistence -------------------------------------------------
    def to_json(self):
        return {"entries": self.entries}

    def save(self, path):
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_json(), fh, indent=2)
        return path

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        memory = cls()
        entries = data.get("entries", []) if isinstance(data, dict) else []
        memory.entries = [e for e in entries if isinstance(e, dict)]
        return memory
