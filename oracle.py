"""Oracle, failure-class and lesson helpers for the agent loop.

Pure Python: no model calls and no SBCL, so every rule here is unit-testable.

* Independent reference oracle: for well-known deterministic algorithms (FNV,
  CRC32, Adler-32, MD5, SHA) the expected value is computed by Python, never
  taken on trust from the model.
* Oracle confidence: how much an expected value can be trusted
  (``high`` = boolean/property or hand-computable, ``low`` = a number or hex
  string no model can compute by hand).
* Failure classes: ``COMPILER_ERROR``, ``RUNTIME_ERROR``, ``IMPLEMENTATION_WRONG``,
  ``TEST_WRONG``, ``AMBIGUOUS``, ``SPEC_INCONSISTENT``, ``REPEATED_CANDIDATE``.
* Lessons: recurring Lisp slips are counted on disk and fed back into prompts.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import zlib
from pathlib import Path

# ---------------------------------------------------------------- reference
_ARG1 = re.compile(r'^\(\s*[^\s()]+\s+"((?:[^"\\]|\\.)*)"\s*\)$')

#: (algorithm id, regex over name/description/prompt text); first match wins.
ALGOS = (
    ("fnv1a64", r"fnv[^a-z0-9]*(1a)?[^a-z0-9]*64|64[^a-z0-9]*bit[^.]*fnv"),
    ("fnv1a32", r"fnv"),
    ("crc32", r"crc-?32"),
    ("adler32", r"adler"),
    ("md5", r"\bmd5\b"),
    ("sha256", r"sha-?256"),
    ("sha1", r"\bsha-?1\b"),
)


def single_string_arg(call):
    """The string literal of a call like ``(f "abc")``, else ``None``."""
    m = _ARG1.match((call or "").strip())
    if not m:
        return None
    return m.group(1).replace('\\"', '"').replace("\\\\", "\\")


def detect_algo(*texts):
    blob = " ".join(t for t in texts if t).lower()
    for name, rx in ALGOS:
        if re.search(rx, blob):
            return name
    return None


def reference_value(algo, text):
    """Independent reference result: an int, or a lowercase hex string."""
    data = text.encode("utf-8")
    if algo == "fnv1a32":
        h = 2166136261
        for b in data:
            h = ((h ^ b) * 16777619) & 0xFFFFFFFF
        return h
    if algo == "fnv1a64":
        h = 14695981039346656037
        for b in data:
            h = ((h ^ b) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
        return h
    if algo == "crc32":
        return zlib.crc32(data) & 0xFFFFFFFF
    if algo == "adler32":
        return zlib.adler32(data) & 0xFFFFFFFF
    if algo in ("md5", "sha1", "sha256"):
        return hashlib.new(algo, data).hexdigest()
    raise ValueError("unknown algorithm %r" % (algo,))


def format_reference(value):
    return str(value) if isinstance(value, int) else '"%s"' % value


def matches_reference(got, ref):
    """Does the code's printed result ``got`` equal the reference value?"""
    got = (got or "").strip()
    if isinstance(ref, int):
        try:
            return int(got) == ref
        except ValueError:
            return False
    return got.strip('"').lower() == str(ref).lower()


# --------------------------------------------------------------- confidence
_LOW_NAMES = re.compile(r"hash|random|rand\b|crypt|digest|checksum|uuid|crc|"
                        r"salt|nonce|timestamp|now\b", re.I)


def oracle_confidence(call, expect):
    """``high`` / ``medium`` / ``low`` trust in an expected value."""
    e = (expect or "").strip()
    if e.upper() in ("T", "NIL"):
        return "high"                       # boolean or property test
    if re.fullmatch(r"-?\d{7,}(\.\d+)?", e):
        return "low"
    if re.fullmatch(r'"?[0-9A-Fa-f]{8,}"?', e):
        return "low"
    if re.fullmatch(r"-?\d+\.\d{6,}", e):
        return "low"
    if _LOW_NAMES.search(call or ""):
        return "low"
    if re.fullmatch(r"-?\d{1,6}", e) or len(e) <= 24:
        return "high"
    return "medium"


# ------------------------------------------------------------ failure class
_COMPILE = re.compile(r"compiled with errors|caught error|odd number of args|"
                      r"reader-error|unmatched close|end of file", re.I)


def is_compile_error(text):
    return bool(_COMPILE.search(text or ""))


def failure_class(infos, drift=None):
    """Classify one failed rehearsal.

    INFOS: one dict per failing test with ``error`` (str), ``got`` (str|None)
    and ``confidence``. DRIFT: calls whose expected value changed between
    attempts in this session.
    """
    if drift:
        return "AMBIGUOUS"
    if any(is_compile_error(i.get("error")) for i in infos):
        return "COMPILER_ERROR"
    wrong_value = [i for i in infos if i.get("got") is not None]
    if wrong_value and all(i.get("confidence") == "low" for i in wrong_value):
        return "TEST_WRONG"
    if wrong_value:
        return "IMPLEMENTATION_WRONG"
    return "RUNTIME_ERROR"


CLASS_LABELS = {
    "COMPILER_ERROR": "the code does not compile",
    "RUNTIME_ERROR": "the code crashed while running",
    "IMPLEMENTATION_WRONG": "the code returns a different value than expected",
    "TEST_WRONG": "the expected value looks like a guess the model could not compute",
    "AMBIGUOUS": "the expected values keep changing between attempts",
    "SPEC_INCONSISTENT": "the tests contradict each other or an earlier verified value",
    "REPEATED_CANDIDATE": "the new code is identical to an earlier failed attempt",
}


def norm_definition(text):
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def error_signature(detail):
    """Stable fingerprint of a failure: first error line, digits/names blurred."""
    first = (detail or "").split(";")[0].lower()
    first = re.sub(r"\(.*?\):", "", first)          # drop the failing call
    first = re.sub(r"\d+", "N", first)
    first = re.sub(r"variable \S+ is unbound", "variable X is unbound", first)
    return re.sub(r"\s+", " ", first).strip()[:120]


# ------------------------------------------------------------------ lessons
#: (key, regex over failure text, advice)
HINTS = (
    ("unbound", r"is unbound|undefined variable",
     "A variable is unbound. In LET all bindings are evaluated in parallel, so "
     "a later binding cannot use an earlier one: use LET* (or nested LETs). "
     "Check every variable is bound where it is used."),
    ("setf-pairs", r"odd number of args|macroexpansion of \(setf|\(place value-form",
     "SETF takes flat place/value pairs: (setf a 1 b 2). Do not wrap a later "
     "pair in parentheses like (setf a 1 (b 2)); write separate SETF forms or "
     "one flat SETF."),
    ("undefined-function", r"undefined function",
     "A function is undefined: define helpers before use or inline them."),
    ("arity", r"invalid number of arguments",
     "Wrong number of arguments: check the lambda list against how the test "
     "calls it."),
    ("not-list", r"is not of type\s+list|the value\s+\S+\s+is not of type\s+list",
     "A value was used as a list but is not one. Vectors, points and records "
     "are plain quoted lists like '(0 0 1); do not use #( ) arrays."),
)


def lisp_hints(detail):
    """``[(key, advice)]`` for the common Lisp slips visible in DETAIL."""
    d = detail or ""
    return [(k, adv) for k, rx, adv in HINTS if re.search(rx, d, re.I)]


class LessonStore:
    """Counts recurring failure causes on disk and turns them into advice."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._mem = {}

    def _read(self):
        if self.path is None:
            return dict(self._mem)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data):
        if self.path is None:
            self._mem = dict(data)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def note(self, keys):
        keys = [k for k in keys if k]
        if not keys:
            return
        with self._lock:
            data = self._read()
            for k in keys:
                data[k] = int(data.get(k, 0)) + 1
            self._write(data)

    def counts(self):
        return self._read()

    def advice(self, min_count=2, limit=4):
        """Advice text for slips that have recurred, most frequent first."""
        data = self._read()
        known = {k: adv for k, _, adv in HINTS}
        top = sorted(((n, k) for k, n in data.items()
                      if n >= min_count and k in known), reverse=True)[:limit]
        if not top:
            return ""
        return ("RECURRING SLIPS TO AVOID (seen in earlier runs): "
                + " ".join(known[k] for _, k in top))


# --------------------------------------------------------------- postmortem
def write_postmortem(directory, session_id, data):
    """Persist a failed session's postmortem as JSON; returns the path."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    data = dict(data, session=session_id, written=round(time.time(), 3))
    path = d / ("%s.json" % session_id)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return path
