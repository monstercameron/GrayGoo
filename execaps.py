"""Executable capability registry: reuse WITHOUT model calls (thesis arm C/D).

A ``Capability`` here pairs a :mod:`retrieve` descriptor (intent, types,
effects, reuse history) with an executable ``fn`` (str -> str) plus a
``precondition`` (str -> bool) that verifies the capability FITS a given
input before it runs. Execution never touches the model: a task solved
on this path costs zero calls and zero tokens.

Applicability (harmful-reuse guard): :meth:`ExecRegistry.find_for_task`
requires ALL of category match, prompt-signature overlap, and
per-input precondition before a capability may execute. A same-category
capability with the wrong procedure (e.g. comma-CSV parser offered a
semicolon input) must NOT fire -- it falls back to the model path.

Seed capabilities are verified implementations of four Family A
exposure procedures (one per category) plus one verified inverse
(cap-table-csv). In production these would be
LLM-synthesized then verified; here they are hand-written and proven
against the exposure checks by :func:`verify_capability`, so the
C/D harness measures reuse mechanics, not synthesis quality.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import retrieve  # noqa: E402

_BENCH = str(Path(__file__).resolve().parent / "benchmarks")
if _BENCH not in sys.path:
    sys.path.insert(0, _BENCH)

try:
    from runner import compare as _runner_compare  # noqa: E402
except Exception:  # offline fallback mirrors runner.compare exact/json
    def _runner_compare(expected, actual, mode):
        if mode == "exact":
            if not isinstance(actual, str) or not isinstance(expected, str):
                return False
            return actual.strip("\n") == expected.strip("\n")
        if mode == "json":
            try:
                return json.loads(actual) == json.loads(expected)
            except (ValueError, TypeError):
                return False
        raise ValueError("unknown compare mode: %r" % (mode,))


def compare(expected, actual, mode):
    """runner.compare (exact/json); shared so execution matches scoring."""
    return _runner_compare(expected, actual, mode)


# ---------------------------------------------------------------------------
# Seed capability implementations (verified against exposure checks)
# ---------------------------------------------------------------------------

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7,
    "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12,
    "dec": 12,
}

_RE_D_MON_Y = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$")
_RE_MON_D_Y = re.compile(r"^([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})$")
_RE_SLASH = re.compile(r"^(\d{4})/(\d{2})/(\d{2})$")
_RE_DOT = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")


def _parse_one_date(line):
    text = line.strip()
    match = _RE_D_MON_Y.match(text)
    if match:
        day, mon, year = match.groups()
        month = _MONTHS.get(mon.lower())
        if month:
            return "%s-%02d-%02d" % (year, month, int(day))
        return None
    match = _RE_MON_D_Y.match(text)
    if match:
        mon, day, year = match.groups()
        month = _MONTHS.get(mon.lower())
        if month:
            return "%s-%02d-%02d" % (year, month, int(day))
        return None
    match = _RE_SLASH.match(text)
    if match:
        year, month, day = match.groups()
        return "%s-%s-%s" % (year, month, day)
    match = _RE_DOT.match(text)
    if match:
        day, month, year = match.groups()
        return "%s-%s-%s" % (year, month, day)
    return None


def fn_date_iso(text):
    """A-EXP-01 procedure: each line -> ISO YYYY-MM-DD."""
    return "\n".join(_parse_one_date(line) or "" for line in text.split("\n"))


def pre_date_iso(text):
    """Every non-empty line matches one of the four supported formats."""
    lines = [line for line in text.split("\n") if line.strip()]
    return bool(lines) and all(_parse_one_date(line) is not None
                               for line in lines)


def fn_csv_parse(text):
    """A-EXP-05 procedure: RFC 4180 CSV -> JSON array of string-valued rows."""
    reader = csv.DictReader(io.StringIO(text))
    rows = [{key: (value if value is not None else "")
             for key, value in row.items()} for row in reader]
    return json.dumps(rows)


def pre_csv_parse(text):
    """Header row exists, contains a comma, rows parse with stable width."""
    if "\n" not in text and "\r" not in text:
        return False
    lines = text.splitlines()
    if not lines or "," not in lines[0]:
        return False
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        return False
    if len(rows) < 2:
        return False
    width = len(rows[0])
    return width >= 2 and all(len(row) == width for row in rows)


def _flatten(value, prefix, out):
    if isinstance(value, dict):
        if not value:
            out[prefix] = {}
            return
        for key in value:
            path = "%s.%s" % (prefix, key) if prefix else str(key)
            _flatten(value[key], path, out)
    elif isinstance(value, list):
        if not value:
            out[prefix] = []
            return
        for i, item in enumerate(value):
            _flatten(item, "%s[%d]" % (prefix, i), out)
    else:
        out[prefix] = value


def fn_flatten(text):
    """A-EXP-09 procedure: nested JSON object -> flat JSON object."""
    data = json.loads(text)
    out = {}
    _flatten(data, "", out)
    return json.dumps(out)


def pre_flatten(text):
    """Input is a single JSON object (not array/scalar)."""
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


_RE_CLF = re.compile(
    r'^(\S+) (\S+) (\S+) \[([^\]]*)\] "([A-Z]+) (\S+) ([^"]*)" (\d{3}) (\S+)$')


def fn_clf_parse(text):
    """A-EXP-13 procedure: one CLF line -> one JSON object."""
    match = _RE_CLF.match(text.strip())
    host, _ident, user, time, method, path, _proto, status, byts = \
        match.groups()
    return json.dumps({
        "host": host,
        "user": None if user == "-" else user,
        "time": time,
        "method": method,
        "path": path,
        "status": int(status),
        "bytes": 0 if byts == "-" else int(byts),
    })


def pre_clf_parse(text):
    """Input is exactly one CLF-shaped line."""
    return _RE_CLF.match(text.strip()) is not None


# ---------------------------------------------------------------------------
# API-workflow primitives (Family W exposure procedures, directive §4)
# ---------------------------------------------------------------------------

def fn_paginate(text):
    """Collect "items" across paged JSON lines into one JSON array."""
    out = []
    for line in text.split("\n"):
        if line.strip():
            out.extend(json.loads(line)["items"])
    return json.dumps(out)


def pre_paginate(text):
    """Every non-empty line is {"items": [...]} (paged-text)."""
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return False
    try:
        return all(isinstance(json.loads(line), dict)
                   and isinstance(json.loads(line).get("items"), list)
                   for line in lines)
    except ValueError:
        return False


def fn_retry_schedule(text):
    """Map a retry spec to its next wait + remaining budget."""
    spec = json.loads(text)
    attempts = spec["attempts"]
    left = max(0, spec["max"] - attempts)
    wait = spec["base_ms"] * (2 ** attempts) if left > 0 else None
    return json.dumps({"next_wait_ms": wait, "retries_left": left})


def pre_retry_schedule(text):
    """Input is {"attempts": n>=0, "max": m>=1, "base_ms": b>=0}."""
    try:
        spec = json.loads(text)
    except ValueError:
        return False
    if not isinstance(spec, dict):
        return False
    try:
        attempts = spec["attempts"]
        maximum = spec["max"]
        base = spec["base_ms"]
    except KeyError:
        return False
    return (isinstance(attempts, int) and isinstance(maximum, int)
            and isinstance(base, int) and attempts >= 0
            and maximum >= 1 and base >= 0)


def fn_ratelimit(text):
    """Fixed-window allow/deny verdicts for ascending hit timestamps."""
    spec = json.loads(text)
    limit = spec["limit"]
    window = spec["window_s"]
    start = spec["hits"][0] if spec["hits"] else 0
    counts = {}
    out = []
    for hit in spec["hits"]:
        key = int((hit - start) // window)
        used = counts.get(key, 0)
        out.append("allow" if used < limit else "deny")
        counts[key] = used + 1
    return json.dumps(out)


def pre_ratelimit(text):
    """Input is {"limit": n>=1, "window_s": w>0, "hits": asc numbers}."""
    try:
        spec = json.loads(text)
    except ValueError:
        return False
    if not isinstance(spec, dict):
        return False
    limit = spec.get("limit")
    window = spec.get("window_s")
    hits = spec.get("hits")
    if not (isinstance(limit, int) and limit >= 1):
        return False
    if not (isinstance(window, (int, float))
            and not isinstance(window, bool) and window > 0):
        return False
    if not isinstance(hits, list):
        return False
    if not all(isinstance(h, (int, float)) and not isinstance(h, bool)
               for h in hits):
        return False
    return all(b >= a for a, b in zip(hits, hits[1:]))


def fn_auth(text):
    """Token freshness verdict: "ok" or "refresh"."""
    spec = json.loads(text)
    if spec["now"] + spec["margin_s"] >= spec["expires_at"]:
        return "refresh"
    return "ok"


def pre_auth(text):
    """Input is {"expires_at", "now", "margin_s" >= 0} numbers."""
    try:
        spec = json.loads(text)
    except ValueError:
        return False
    if not isinstance(spec, dict):
        return False
    try:
        expires = spec["expires_at"]
        now = spec["now"]
        margin = spec["margin_s"]
    except KeyError:
        return False
    nums = all(isinstance(v, (int, float)) and not isinstance(v, bool)
               for v in (expires, now, margin))
    return nums and margin >= 0


def fn_normalize(text):
    """Strip string values; lowercase email fields (JSON array)."""
    rows = json.loads(text)
    out = []
    for row in rows:
        clean = {}
        for key, value in row.items():
            if isinstance(value, str):
                value = value.strip()
                if key == "email":
                    value = value.lower()
            clean[key] = value
        out.append(clean)
    return json.dumps(out)


def pre_normalize(text):
    """Input is a JSON array of objects."""
    try:
        rows = json.loads(text)
    except ValueError:
        return False
    return isinstance(rows, list) and all(
        isinstance(row, dict) for row in rows)


def fn_dedup(text):
    """Drop exact-duplicate elements, keep first occurrence order."""
    rows = json.loads(text)
    seen = set()
    out = []
    for row in rows:
        key = json.dumps(row, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return json.dumps(out)


def pre_dedup(text):
    """Input is a JSON array."""
    try:
        return isinstance(json.loads(text), list)
    except ValueError:
        return False


def fn_cache(text):
    """Cache lookup: value JSON on hit, "MISS" otherwise."""
    spec = json.loads(text)
    if spec["key"] in spec["entries"]:
        return json.dumps(spec["entries"][spec["key"]])
    return "MISS"


def pre_cache(text):
    """Input is {"entries": {...}, "key": str}."""
    try:
        spec = json.loads(text)
    except ValueError:
        return False
    return (isinstance(spec, dict)
            and isinstance(spec.get("entries"), dict)
            and isinstance(spec.get("key"), str))


def fn_validate(text):
    """Required-field check: "ok" or "missing:a,b" (sorted)."""
    spec = json.loads(text)
    absent = sorted(k for k in spec["required"]
                    if k not in spec["record"])
    if not absent:
        return "ok"
    return "missing:" + ",".join(absent)


def pre_validate(text):
    """Input is {"required": [str], "record": {...}}."""
    try:
        spec = json.loads(text)
    except ValueError:
        return False
    return (isinstance(spec, dict)
            and isinstance(spec.get("required"), list)
            and all(isinstance(k, str) for k in spec["required"])
            and isinstance(spec.get("record"), dict))


# ---------------------------------------------------------------------------
# Composition glue (tiny verified plumbing between reused capabilities)
# ---------------------------------------------------------------------------

def fn_table_csv(text):
    """JSON array of objects -> RFC 4180 CSV text (header + rows)."""
    rows = json.loads(text)
    if not rows:
        return ""
    header = list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in header})
    return buf.getvalue().rstrip("\n")


def pre_table_csv(text):
    """Input is a non-empty JSON array of flat string-valued objects."""
    try:
        rows = json.loads(text)
    except ValueError:
        return False
    if not isinstance(rows, list) or not rows:
        return False
    if not all(isinstance(row, dict) for row in rows):
        return False
    return all(isinstance(value, (str, int, float, bool, type(None)))
               for row in rows for value in row.values())


def glue_flat_to_rows(flat_json):
    """Thin plumbing: flat JSON object -> JSON array of key/value rows."""
    data = json.loads(flat_json)
    rows = []
    for key in data:
        value = data[key]
        if not isinstance(value, str):
            value = json.dumps(value)
        rows.append({"key": key, "value": value})
    return json.dumps(rows)


# Typed glue adapters for composition search: name -> (in, out, fn).
# Search may only bridge a type gap with glue whose in/out types link
# the two neighboring steps; untyped plumbing is not searchable.
GLUE_SPECS = {
    "flat_to_rows": (("flat-json-object",), ("json-array",),
                     glue_flat_to_rows),
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ExecCapability:
    """One executable capability: descriptor + fn + fit checks."""

    def __init__(self, cap_id, intent, category, input_types, output_types,
                 fn, precondition, prompt_keywords, source_task,
                 veto_words=(), params=None):
        self.descriptor = retrieve.Capability(
            id=cap_id, intent=intent, input_types=input_types,
            output_types=output_types, effects=(), family="A")
        self.category = category
        self.fn = fn
        self.precondition = precondition
        self.prompt_keywords = frozenset(prompt_keywords)
        self.source_task = source_task
        # Discriminative veto words (mined by distill.consolidate from
        # harm pairs, never hand-written): prompt vocabulary that
        # proves a DIFFERENT procedure is needed (e.g. "null" vetoes
        # the empty-string csv parser). Veto overrides all positive
        # evidence -- false-positive reuse is worse than a miss.
        self.veto_words = frozenset(veto_words)
        # Optional prompt-bound parameter (sibling-collapse
        # abstractions): {"name", "default", "alt", "alt_triggers"}.
        # When alt-trigger words appear in the task prompt the alt
        # value binds, else the default. Unparameterized capabilities
        # (params None) ignore binding entirely.
        self.params = params
        # Reuse evidence (directive §1-2): positive/negative task ids
        # from trusted scoring, never from model claims. Updated by
        # the driver after verification, not by the capability itself.
        self.positive = []
        self.negative = []

    @property
    def id(self):
        return self.descriptor.id

    def prompt_overlap(self, task):
        """Fraction of signature keywords present in the task prompt."""
        return sig_overlap(self.prompt_keywords, _prompt_words(task))

    def prompt_vetoed(self, task):
        """True when veto vocabulary appears in the task prompt."""
        if not self.veto_words:
            return False
        words = _prompt_words(task)
        return any(
            sig_word_hits((veto,), words)[veto] is not None
            for veto in self.veto_words)

    def applies_to(self, task, check_input, min_overlap=0.5):
        """True only when category, prompt, AND input precondition agree."""
        if task.get("category") != self.category:
            return False
        if self.prompt_vetoed(task):
            return False
        if self.prompt_overlap(task) < min_overlap:
            return False
        try:
            return bool(self.precondition(check_input))
        except Exception:
            return False

    def bind(self, task):
        """Bind the prompt-bound parameter for a task (default: default).

        Returns None for unparameterized capabilities. Binding is a
        pure function of the task prompt -- no shared mutable state,
        so the same capability serves both siblings interleaved.
        """
        if not self.params:
            return None
        triggers = self.params.get("alt_triggers", ())
        if triggers:
            words = _prompt_words(task)
            hits = sig_word_hits(tuple(triggers), words)
            if any(hit is not None for hit in hits.values()):
                return self.params.get("alt")
        return self.params.get("default")

    def execute(self, check_input, task=None):
        """Run the capability (pure computation, never the model).

        Parameterized capabilities bind from ``task`` (default value
        when no task is given, preserving every existing call site).
        """
        bound = self.bind(task) if task is not None else None
        if bound is None and self.params:
            bound = self.params.get("default")
        if bound is None:
            return self.fn(check_input)
        return self.fn(check_input, bound)

    def record(self, task_id, helped):
        """Append trusted reuse evidence (driver calls post-scoring)."""
        target = self.positive if helped else self.negative
        if task_id not in target:
            target.append(task_id)

    def evidence(self):
        """Evidence envelope for promotion accounting (§9)."""
        return {"positive": list(self.positive),
                "negative": list(self.negative),
                "reuse_count": len(self.positive) + len(self.negative)}


def seed_capabilities():
    """The thirteen verified seed capabilities.

    Four mirror Family A exposure procedures (one per category); the
    fifth (cap-table-csv) is the verified inverse of cap-csv-parse
    with its own contract checks (see SYNTHETIC_CHECKS below); eight
    more implement the Family W API-workflow primitives (directive
    §4), each verified against its W-EXP exposure task.
    """
    return [
        ExecCapability(
            "cap-date-iso", "convert dates to ISO YYYY-MM-DD format",
            "dates", ("date-text",), ("iso-date-text",),
            fn_date_iso, pre_date_iso,
            ("convert", "date", "iso", "format", "line", "order"),
            "A-EXP-01"),
        ExecCapability(
            "cap-csv-parse", "parse RFC 4180 CSV into JSON array of objects",
            "csv", ("csv-text",), ("json-array",),
            fn_csv_parse, pre_csv_parse,
            ("parse", "csv", "header", "json", "array", "objects"),
            "A-EXP-05"),
        ExecCapability(
            "cap-flatten", "flatten nested JSON object to dot-separated keys",
            "records", ("json-object",), ("flat-json-object",),
            fn_flatten, pre_flatten,
            ("flatten", "json", "object", "nested", "keys", "output"),
            "A-EXP-09"),
        ExecCapability(
            "cap-clf-parse", "parse Common Log Format line into JSON object",
            "logs", ("clf-text",), ("json-object",),
            fn_clf_parse, pre_clf_parse,
            ("parse", "common", "log", "format", "output", "json"),
            "A-EXP-13"),
        ExecCapability(
            "cap-table-csv", "serialize JSON array of objects to CSV text",
            "csv", ("json-array",), ("csv-text",),
            fn_table_csv, pre_table_csv,
            ("serialize", "json", "array", "csv", "header", "rows"),
            "SYNTHETIC:table-csv"),
        ExecCapability(
            "cap-paginate", "collect items across paged JSON lines",
            "api", ("paged-text",), ("json-array",),
            fn_paginate, pre_paginate,
            ("paginate", "pages", "cursor", "items", "collect", "next"),
            "W-EXP-01"),
        ExecCapability(
            "cap-retry-schedule", "map a retry spec to next wait + budget",
            "api", ("retry-spec",), ("schedule-json",),
            fn_retry_schedule, pre_retry_schedule,
            ("retry", "backoff", "attempts", "wait", "schedule",
             "exponential"),
            "W-EXP-02"),
        ExecCapability(
            "cap-ratelimit", "fixed-window allow/deny verdicts per hit",
            "api", ("rate-spec",), ("verdict-array",),
            fn_ratelimit, pre_ratelimit,
            ("ratelimit", "throttle", "window", "quota", "hits",
             "allowance"),
            "W-EXP-03"),
        ExecCapability(
            "cap-auth", "token freshness verdict ok/refresh",
            "api", ("auth-spec",), ("verdict-text",),
            fn_auth, pre_auth,
            ("auth", "token", "refresh", "expiry", "margin", "session"),
            "W-EXP-04"),
        ExecCapability(
            "cap-normalize", "strip strings and lowercase emails",
            "api", ("json-array",), ("json-array",),
            fn_normalize, pre_normalize,
            ("normalize", "strip", "whitespace", "lowercase", "email",
             "trim"),
            "W-EXP-05"),
        ExecCapability(
            "cap-dedup", "drop exact-duplicate array elements",
            "api", ("json-array",), ("json-array",),
            fn_dedup, pre_dedup,
            ("dedup", "duplicate", "rows", "exact", "identical",
             "whole"),
            "W-EXP-06"),
        ExecCapability(
            "cap-cache", "cache lookup with MISS default",
            "api", ("cache-spec",), ("verdict-text",),
            fn_cache, pre_cache,
            ("cache", "lookup", "entries", "hit", "miss", "stored"),
            "W-EXP-07"),
        ExecCapability(
            "cap-validate", "required-field check ok/missing",
            "api", ("validate-spec",), ("verdict-text",),
            fn_validate, pre_validate,
            ("validate", "schema", "required", "missing", "record",
             "fields"),
            "W-EXP-08"),
    ]


# Contract checks for the synthetic cap-table-csv capability (its own
# task, since no exposure task covers JSON-array -> CSV serialization).
SYNTHETIC_CHECKS = {
    "cap-table-csv": [
        {"input": '[{"a": "x,y", "b": "p"}]',
         "expected": 'a,b\n"x,y",p',
         "compare": "exact"},
        {"input": '[{"key": "a.b", "value": "1"}, '
                  '{"key": "c", "value": "x"}]',
         "expected": 'key,value\na.b,1\nc,x',
         "compare": "exact"},
    ],
}

# Full source task for distilling the table-csv inverse (distill.py
# --include-synthetic-table). Prompt carries the signature words the
# hand seed uses, so learned replacements stay behavior-compatible.
TABLE_CSV_TASK = {
    "id": "SYNTHETIC:table-csv",
    "family": "A",
    "split": "exposure",
    "category": "csv",
    "prompt": ("Serialize the input JSON array of objects as CSV text "
               "(RFC 4180: comma delimiter, double-quote quoting, first "
               "row is the header with the first object's keys; one row "
               "per object, values in header order). Output only the CSV."),
    "checks": SYNTHETIC_CHECKS["cap-table-csv"],
    "notes": "Synthetic exposure contract for the table-csv inverse.",
}


# ---------------------------------------------------------------------------
# Deterministic composition search (directive §3, order item 4)
#
# Plans are DISCOVERED, not registered: search enumerates small plans
# over the capability library, validates type/effect compatibility,
# requires per-step prompt evidence plus output-type agreement, and
# trial-executes survivors. Two plan shapes:
#
#   SEQ  step1 -> [glue ->] step2 [-> step3]: direct string threading
#        where output/input types connect (at most one typed glue
#        adapter per gap, depth <= 3).
#   MAP  array-producer -> map element-cap over a prompt-named field.
#
# No model call anywhere: enumeration, validation, and ranking are
# pure computation with deterministic tie-breaks (type-match tier,
# procedure coverage, fewer steps, higher mean overlap, plan id).
# ---------------------------------------------------------------------------

# Prompt phrases/words -> data types they evince, read AFTER the word
# "output": a plan whose final outputs cannot satisfy the demand is
# rejected before trial. Multi-word cues are specific ("flat JSON
# object" means ONLY flat-json-object) and suppress the generic
# json-family single words: without that narrowing, a lossy
# flatten->csv->parse roundtrip chain passes as "JSON" on a task
# demanding a flat object (regression pinned in test_execaps).
_OUTPUT_PHRASES = (
    ("flat json object", ("flat-json-object",)),
    ("json array", ("json-array",)),
    ("json object", ("json-object",)),
)
_OUTPUT_KEYWORDS = {
    "json": ("json-array", "json-object", "flat-json-object"),
    "array": ("json-array",),
    "object": ("json-object", "flat-json-object"),
    "objects": ("json-array",),
    "csv": ("csv-text",),
    "iso": ("iso-date-text",),
    "date": ("date-text", "iso-date-text"),
    "dates": ("date-text", "iso-date-text"),
    "clf": ("json-object",),
}
_JSON_FAMILY_WORDS = frozenset({"json", "array", "object", "objects"})

_MIN_STEP_OVERLAP = 0.2

# Minimum distinct procedure words a chain must explain. Two steps
# resting on the SAME two generic words ("json"+"array" match any
# JSON task) is not composition evidence -- without this floor the
# csv-parse>table-csv roundtrip trial-passes on novelty prompts that
# lack an "Output" demand clause (W-NOV-01 regression).
_MIN_COVERAGE_WORDS = 3

# Minimum stem length for inflection-tolerant signature matching:
# shorter shared prefixes ("to"/"token", "in"/"input") are noise.
_MIN_STEM_MATCH = 4


def sig_word_hits(keywords, words):
    """Map each signature keyword to its matched prompt word (or None).

    Matching is inflection-tolerant: a keyword hits when it equals a
    prompt word or shares a common stem of >= 4 chars via prefix
    (``duplicates`` ~ ``duplicate``, ``dedupe`` ~ ``dedup``,
    ``keeping`` ~ ``keep``). Learned signatures legitimately inflect
    exposure wording, so exact-token matching would silently drop
    transfer retrieval; the stem floor keeps short-word collisions
    (``in``/``input``, ``to``/``token``) from counting as evidence.
    """
    words = set(words)
    hits = {}
    for keyword in keywords:
        hit = None
        if keyword in words:
            hit = keyword
        else:
            for word in sorted(words):
                short, long = ((keyword, word) if len(keyword) <= len(word)
                               else (word, keyword))
                if len(short) >= _MIN_STEM_MATCH and long.startswith(short):
                    hit = word
                    break
        hits[keyword] = hit
    return hits


def sig_overlap(keywords, words):
    """Fraction of signature keywords with a prompt-word hit."""
    keywords = tuple(keywords)
    if not keywords:
        return 0.0
    hits = sig_word_hits(keywords, words)
    return sum(1 for keyword in keywords
               if hits[keyword] is not None) / len(keywords)


def _prompt_words(task):
    return set(re.findall(r"[a-z0-9]+",
                          str(task.get("prompt", "")).lower()))


def infer_output_types(task):
    """Data types the task prompt demands AFTER the word "output".

    Empty set when the prompt has no output section (caller then
    relies on trial execution alone, ranked below type-matched plans).
    """
    prompt = str(task.get("prompt", "")).lower()
    _, sep, after = prompt.partition("output")
    if not sep:
        return frozenset()
    flat = re.sub(r"\s+", " ", after)
    out = set()
    specific = False
    for phrase, types in _OUTPUT_PHRASES:
        if phrase in flat:
            out.update(types)
            specific = True
    words = set(re.findall(r"[a-z0-9]+", after))
    for word, types in _OUTPUT_KEYWORDS.items():
        if specific and word in _JSON_FAMILY_WORDS:
            continue
        if word in words:
            out.update(types)
    return frozenset(out)


def _prompt_fields(task):
    """Field names the prompt nominates for MAP plans.

    Single-quoted single tokens ('date') plus "<name> column"
    phrasing. Multi-word quotes are format examples, not fields.
    """
    prompt = str(task.get("prompt", ""))
    fields = set(re.findall(r"'([A-Za-z_][A-Za-z0-9_]*)'", prompt))
    fields.update(m.lower()
                  for m in re.findall(r"([A-Za-z_]+)\s+column", prompt,
                                      flags=re.IGNORECASE))
    return fields


def _links_between(out_types, in_types):
    """Glue options bridging out_types -> in_types (None = direct).

    Yields None when the types connect directly, plus every typed
    glue adapter whose in/out links the gap. Deterministic order:
    direct first, then glue by name.
    """
    if set(out_types) & set(in_types):
        yield None
    for name in sorted(GLUE_SPECS):
        glue_in, glue_out, _ = GLUE_SPECS[name]
        if (set(out_types) & set(glue_in)
                and set(glue_out) & set(in_types)):
            yield name


def run_seq_plan(registry, pieces, check_input, task=None):
    """Execute a SEQ plan: alternating cap ids and glue names.

    Steps bind prompt-bound parameters from ``task`` (shared chain
    prompt); None binds every step's default.
    """
    value = check_input
    for piece in pieces:
        if piece in GLUE_SPECS:
            value = GLUE_SPECS[piece][2](value)
        else:
            value = registry.get(piece).execute(value, task)
    return value


def run_map_plan(registry, producer_id, field, element_id, check_input,
                 task=None):
    """Execute a MAP plan: producer -> map element-cap over field."""
    rows = json.loads(registry.get(producer_id).execute(
        check_input, task))
    if not isinstance(rows, list) or not rows:
        raise ValueError("MAP producer must yield a non-empty JSON array")
    if not any(isinstance(row, dict) and field in row for row in rows):
        raise ValueError("field %r absent from producer rows" % (field,))
    element = registry.get(element_id)
    out = []
    for row in rows:
        row = dict(row)
        if field in row:
            row[field] = element.execute(str(row[field]), task)
        out.append(row)
    return json.dumps(out)


class Composition:
    """A verified plan combining >=2 capabilities (zero model calls).

    ``uses`` names the executed capabilities in order; ``run`` threads
    them (plus thin glue) from input to output. ``executed_count`` is
    len(uses) -- always >= 2, so a solved composition task classifies
    as COMPOSE, never REUSE.
    """

    def __init__(self, comp_id, intent, uses, run, category,
                 prompt_keywords, input_types, output_types):
        assert len(uses) >= 2, "a composition reuses at least 2 capabilities"
        self.descriptor = retrieve.Capability(
            id=comp_id, intent=intent, input_types=input_types,
            output_types=output_types, effects=(), family="A")
        self.uses = list(uses)
        self.run = run
        self.category = category
        self.prompt_keywords = frozenset(prompt_keywords)
        self._positive = []
        self._negative = []

    @property
    def id(self):
        return self.descriptor.id

    @property
    def executed_count(self):
        return len(self.uses)

    def prompt_overlap(self, task):
        return sig_overlap(self.prompt_keywords, _prompt_words(task))

    def execute(self, registry, check_input, task=None):
        """Run the plan (pure computation, never the model).

        Steps bind prompt-bound parameters from ``task``; None
        binds defaults (preserves every existing call site).
        """
        return self.run(registry, check_input, task)

    def record(self, task_id, helped):
        """Append trusted reuse evidence (driver calls post-scoring)."""
        target = self._positive if helped else self._negative
        if task_id not in target:
            target.append(task_id)

    def evidence(self):
        """Evidence envelope for promotion accounting (§9)."""
        return {"positive": list(self._positive),
                "negative": list(self._negative),
                "reuse_count": len(self._positive) + len(self._negative)}


def _seq_candidates(caps):
    """Yield (steps, pieces) SEQ chains, depth 2..3.

    ``steps`` are cap ids in order; ``pieces`` interleave glue names.
    Every gap links by direct type connection or one typed glue
    adapter; steps never repeat within a chain.
    """
    ordered = sorted(caps, key=lambda c: c.id)
    pairs = [(a, b) for a in ordered for b in ordered if a is not b]
    for first, second in pairs:
        for glue in _links_between(first.descriptor.output_types,
                                   second.descriptor.input_types):
            pieces = [first.id]
            if glue is not None:
                pieces.append(glue)
            pieces.append(second.id)
            yield ([first.id, second.id], pieces)
    triples = [(a, b, c) for a in ordered for b in ordered
               for c in ordered if len({a.id, b.id, c.id}) == 3]
    for first, second, third in triples:
        for glue_ab in _links_between(
                first.descriptor.output_types,
                second.descriptor.input_types):
            for glue_bc in _links_between(
                    second.descriptor.output_types,
                    third.descriptor.input_types):
                pieces = [first.id]
                if glue_ab is not None:
                    pieces.append(glue_ab)
                pieces.append(second.id)
                if glue_bc is not None:
                    pieces.append(glue_bc)
                pieces.append(third.id)
                yield ([first.id, second.id, third.id], pieces)


def _map_candidates(caps, fields):
    """Yield (producer, field, element) MAP candidates.

    The producer must emit a JSON array; the element step may be any
    other capability; the field must be prompt-nominated.
    """
    ordered = sorted(caps, key=lambda c: c.id)
    for producer in ordered:
        if "json-array" not in producer.descriptor.output_types:
            continue
        for element in ordered:
            if element is producer:
                continue
            for field in sorted(fields):
                yield (producer.id, field, element.id)


def search_compositions(registry, task, check_input, max_plans=8):
    """Deterministically discover composition plans for a task+input.

    Enumerates SEQ chains (depth <= 3) and MAP plans, then applies
    the validation gates in order: effect compatibility (effectful
    plans need task-allowed effects), per-step prompt evidence (>=
    0.2 each), veto clearance (no step may carry veto vocabulary
    for this task), joint coverage (>= 3 distinct procedure words --
    steps resting on the same two generic words do not compose),
    output-type agreement with the prompt demand, trial
    execution on ``check_input``, and non-vacuity (the plan must
    compute something neither endpoint step computes alone). Mean
    step overlap is a RANKING signal only, never a gate: a chain
    whose every step is prompt-grounded must not die because one
    step's signature carries exposure-specific filler words.
    Survivors rank by (type-match tier, mention-order inversions,
    -procedure coverage, steps, -mean step overlap, plan id); at most
    ``max_plans`` return. Mention order comes first among prompt
    signals: procedural prompts list steps in execution order
    ("normalize ... and dedup"), so a plan whose steps run against
    that order loses to one that follows it. Procedure coverage
    (distinct NON-demand prompt words the steps' signatures explain)
    outranks brevity next: on a three-procedure prompt the full
    3-chain must beat a trial-passing 2-subchain. Empty means "no
    composable plan" -- the caller falls through to
    adaptation/synthesis. No model call anywhere.
    """
    caps = list(registry._caps.values())
    by_id = {cap.id: cap for cap in caps}
    words = _prompt_words(task)
    before, sep, _ = str(task.get("prompt", "")).lower().partition("output")
    procedure_words = (set(re.findall(r"[a-z0-9]+", before)) if sep
                       else set(words))
    # Step evidence uses PROCEDURE words only: demand words ("output",
    # "json") appear in every JSON task's demand clause and must not
    # support a procedure match -- otherwise e.g. flatten (whose
    # signature contains "json"+"output") reaches 2/6 on any JSON
    # prompt and spurious chains trial-pass on single-procedure
    # tasks (W-EXP-05 regression).
    step_overlap = {}
    step_hits = {}
    for cap in caps:
        hits = sig_word_hits(cap.prompt_keywords, procedure_words)
        step_hits[cap.id] = hits
        step_overlap[cap.id] = (
            sum(1 for hit in hits.values() if hit is not None)
            / len(cap.prompt_keywords)) if cap.prompt_keywords else 0.0
    # Hierarchy level 1 (scaling): enumerate chains only over steps
    # that clear the evidence floor and veto clearance. viable()
    # would reject every other chain anyway, so this preserves
    # results exactly while keeping enumeration proportional to
    # survivors, not to registry size -- hundreds of distractor
    # capabilities cost one overlap scan, not n^2 trials.
    caps = [cap for cap in caps
            if step_overlap[cap.id] >= _MIN_STEP_OVERLAP
            and not cap.prompt_vetoed(task)]
    demanded = infer_output_types(task)
    allowed = task.get("allowed_effects")

    def trial_ok(plan, uses):
        """Trial execution plus the non-vacuity gate.

        The plan must run without error AND compute something
        neither endpoint step computes alone: a chain whose output
        equals its first step's output (e.g. flattening an
        already-flat object) is not a composition, it is a
        single-capability task wearing a costume.
        """
        try:
            out = plan.execute(registry, check_input, task)
        except Exception:
            return False
        try:
            if out == by_id[uses[0]].execute(check_input, task):
                return False
        except Exception:
            pass
        try:
            if out == by_id[uses[-1]].execute(check_input, task):
                return False
        except Exception:
            pass
        return True

    ordered_words = re.findall(r"[a-z0-9]+",
                               str(task.get("prompt", "")).lower())
    first_mention = {}
    for pos, word in enumerate(ordered_words):
        first_mention.setdefault(word, pos)

    def viable(uses):
        if any(step_overlap[u] < _MIN_STEP_OVERLAP for u in uses):
            return None
        if any(by_id[u].prompt_vetoed(task) for u in uses):
            return None
        effects = set()
        for u in uses:
            effects.update(by_id[u].descriptor.effects)
        if effects and (allowed is None
                        or not effects <= set(allowed)):
            return None
        mean = sum(step_overlap[u] for u in uses) / len(uses)
        covered = set()
        for u in uses:
            covered.update(hit for hit in step_hits[u].values()
                           if hit is not None)
        if len(covered) < _MIN_COVERAGE_WORDS:
            return None
        positions = [min(first_mention[hit]
                         for hit in step_hits[u].values()
                         if hit is not None)
                     for u in uses]
        inversions = sum(1 for i in range(len(positions))
                         for j in range(i + 1, len(positions))
                         if positions[i] > positions[j])
        return mean, len(covered), inversions

    ranked = []
    for steps, pieces in _seq_candidates(caps):
        score = viable(steps)
        if score is None:
            continue
        mean, coverage, inversions = score
        final = by_id[steps[-1]].descriptor.output_types
        if demanded and not (set(final) & set(demanded)):
            continue
        plan_id = "seq:" + ">".join(pieces)
        keywords = set()
        for u in steps:
            keywords.update(by_id[u].prompt_keywords)
        plan = Composition(
            plan_id,
            " then ".join(by_id[u].descriptor.intent for u in steps),
            steps,
            lambda reg, text, task, p=pieces: run_seq_plan(
                reg, p, text, task),
            task.get("category"),
            tuple(sorted(keywords)),
            by_id[steps[0]].descriptor.input_types,
            tuple(final))
        if not trial_ok(plan, steps):
            continue
        ranked.append((len(steps), -mean, plan_id, plan,
                       bool(demanded and set(final) & set(demanded)),
                       coverage, inversions))
    for producer, field, element in _map_candidates(
            caps, _prompt_fields(task)):
        uses = [producer, element]
        score = viable(uses)
        if score is None:
            continue
        mean, coverage, inversions = score
        final = by_id[producer].descriptor.output_types
        if demanded and not (set(final) & set(demanded)):
            continue
        plan_id = "map:%s[%s]>%s" % (producer, field, element)
        keywords = (by_id[producer].prompt_keywords
                    | by_id[element].prompt_keywords)
        plan = Composition(
            plan_id,
            "%s then map %s over field %r"
            % (by_id[producer].descriptor.intent,
               by_id[element].descriptor.intent, field),
            uses,
            lambda reg, text, task, p=producer, f=field, e=element: (
                run_map_plan(reg, p, f, e, text, task)),
            task.get("category"),
            tuple(sorted(keywords)),
            by_id[producer].descriptor.input_types,
            tuple(final))
        if not trial_ok(plan, uses):
            continue
        ranked.append((len(uses), -mean, plan_id, plan,
                       bool(demanded and set(final) & set(demanded)),
                       coverage, inversions))
    # Type-match tier, mention-order inversions, procedure coverage,
    # fewer steps, higher mean overlap, plan id (fully deterministic).
    ranked.sort(key=lambda row: (not row[4], row[6], -row[5], row[0],
                                 row[1], row[2]))
    return [row[3] for row in ranked[:max(0, max_plans)]]


class ExecRegistry:
    """Executable store: retrieval (via retrieve.py) + verified execution.

    ``_comps`` is the discovered-plan cache: search synthesizes plans
    and memoizes winners here (promotion evidence accrues on the
    cached plan). Nothing is pre-registered: an empty registry
    discovers the same plans as a warm one.
    """

    def __init__(self, capabilities=None, compositions=None):
        self._caps = {}
        for cap in capabilities or seed_capabilities():
            self._caps[cap.id] = cap
        self._comps = {}
        for comp in compositions or ():
            self._comps[comp.id] = comp
        self.index = retrieve.CapabilityIndex(
            [cap.descriptor for cap in self._caps.values()])

    def get(self, cap_id):
        return self._caps.get(cap_id)

    def capability_count(self):
        return len(self._caps) + len(self._comps)

    def find_for_task(self, task, check_input, min_overlap=0.5):
        """All capabilities whose category+prompt+precondition fit.

        Ordered by retrieval score (retrieve.py signals); empty means
        "no applicable capability" -- the caller must use the model.
        """
        fitting = [cap for cap in self._caps.values()
                   if cap.applies_to(task, check_input,
                                     min_overlap=min_overlap)]
        if not fitting:
            return []
        # No family constraint: reuse ACROSS task families is the
        # point (family-R tasks reuse family-A procedures); category,
        # prompt overlap, and precondition already scope the match.
        goal = {"text": task.get("prompt", "")}
        scored = retrieve.find_capabilities(
            goal, {"index": retrieve.CapabilityIndex(
                [cap.descriptor for cap in fitting])}, k=len(fitting))
        by_id = {cap.id: cap for cap in fitting}
        return [by_id[cap.id] for cap, _ in scored if cap.id in by_id]

    def find_composition(self, task, check_input):
        """Best searched plan for a task+input, memoized in the cache.

        Delegates to :func:`search_compositions` (type/effect/prompt/
        trial gates, deterministic rank) and caches the winner so
        promotion evidence accrues on one stable plan id. Category
        match is required: a plan discovered for csv never fires on
        records. Empty registry discovers the same plans as a warm
        one -- nothing is pre-registered.
        """
        # Always re-search: the full gate stack (per-step
        # evidence, output-type demand, trial) must pass on THIS
        # input, so a memoized plan can never fire where only a
        # subset of its steps applies. Winners memoize by plan id
        # for stable promotion evidence.
        plans = search_compositions(
            self, task, check_input, max_plans=1)
        if not plans:
            return None
        if plans[0].category != task.get("category"):
            return None
        self._comps.setdefault(plans[0].id, plans[0])
        return self._comps[plans[0].id]


def verify_capability(cap, task):
    """Run a capability over every check of its source task.

    Returns (passed: bool, failed_checks: list). A seed capability MUST
    pass its source task -- otherwise the reuse experiment is rigged.
    """
    failed = []
    for j, check in enumerate(task.get("checks", [])):
        try:
            actual = cap.execute(check["input"], task)
        except Exception as exc:
            failed.append((j, "raised %r" % (exc,)))
            continue
        if not compare(check["expected"], actual, check["compare"]):
            failed.append((j, actual))
    return (not failed), failed


def verify_composition(comp, registry, task):
    """Run a composition over every check of a task; same contract."""
    failed = []
    for j, check in enumerate(task.get("checks", [])):
        try:
            actual = comp.execute(registry, check["input"], task)
        except Exception as exc:
            failed.append((j, "raised %r" % (exc,)))
            continue
        if not compare(check["expected"], actual, check["compare"]):
            failed.append((j, actual))
    return (not failed), failed
