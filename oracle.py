"""Oracle, failure-class and lesson helpers for the agent loop.

Pure Python: no model calls and no SBCL, so every rule here is unit-testable.

* Independent reference oracle: for well-known deterministic algorithms (FNV,
  CRC32, Adler-32, MD5, SHA) the expected value is computed by Python, never
  taken on trust from the model.
* Oracle confidence: how much an expected value can be trusted
  (``high`` = boolean/property or hand-computable, ``low`` = a number or hex
  string no model can compute by hand).
* Failure classes: ``COMPILER_ERROR``, ``RUNTIME_ERROR``, ``IMPLEMENTATION_WRONG``,
  ``TEST_WRONG``, ``TEST_CALL_INVALID``, ``AMBIGUOUS``, ``SPEC_INCONSISTENT``,
  ``REPEATED_CANDIDATE``.
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


def _norm_lisp(text):
    """Comparable form of Lisp text: case, whitespace, quotes and NIL/() unified.

    SBCL prints ``()`` as ``NIL`` and upper-cases symbols, so both sides are
    reduced to lower case with ``()`` as the only empty-list spelling.
    """
    s = (text or "").lower().replace("'", "")
    s = re.sub(r"\(\s*\)", " nil ", s)
    s = re.sub(r"(?<![\w*+%<>=/!?.&:-])nil(?![\w*+%<>=/!?.&:-])", "()", s)
    return re.sub(r"\s+", "", s)


def _has_word(word, text):
    """Whole-symbol match of WORD in TEXT (``insert`` is not in ``db-insert``)."""
    rx = r"(?<![\w*+%<>=/!?.&-])" + re.escape(word) + r"(?![\w*+%<>=/!?.&-])"
    return bool(re.search(rx, text or "", re.I))


_FORM = re.compile(r"Form:\s*(.+?)(?=\s+(?:Compile-time|Compilation|Execution|"
                   r"--- backtrace|Source form)|\Z)", re.I | re.S)
_UNDEF = re.compile(r"\bthe variable (\S+) is unbound|"
                    r"undefined (?:variable|function):?\s+(\S+)|"
                    r"\bthe function (\S+) is undefined", re.I)
_BAD_HEAD = re.compile(r"\((\(|\"|[-+]?\d)")


_NOT_LIST = re.compile(r"the value\s+(\"(?:[^\"\\]|\\.)*\"|[^\s]+)\s+is not of type\s+list", re.I)


def is_flat_data_error(info, calls=()):
    """True when a test passed FLAT data where the other tests pass NESTED data.

    The error is ``The value "users" is not of type LIST`` and that very value
    is the first element of a quoted argument in this call, written
    ``'("users" ...)``, while another test of the same tool writes its data as
    ``'((...``. The code expects a list of records and this one test dropped a
    level of parentheses.
    """
    m = _NOT_LIST.search(info.get("error") or "")
    call = info.get("call") or ""
    if not m or not call or m.group(1).startswith(":"):
        return False
    flat = re.search(r"'\(\s*" + re.escape(m.group(1)) + r"(?![^\s()])", call, re.I)
    nested_elsewhere = any(re.search(r"'\(\s*\(", c) for c in calls if c != call)
    return bool(flat) and nested_elsewhere


def is_test_call_error(info, definition=""):
    """True when this failed test's error comes from its CALL, not the definition.

    Three signals, each only when the offending text is absent from the
    definition: SBCL's ``Form:`` (normalised) appears inside the call; an
    ``illegal function call`` on a form whose head is a string, number or list;
    or an undefined variable/function named in the error that the call uses.
    """
    err = info.get("error") or ""
    call = info.get("call") or ""
    if not err or not call:
        return False
    defn_n = _norm_lisp(definition)
    m = _FORM.search(err)
    if m:
        form_n = _norm_lisp(m.group(1))
        if form_n and form_n not in defn_n:
            if form_n in _norm_lisp(call):
                return True
            if re.search(r"illegal function call", err, re.I) and \
                    _BAD_HEAD.match(form_n):
                return True
    u = _UNDEF.search(err)
    if u:
        name = (u.group(1) or u.group(2) or u.group(3)).rstrip(".,;").split(":")[-1].lower()
        if name and _has_word(name, call) and not _has_word(name, definition):
            return True
    return False


def failure_class(infos, drift=None, definition=None, calls=()):
    """Classify one failed rehearsal.

    INFOS: one dict per failing test with ``error`` (str), ``got`` (str|None),
    ``call`` (str) and ``confidence``. DRIFT: calls whose expected value changed
    between attempts in this session. DEFINITION: the code under test, used to
    tell a broken test call from a broken definition.
    """
    if drift:
        return "AMBIGUOUS"
    if any(is_test_call_error(i, definition or "") for i in infos):
        return "TEST_CALL_INVALID"
    # only when every failure is this slip: a real bug elsewhere outranks it
    if infos and all(is_flat_data_error(i, calls or ()) for i in infos):
        return "TEST_CALL_INVALID"
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
    "TEST_CALL_INVALID": "a test call is not valid Lisp (the definition itself is fine)",
    "REGRESSION": "the change breaks a tool that was already working",
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
    ("undefined-function", r"undefined function|the function (?!:)\S+ is undefined",
     "A function is undefined. If its name is one of your own LET/LET* "
     "variables, the binding list was closed one paren too early, so the next "
     "binding (name value) was read as a call: all bindings go inside ONE "
     "list, (let* ((a 1) (b 2)) body). Otherwise define the helper before use "
     "or inline it."),
    ("keyword-call", r"the function :\S+ is undefined",
     "A plist was written as code: (:output text) calls a function named "
     "OUTPUT. Build a plist with (list :output text :state state), never "
     "(:output text); a keyword is data, not a function."),
    ("key-arg", r"unknown &key argument",
     "A function was given a keyword it does not accept. STRING=, STRING-EQUAL, "
     "CHAR= and EQUAL take NO :TEST; :TEST belongs to ASSOC, FIND, MEMBER, "
     "REMOVE, POSITION and COUNT, e.g. (assoc key table :test #'string=)."),
    ("plist-as-alist", r"the value :\S+ is not of type\s+list",
     "A KEYWORD was used as a list: the code called ASSOC (or CAR/FIRST) on a "
     "plist. A plist such as the REQUEST is read with (getf plist :key), or "
     "with the kit tools (cookie-value request \"sid\"), (form-value request "
     "\"title\"), (request-field request :path). Never ASSOC a plist."),
    ("string-of-strings", r"is not of type character when setting an element",
     "CONCATENATE 'STRING was given a LIST of strings as one argument, so it "
     "tried to store whole strings as characters. Join a list of strings with "
     "(apply #'concatenate 'string list-of-strings) or "
     "(format nil \"~{~a~}\" list-of-strings); pass separate strings as "
     "separate arguments."),
    ("paren-balance", r"unbalanced delimiter|unterminated '\('|unexpected '\)'",
     "The parentheses of the definition do not balance. Keep the function "
     "short, close every LET binding list before the body, and count: each "
     "opening parenthesis needs exactly one closing one."),
    ("one-form", r"trailing content after first s-expression|expected a single form",
     "A tool is exactly ONE defun. Do not send a helper defun next to it: make "
     "the helper its own tool first, or define it inside with FLET or LABELS."),
    ("constant-name", r"names a defined constant",
     "T and NIL are constants and cannot be variable or parameter names. "
     "Rename the variable: (lambda (task) ...) or (lambda (item) ...), never "
     "(lambda (t) ...)."),
    ("loop-collect", r"function \S*collect is undefined",
     "COLLECT is a LOOP keyword, not a function: write (loop for x in xs "
     "collect (f x)), never (collect ...) inside DO. To join strings with a "
     "separator use (format nil \"~{~a~^~%~}\" list)."),
    ("arity", r"invalid number of arguments",
     "Wrong number of arguments: check the lambda list against how the test "
     "calls it."),
    ("not-list", r"is not of type\s+list|the value\s+\S+\s+is not of type\s+list",
     "A value was used as a list but is not one. Vectors, points and records "
     "are plain quoted lists like '(0 0 1); do not use #( ) arrays. If the "
     "error names a piece of your TEST data, the data has the wrong nesting: "
     "copy the argument shape from the e.g. call of the REGISTRY tool that "
     "receives it (a table list is '((\"name\" (rows...))), with two opening "
     "parentheses)."),
)


_GOT = re.compile(r"got (.+?), expected (.+?)(?:;|$)", re.S)
WRAPPED = ("extra-nesting",
           "The result has one level of parentheses too many (or too few). In a "
           "(key value) pair the value is (SECOND pair) or (CADR pair); (CDR pair) "
           "is the LIST (value). A table is (name rows): its rows are "
           "(second table), and (table-rows state name) returns them directly.")


def _one_level_off(detail):
    """True when a mismatch differs only by one wrapping pair of parentheses."""
    norm = lambda x: re.sub(r"\s+", " ", x.strip()).lower().replace("nil", "()")
    for got, want in _GOT.findall(detail or ""):
        g, w = norm(got), norm(want)
        if g == "(%s)" % w or w == "(%s)" % g:
            return True
    return False


def lisp_hints(detail):
    """``[(key, advice)]`` for the common Lisp slips visible in DETAIL."""
    d = detail or ""
    found = [(k, adv) for k, rx, adv in HINTS if re.search(rx, d, re.I)]
    if _one_level_off(d):
        found.append(WRAPPED)
    return found


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

    # -- detail ledger ---------------------------------------------------
    # ``lessons.json`` keeps the plain counts. The ledger beside it records what
    # the counts cannot: where a slip happens, whether warning about it works,
    # which errors have no lesson yet, and what the harness had to fix itself.
    def _detail_path(self):
        return self.path.with_name(self.path.stem + ".detail.json") if self.path else None

    def _read_detail(self):
        blank = {"lessons": {}, "signatures": {}, "fixes": {}, "harness": {}}
        if self.path is None:
            return json.loads(json.dumps(getattr(self, "_detail_mem", blank)))
        try:
            data = json.loads(self._detail_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return blank
        if not isinstance(data, dict):
            return blank
        for k, v in blank.items():
            if not isinstance(data.get(k), dict):
                data[k] = v
        return data

    def _write_detail(self, data):
        if self.path is None:
            self._detail_mem = data
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._detail_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self._detail_path())

    @staticmethod
    def _entry(detail, key):
        return detail["lessons"].setdefault(
            key, {"projects": {}, "shown": 0, "recurred": 0})

    def record(self, keys, signature="", detail="", project=None, warned=()):
        """One failed attempt.

        KEYS are the known slips in it (they are counted, per project too).
        A slip that was WARNED about earlier in the same run counts as a
        recurrence, which is how we learn that a warning does not work. A
        failure with no known slip is remembered by SIGNATURE so that errors
        nobody has written a lesson for become visible once they repeat.
        """
        keys = [k for k in dict.fromkeys(keys) if k]
        self.note(keys)
        with self._lock:
            data = self._read_detail()
            for k in keys:
                entry = self._entry(data, k)
                if project:
                    entry["projects"][project] = entry["projects"].get(project, 0) + 1
                if k in warned:
                    entry["recurred"] += 1
            if not keys and signature:
                sig = data["signatures"].setdefault(
                    signature, {"count": 0, "example": "", "projects": {}})
                sig["count"] += 1
                sig["example"] = (detail or sig["example"])[:300]
                if project:
                    sig["projects"][project] = sig["projects"].get(project, 0) + 1
            self._write_detail(data)

    def mark_shown(self, keys):
        """KEYS were put in front of the model (call once per run per key)."""
        keys = [k for k in dict.fromkeys(keys) if k]
        if not keys:
            return
        with self._lock:
            data = self._read_detail()
            for k in keys:
                self._entry(data, k)["shown"] += 1
            self._write_detail(data)

    def record_fixes(self, names):
        """The harness silently repaired these model slips."""
        self._bump("fixes", names)

    def record_harness(self, event):
        """A harness-level event worth counting (bad JSON, repeated code, ...)."""
        self._bump("harness", [event])

    def _bump(self, section, names):
        names = [n for n in names if n]
        if not names:
            return
        with self._lock:
            data = self._read_detail()
            for n in names:
                data[section][n] = data[section].get(n, 0) + 1
            self._write_detail(data)

    def ineffective(self, min_shown=4, rate=0.5):
        """Lessons that keep recurring in runs where they were already shown."""
        lessons = self._read_detail()["lessons"]
        return sorted(k for k, e in lessons.items()
                      if e["shown"] >= min_shown and e["recurred"] >= rate * e["shown"])

    def unhandled(self, min_count=3):
        """Repeated failures that match no lesson: ``[(signature, count, example)]``."""
        sigs = self._read_detail()["signatures"]
        return sorted(((s, e["count"], e["example"]) for s, e in sigs.items()
                       if e["count"] >= min_count), key=lambda r: -r[1])

    def select(self, project=None, session_keys=(), min_count=2, limit=2):
        """Which lessons to show now, most relevant first.

        Slips already made in THIS run come first, then the ones most frequent
        in this project, then the globally frequent ones.
        """
        counts = self._read()
        detail = self._read_detail()["lessons"]
        known = {k for k, _, _ in HINTS} | {WRAPPED[0]}
        session = [k for k in dict.fromkeys(session_keys) if k in known]

        def rank(k):
            return (-(detail.get(k, {}).get("projects", {}).get(project, 0) if project else 0),
                    -int(counts.get(k, 0)), k)
        rest = sorted((k for k, n in counts.items()
                       if k in known and k not in session and int(n) >= min_count), key=rank)
        return (session + rest)[:max(limit, len(session[:limit]))][:limit]

    def advice(self, min_count=2, limit=4, brief=False, project=None, session_keys=()):
        """Advice text for the selected lessons.

        ``brief`` keeps the first sentence of each lesson, except for lessons
        this run already tripped over and lessons known to be ineffective when
        brief: those are given in full, because the short form did not work.
        """
        keys = self.select(project, session_keys, min_count, limit)
        if not keys:
            return ""
        known = dict({k: adv for k, _, adv in HINTS}, **{WRAPPED[0]: WRAPPED[1]})
        weak = set(self.ineffective())
        again = [k for k in keys if k in session_keys]
        other = [k for k in keys if k not in session_keys]

        def text(k):
            if not brief or k in weak or k in again:
                return known[k]
            first = re.split(r"(?<=[.:])\s", known[k], 1)[0]
            return first if first.endswith(".") else first.rstrip(":") + "."
        parts = []
        if again:
            parts.append("YOU ALREADY MADE THESE MISTAKES IN THIS RUN - do not repeat "
                         "them: " + " ".join(text(k) for k in again))
        if other:
            parts.append("RECURRING SLIPS TO AVOID (seen in earlier runs): "
                         + " ".join(text(k) for k in other))
        return "\n".join(parts)

    def report(self):
        """Everything the layer has learned, for a person deciding what to fix."""
        counts = self._read()
        detail = self._read_detail()
        lessons = []
        for k, n in sorted(counts.items(), key=lambda kv: -int(kv[1])):
            e = detail["lessons"].get(k, {})
            shown = e.get("shown", 0)
            lessons.append({"lesson": k, "count": int(n), "shown_in_runs": shown,
                            "recurred_after_shown": e.get("recurred", 0),
                            "recurrence_rate": round(e.get("recurred", 0) / shown, 2)
                            if shown else None,
                            "projects": e.get("projects", {})})
        return {"lessons": lessons, "ineffective": self.ineffective(),
                "unhandled_errors": [{"signature": s, "count": c, "example": x}
                                     for s, c, x in self.unhandled()],
                "harness_fixes": dict(sorted(detail["fixes"].items(), key=lambda kv: -kv[1])),
                "harness_events": dict(sorted(detail["harness"].items(), key=lambda kv: -kv[1]))}


# --------------------------------------------------------------- postmortem
def write_postmortem(directory, session_id, data):
    """Persist a failed session's postmortem as JSON; returns the path."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    data = dict(data, session=session_id, written=round(time.time(), 3))
    path = d / ("%s.json" % session_id)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return path
