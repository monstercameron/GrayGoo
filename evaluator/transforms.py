"""Metamorphic / equivalence-preserving input generators (plan.md section 37).

Static hidden tests are insufficient: candidates can overfit to test
structure rather than task semantics. This module generates fresh,
semantics-preserving variants of Family A parsing cases:

- ``rename_identifiers`` — rename headers/keys consistently
- ``permute_ordering`` — reorder columns, pairs, keys, or lines
- ``rescale_values`` — scale numeric values on both sides
- ``inject_irrelevant_fields`` — add noise the task must ignore (or echo)
- ``change_formatting`` — equivalent serializations, same meaning
- ``new_boundary_examples`` — fresh boundary inputs with solver-checked answers

A case is a plain dict::

    {"kind": "flatten" | "csv_select" | "kv_parse" | "dates",
     "input": "<candidate-facing text>",
     "expected": "<reference output text>",
     "compare": "json" | "exact",
     "params": {<kind-specific solver parameters>},
     "transforms_applied": [<names, oldest first>]}

Each transform takes a case and returns a NEW case (inputs are never
mutated). ``check_case`` replays the kind's reference solver over the new
input and confirms it still produces the new expected output, which is
how the suite proves the transforms preserve semantics.

Stdlib only. Deterministic: every randomized choice derives from ``seed``.
"""

import csv
import io
import json
import random
import re
from datetime import date

COMPARE_MODES = ("json", "exact")

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH_ABBR = {
    1: "Jan",
    2: "Feb",
    3: "Mar",
    4: "Apr",
    5: "May",
    6: "Jun",
    7: "Jul",
    8: "Aug",
    9: "Sep",
    10: "Oct",
    11: "Nov",
    12: "Dec",
}


# --------------------------------------------------------------------------
# Reference solvers (define the task semantics the transforms must preserve)
# --------------------------------------------------------------------------

def solve_flatten(text):
    """Flatten a JSON object: objects -> dot keys, arrays -> key[i]."""
    flat = {}

    def walk(value, prefix):
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, "%s.%s" % (prefix, key) if prefix else str(key))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, "%s[%d]" % (prefix, i))
        else:
            flat[prefix] = value

    walk(json.loads(text), "")
    return json.dumps(flat, sort_keys=True)


def solve_csv_select(text, columns):
    """Parse RFC 4180 CSV; emit JSON array keeping mapped header->outkey."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return "[]"
    header, data = rows[0], rows[1:]
    kept = [
        (i, columns[name]) for i, name in enumerate(header) if name in columns
    ]
    out = []
    for row in data:
        obj = {}
        for i, outkey in kept:
            obj[outkey] = row[i] if i < len(row) else ""
        out.append(obj)
    return json.dumps(out, sort_keys=True)


_KV_TOKEN = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*("[^"]*"|\S*)')


def parse_kv_pairs(text):
    """Parse space-separated key=value pairs (double-quoted values allowed)."""
    pairs = []
    pos = 0
    end = len(text)
    while pos < end:
        match = _KV_TOKEN.match(text, pos)
        if match is None or match.end() == pos:
            raise ValueError("malformed key=value input near %r" % text[pos:])
        key, raw = match.group(1), match.group(2)
        pairs.append((key, raw[1:-1] if raw.startswith('"') else raw))
        pos = match.end()
    return pairs


def render_kv_pairs(pairs, spacing=" "):
    """Serialize pairs, quoting values that contain whitespace."""
    parts = []
    for key, value in pairs:
        if re.search(r"\s", value) or value == "":
            parts.append('%s="%s"' % (key, value))
        else:
            parts.append("%s=%s" % (key, value))
    return spacing.join(parts)


def solve_kv(text, ignore=()):
    """Parse one key=value log line into a JSON object, dropping ignored keys."""
    ignored = set(ignore)
    obj = {k: v for k, v in parse_kv_pairs(text.strip()) if k not in ignored}
    return json.dumps(obj, sort_keys=True)


_RECORD_LINE = re.compile(r"^\s*Record\s+(\S+)\s*:\s*(.+?)\s*\((.*)\)\s*$")
_DATE_FORMS = (
    re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})$"),
    re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$"),
    re.compile(r"^([A-Za-z]{3,9})\s+(\d{1,2}),\s*(\d{4})$"),
)


def parse_supported_date(text):
    """Parse one of the supported equivalent date forms to a date."""
    text = text.strip()
    match = _DATE_FORMS[0].match(text)
    if match:
        day, mon, year = match.groups()
        return date(int(year), _MONTHS[mon[:3].lower()], int(day))
    match = _DATE_FORMS[1].match(text)
    if match:
        year, mon, day = match.groups()
        return date(int(year), int(mon), int(day))
    match = _DATE_FORMS[2].match(text)
    if match:
        mon, day, year = match.groups()
        return date(int(year), _MONTHS[mon[:3].lower()], int(day))
    raise ValueError("unsupported date form: %r" % text)


def solve_dates(text):
    """Convert each 'Record <id>: <date> (<note>)' line to ISO YYYY-MM-DD."""
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _RECORD_LINE.match(line)
        if match is None:
            raise ValueError("malformed record line: %r" % line)
        out.append(parse_supported_date(match.group(2)).isoformat())
    return "\n".join(out)


def solve_case(case):
    """Run the reference solver for a case dict; return output text."""
    kind, params = case["kind"], case.get("params", {})
    if kind == "flatten":
        return solve_flatten(case["input"])
    if kind == "csv_select":
        return solve_csv_select(case["input"], params["columns"])
    if kind == "kv_parse":
        return solve_kv(case["input"], params.get("ignore", ()))
    if kind == "dates":
        return solve_dates(case["input"])
    raise ValueError("unknown case kind: %r" % kind)


def outputs_equal(actual, expected, compare):
    """Compare-mode equality, mirroring benchmarks/runner.py semantics."""
    if compare == "exact":
        return actual.strip("\n") == expected.strip("\n")
    if compare == "json":
        try:
            return json.loads(actual) == json.loads(expected)
        except (ValueError, TypeError):
            return False
    raise ValueError("unknown compare mode: %r" % compare)


def check_case(case):
    """True iff the reference solver maps input -> expected under compare."""
    try:
        actual = solve_case(case)
    except (ValueError, KeyError):
        return False
    return outputs_equal(actual, case["expected"], case["compare"])


# --------------------------------------------------------------------------
# Case plumbing
# --------------------------------------------------------------------------

def make_case(kind, input_text, params=None, compare=None, name=None):
    """Build a case dict, computing ``expected`` with the reference solver."""
    params = dict(params or {})
    if compare is None:
        compare = "exact" if kind == "dates" else "json"
    case = {
        "kind": kind,
        "input": input_text,
        "expected": "",
        "compare": compare,
        "params": params,
        "transforms_applied": [],
    }
    if name is not None:
        case["name"] = name
    case["expected"] = solve_case(case)
    return case


def _derive(case, input_text=None, expected=None, params=None, transform=None):
    """Copy a case, recording the applied transform name."""
    new = {
        "kind": case["kind"],
        "input": case["input"] if input_text is None else input_text,
        "expected": case["expected"] if expected is None else expected,
        "compare": case["compare"],
        "params": dict(case.get("params", {})) if params is None else params,
        "transforms_applied": list(case.get("transforms_applied", [])),
    }
    if "name" in case:
        new["name"] = case["name"]
    if transform is not None:
        new["transforms_applied"].append(transform)
    return new


# --------------------------------------------------------------------------
# 1. rename_identifiers
# --------------------------------------------------------------------------

def _rename_flat_key(flat_key, mapping):
    parts = re.split(r"(\.|\[\d+\])", flat_key)
    renamed = []
    for part in parts:
        if part == "" or part == "." or part.startswith("["):
            renamed.append(part)
        else:
            renamed.append(mapping.get(part, part))
    return "".join(renamed)


def _rename_json_keys(value, mapping):
    if isinstance(value, dict):
        return {
            mapping.get(k, k): _rename_json_keys(v, mapping)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_rename_json_keys(item, mapping) for item in value]
    return value


def _rename_csv(case, mapping):
    rows = list(csv.reader(io.StringIO(case["input"])))
    header = [mapping.get(name, name) for name in rows[0]]
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows([header] + rows[1:])
    columns = {
        mapping.get(name, name): outkey
        for name, outkey in case["params"]["columns"].items()
    }
    params = dict(case.get("params", {}), columns=columns)
    return _derive(case, input_text=buf.getvalue(), params=params,
                   transform="rename_identifiers")


def _rename_kv(case, mapping):
    pairs = [
        (mapping.get(k, k), v) for k, v in parse_kv_pairs(case["input"].strip())
    ]
    expected = {
        mapping.get(k, k): v for k, v in json.loads(case["expected"]).items()
    }
    params = dict(case.get("params", {}))
    if "ignore" in params:
        params["ignore"] = [mapping.get(k, k) for k in params["ignore"]]
    return _derive(case, input_text=render_kv_pairs(pairs),
                   expected=json.dumps(expected, sort_keys=True),
                   params=params, transform="rename_identifiers")


def _rename_flatten(case, mapping):
    renamed_in = _rename_json_keys(json.loads(case["input"]), mapping)
    renamed_out = {
        _rename_flat_key(k, mapping): v
        for k, v in json.loads(case["expected"]).items()
    }
    return _derive(case, input_text=json.dumps(renamed_in),
                   expected=json.dumps(renamed_out, sort_keys=True),
                   transform="rename_identifiers")


def rename_identifiers(case, mapping):
    """Rename identifiers consistently on both sides of a case.

    ``mapping`` maps old -> new names. CSV output keys are fixed by the
    task, so only headers (and the column map) change and ``expected``
    is untouched; kv/flatten expected outputs embed the identifiers,
    so they are rewritten too.
    """
    if not mapping:
        raise ValueError("rename_identifiers requires a non-empty mapping")
    if case["kind"] == "csv_select":
        return _rename_csv(case, mapping)
    if case["kind"] == "kv_parse":
        return _rename_kv(case, mapping)
    if case["kind"] == "flatten":
        return _rename_flatten(case, mapping)
    raise ValueError("rename_identifiers does not support kind %r"
                     % case["kind"])


# --------------------------------------------------------------------------
# 2. permute_ordering
# --------------------------------------------------------------------------

def _permute_csv(case, rng):
    rows = list(csv.reader(io.StringIO(case["input"])))
    order = list(range(len(rows[0])))
    rng.shuffle(order)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(
        [[row[i] if i < len(row) else "" for i in order] for row in rows]
    )
    return _derive(case, input_text=buf.getvalue(),
                   transform="permute_ordering")


def _permute_kv(case, rng):
    pairs = parse_kv_pairs(case["input"].strip())
    rng.shuffle(pairs)
    return _derive(case, input_text=render_kv_pairs(pairs),
                   transform="permute_ordering")


def _permute_json(value, rng):
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {k: _permute_json(v, rng) for k, v in items}
    if isinstance(value, list):
        return [_permute_json(item, rng) for item in value]
    return value


def _permute_flatten(case, rng):
    shuffled = _permute_json(json.loads(case["input"]), rng)
    return _derive(case, input_text=json.dumps(shuffled),
                   transform="permute_ordering")


def _permute_dates(case, rng):
    lines = [ln for ln in case["input"].splitlines() if ln.strip()]
    expected = case["expected"].split("\n")
    order = list(range(len(lines)))
    rng.shuffle(order)
    return _derive(case, input_text="\n".join(lines[i] for i in order),
                   expected="\n".join(expected[i] for i in order),
                   transform="permute_ordering")


def permute_ordering(case, seed=0):
    """Reorder columns / pairs / keys / lines; meaning is unchanged.

    Dates lines move together with their expected outputs; every other
    kind keeps ``expected`` byte-identical (JSON comparison ignores order).
    """
    rng = random.Random(seed)
    if case["kind"] == "csv_select":
        return _permute_csv(case, rng)
    if case["kind"] == "kv_parse":
        return _permute_kv(case, rng)
    if case["kind"] == "flatten":
        return _permute_flatten(case, rng)
    if case["kind"] == "dates":
        return _permute_dates(case, rng)
    raise ValueError("unknown case kind: %r" % case["kind"])


# --------------------------------------------------------------------------
# 3. rescale_values
# --------------------------------------------------------------------------

def _rescale_json(value, factor):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value * factor
    if isinstance(value, float):
        return value * factor
    if isinstance(value, dict):
        return {k: _rescale_json(v, factor) for k, v in value.items()}
    if isinstance(value, list):
        return [_rescale_json(item, factor) for item in value]
    return value


def rescale_values(case, factor=100):
    """Multiply every numeric value by ``factor`` on both sides.

    Only ``flatten`` passes numbers through to its output today.
    """
    if case["kind"] != "flatten":
        raise ValueError("rescale_values does not support kind %r"
                         % case["kind"])
    if isinstance(factor, bool) or not isinstance(factor, (int, float)):
        raise ValueError("factor must be a number")
    scaled_in = _rescale_json(json.loads(case["input"]), factor)
    scaled_out = _rescale_json(json.loads(case["expected"]), factor)
    return _derive(case, input_text=json.dumps(scaled_in),
                   expected=json.dumps(scaled_out, sort_keys=True),
                   transform="rescale_values")


# --------------------------------------------------------------------------
# 4. inject_irrelevant_fields
# --------------------------------------------------------------------------

def _inject_csv(case, rng, count):
    rows = list(csv.reader(io.StringIO(case["input"])))
    junk_names = ["noise_%d" % rng.randrange(10 ** 6) for _ in range(count)]
    header = rows[0] + junk_names
    data = [row + ["junk-%d" % i for i in range(count)] for row in rows[1:]]
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows([header] + data)
    return _derive(case, input_text=buf.getvalue(),
                   transform="inject_irrelevant_fields")


def _inject_kv(case, rng, count):
    pairs = parse_kv_pairs(case["input"].strip())
    params = dict(case.get("params", {}))
    ignored = list(params.get("ignore", []))
    for _ in range(count):
        key = "noise_%d" % rng.randrange(10 ** 6)
        ignored.append(key)
        pairs.append((key, "junk value %d" % rng.randrange(10 ** 6)))
    params["ignore"] = ignored
    return _derive(case, input_text=render_kv_pairs(pairs), params=params,
                   transform="inject_irrelevant_fields")


def _inject_flatten(case, rng, count):
    obj = json.loads(case["input"])
    expected = json.loads(case["expected"])
    for _ in range(count):
        key = "noise_%d" % rng.randrange(10 ** 6)
        obj[key] = {"tag": "junk", "n": rng.randrange(10 ** 6)}
        expected["%s.tag" % key] = "junk"
        expected["%s.n" % key] = obj[key]["n"]
    return _derive(case, input_text=json.dumps(obj),
                   expected=json.dumps(expected, sort_keys=True),
                   transform="inject_irrelevant_fields")


def _inject_dates(case, rng):
    lines = []
    for line in case["input"].splitlines():
        match = _RECORD_LINE.match(line)
        if match is None:
            lines.append(line)
            continue
        lines.append("Record %d: %s (note-%d)" % (
            rng.randrange(10 ** 6), match.group(2), rng.randrange(10 ** 6)))
    return _derive(case, input_text="\n".join(lines),
                   transform="inject_irrelevant_fields")


def inject_irrelevant_fields(case, seed=0, count=1):
    """Add noise the task must ignore (or, for flatten, echo faithfully)."""
    if count < 1:
        raise ValueError("count must be >= 1")
    rng = random.Random(seed)
    if case["kind"] == "csv_select":
        return _inject_csv(case, rng, count)
    if case["kind"] == "kv_parse":
        return _inject_kv(case, rng, count)
    if case["kind"] == "flatten":
        return _inject_flatten(case, rng, count)
    if case["kind"] == "dates":
        return _inject_dates(case, rng)
    raise ValueError("unknown case kind: %r" % case["kind"])


# --------------------------------------------------------------------------
# 5. change_formatting
# --------------------------------------------------------------------------

def _format_flatten(case, rng):
    obj = json.loads(case["input"])
    style = rng.randrange(3)
    if style == 0:
        text = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    elif style == 1:
        text = json.dumps(obj, indent=2, sort_keys=True)
    else:
        text = json.dumps(obj, indent=4) + "\n"
    return _derive(case, input_text=text, transform="change_formatting")


def _format_csv(case, rng):
    rows = list(csv.reader(io.StringIO(case["input"])))
    buf = io.StringIO()
    quoting = rng.choice([csv.QUOTE_MINIMAL, csv.QUOTE_ALL, csv.QUOTE_NONNUMERIC])
    csv.writer(buf, lineterminator="\n", quoting=quoting).writerows(rows)
    return _derive(case, input_text=buf.getvalue(),
                   transform="change_formatting")


def _format_kv(case, rng):
    pairs = parse_kv_pairs(case["input"].strip())
    spacing = rng.choice([" ", "  ", "   "])
    return _derive(case, input_text=render_kv_pairs(pairs, spacing),
                   transform="change_formatting")


def _render_date(value, style):
    if style == 0:
        return "%d %s %d" % (value.day, _MONTH_ABBR[value.month], value.year)
    if style == 1:
        return "%04d/%02d/%02d" % (value.year, value.month, value.day)
    return "%s %d, %d" % (_MONTH_ABBR[value.month], value.day, value.year)


def _format_dates(case, rng):
    lines = []
    for line in case["input"].splitlines():
        match = _RECORD_LINE.match(line)
        if match is None:
            lines.append(line)
            continue
        value = parse_supported_date(match.group(2))
        lines.append("Record %s: %s (%s)" % (
            match.group(1), _render_date(value, rng.randrange(3)),
            match.group(3)))
    return _derive(case, input_text="\n".join(lines),
                   transform="change_formatting")


def change_formatting(case, seed=0):
    """Re-serialize the input equivalently; ``expected`` is untouched."""
    rng = random.Random(seed)
    if case["kind"] == "flatten":
        return _format_flatten(case, rng)
    if case["kind"] == "csv_select":
        return _format_csv(case, rng)
    if case["kind"] == "kv_parse":
        return _format_kv(case, rng)
    if case["kind"] == "dates":
        return _format_dates(case, rng)
    raise ValueError("unknown case kind: %r" % case["kind"])


# --------------------------------------------------------------------------
# 6. new_boundary_examples
# --------------------------------------------------------------------------

_BOUNDARY_INPUTS = {
    "flatten": [
        "{}",
        '{"empty": {}, "nil": null, "flag": false, "zero": 0}',
        '{"uni": "héllo wörld ✓", "neg": -3, "pi": 3.5}',
        '{"deep": {"deeper": {"deepest": [[1], []]}}}',
        "[]",
    ],
    "csv_select": [
        'name,city,note\n"","Nowhere","empty name"',
        'name,city,note\n"O\'Brien, Kim",Dublin,"says ""hi"""',
        "name,city,note\nZoë,Zürich,unicode ✓",
        'note,city,name\n"trailing","comma, here",Last',
    ],
    "kv_parse": [
        'level=info msg="" count=0',
        'user=bob note="multi word value here" ok=false',
        'uni="héllo ✓" neg=-12 ratio=3.5',
        "single=pair",
    ],
    "dates": [
        "Record 1: 29 Feb 2024 (leap day)",
        "Record 2: 1 Jan 2000 (y2k)\nRecord 3: 31 Dec 1999 (eve)",
        "Record 4: 2024/02/29 (iso-ish leap)",
        "Record 5: Feb 29, 2024 (us leap)",
    ],
}


def new_boundary_examples(kind, seed=0, count=4, params=None):
    """Generate fresh boundary cases with solver-computed expected outputs.

    ``params`` supplies kind parameters (required for ``csv_select``);
    ``seed`` rotates through the boundary pool deterministically.
    """
    if kind not in _BOUNDARY_INPUTS:
        raise ValueError("unknown case kind: %r" % kind)
    if count < 1:
        raise ValueError("count must be >= 1")
    pool = _BOUNDARY_INPUTS[kind]
    if kind == "csv_select" and not (params or {}).get("columns"):
        raise ValueError("csv_select boundary cases need params['columns']")
    cases = []
    for i in range(count):
        text = pool[(seed + i) % len(pool)]
        case = make_case(kind, text, params=params)
        case["transforms_applied"] = ["new_boundary_example"]
        case["name"] = "boundary-%s-%d" % (kind, seed + i)
        cases.append(case)
    return cases


# --------------------------------------------------------------------------
# Registry + pipelines
# --------------------------------------------------------------------------

TRANSFORMS = {
    "rename_identifiers": rename_identifiers,
    "permute_ordering": permute_ordering,
    "rescale_values": rescale_values,
    "inject_irrelevant_fields": inject_irrelevant_fields,
    "change_formatting": change_formatting,
}


def apply_transforms(case, specs):
    """Apply ``[(name, kwargs), ...]`` left to right, returning a new case."""
    for name, kwargs in specs:
        try:
            func = TRANSFORMS[name]
        except KeyError:
            raise ValueError("unknown transform: %r" % name)
        case = func(case, **dict(kwargs))
    return case
