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
# Composition glue (tiny verified plumbing between reused capabilities)
# ---------------------------------------------------------------------------

def glue_csv_date_iso(rows_json, date_field="date"):
    """Normalize the ``date_field`` of each row in a JSON array to ISO."""
    rows = json.loads(rows_json)
    out = []
    for row in rows:
        row = dict(row)
        if date_field in row:
            row[date_field] = fn_date_iso(str(row[date_field]))
        out.append(row)
    return json.dumps(out)


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


GLUE = {
    "csv_date_iso": glue_csv_date_iso,
    "flat_to_rows": glue_flat_to_rows,
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ExecCapability:
    """One executable capability: descriptor + fn + fit checks."""

    def __init__(self, cap_id, intent, category, input_types, output_types,
                 fn, precondition, prompt_keywords, source_task):
        self.descriptor = retrieve.Capability(
            id=cap_id, intent=intent, input_types=input_types,
            output_types=output_types, effects=(), family="A")
        self.category = category
        self.fn = fn
        self.precondition = precondition
        self.prompt_keywords = frozenset(prompt_keywords)
        self.source_task = source_task

    @property
    def id(self):
        return self.descriptor.id

    def prompt_overlap(self, task):
        """Fraction of signature keywords present in the task prompt."""
        words = set(re.findall(r"[a-z0-9]+",
                               str(task.get("prompt", "")).lower()))
        if not self.prompt_keywords:
            return 0.0
        return len(self.prompt_keywords & words) / len(self.prompt_keywords)

    def applies_to(self, task, check_input, min_overlap=0.5):
        """True only when category, prompt, AND input precondition agree."""
        if task.get("category") != self.category:
            return False
        if self.prompt_overlap(task) < min_overlap:
            return False
        try:
            return bool(self.precondition(check_input))
        except Exception:
            return False

    def execute(self, check_input):
        """Run the capability (pure computation, never the model)."""
        return self.fn(check_input)


def seed_capabilities():
    """The five verified seed capabilities.

    Four mirror Family A exposure procedures (one per category); the
    fifth (cap-table-csv) is the verified inverse of cap-csv-parse with
    its own contract checks (see SYNTHETIC_CHECKS below).
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


def run_csv_date_iso(registry, check_input):
    """csv-parse -> map date-iso over the date field. Uses 2 capabilities."""
    rows_json = registry.get("cap-csv-parse").execute(check_input)
    date_fn = registry.get("cap-date-iso").fn
    rows = json.loads(rows_json)
    out = []
    for row in rows:
        row = dict(row)
        if "date" in row:
            row["date"] = date_fn(str(row["date"]))
        out.append(row)
    return json.dumps(out)


def run_flat_to_csv(registry, check_input):
    """flatten -> rows glue -> table-csv. Uses 2 capabilities + plumbing."""
    flat = registry.get("cap-flatten").execute(check_input)
    rows = glue_flat_to_rows(flat)
    return registry.get("cap-table-csv").execute(rows)


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

    @property
    def id(self):
        return self.descriptor.id

    @property
    def executed_count(self):
        return len(self.uses)

    def prompt_overlap(self, task):
        words = set(re.findall(r"[a-z0-9]+",
                               str(task.get("prompt", "")).lower()))
        if not self.prompt_keywords:
            return 0.0
        return len(self.prompt_keywords & words) / len(self.prompt_keywords)

    def execute(self, registry, check_input):
        """Run the plan (pure computation, never the model)."""
        return self.run(registry, check_input)


def seed_compositions():
    """Two verified compositions, each reusing 2 seed capabilities."""
    return [
        Composition(
            "cmp-csv-date-iso",
            "parse CSV then normalize its date column to ISO",
            ["cap-csv-parse", "cap-date-iso"], run_csv_date_iso, "csv",
            ("parse", "csv", "date", "column", "iso", "json"),
            ("csv-text",), ("json-array",)),
        Composition(
            "cmp-flat-to-csv",
            "flatten nested JSON object then serialize as key,value CSV",
            ["cap-flatten", "cap-table-csv"], run_flat_to_csv, "records",
            ("flatten", "json", "csv", "key", "value", "rows"),
            ("json-object",), ("csv-text",)),
    ]


class ExecRegistry:
    """Executable store: retrieval (via retrieve.py) + verified execution."""

    def __init__(self, capabilities=None, compositions=None):
        self._caps = {}
        for cap in capabilities or seed_capabilities():
            self._caps[cap.id] = cap
        self._comps = {}
        for comp in compositions or seed_compositions():
            self._comps[comp.id] = comp
        self.index = retrieve.CapabilityIndex(
            [cap.descriptor for cap in self._caps.values()] +
            [comp.descriptor for comp in self._comps.values()])

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

    def find_composition(self, task, check_input, min_overlap=0.6):
        """First composition whose category+prompt fits and steps run.

        The default bar (0.6) is stricter than single-capability
        matching (0.5): a composition skips the model for a MULTI-step
        plan, so the prompt must show evidence of both steps, not just
        one shared procedure word.
        """
        words = set(re.findall(
            r"[a-z0-9]+", str(task.get("prompt", "")).lower()))
        for comp in self._comps.values():
            if task.get("category") != comp.category:
                continue
            overlap = (len(comp.prompt_keywords & words)
                       / len(comp.prompt_keywords))
            if overlap < min_overlap:
                continue
            try:
                comp.execute(self, check_input)
            except Exception:
                continue
            return comp
        return None


def verify_capability(cap, task):
    """Run a capability over every check of its source task.

    Returns (passed: bool, failed_checks: list). A seed capability MUST
    pass its source task -- otherwise the reuse experiment is rigged.
    """
    failed = []
    for j, check in enumerate(task.get("checks", [])):
        try:
            actual = cap.execute(check["input"])
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
            actual = comp.execute(registry, check["input"])
        except Exception as exc:
            failed.append((j, "raised %r" % (exc,)))
            continue
        if not compare(check["expected"], actual, check["compare"]):
            failed.append((j, actual))
    return (not failed), failed
