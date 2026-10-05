"""Reference generator for supplemental transfer tasks A-TRN-09..16.

Family A follows an append-only convention (benchmarks/family-a/README.md
"Adding tasks"): existing tasks are frozen, new ids are sequential. This
script computes every expected output with INDEPENDENT stdlib reference
implementations (datetime/csv/json/re/shlex + small hand parsers) and
appends the 8 tasks to transfer.json. Deterministic: re-running
produces byte-identical task JSON (verified by tests/test_transfer2.py).

Novelty vs TRN-01..08 (RFC-2822, ISO week, semicolon, comment-CSV,
deep merge, address norm, syslog, sessionization): epoch seconds,
12-hour+EST, TSV with quoted tabs, backslash-escaped pipes, unflatten,
group-by-count, Apache combined logs, quoted key=value.

Usage: uv run python benchmarks/family-a/make_transfer2.py [--check]
  --check verifies transfer.json already contains byte-identical tasks.
"""

import csv
import io
import json
import re
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

FAMILY_DIR = Path(__file__).resolve().parent
TRANSFER_PATH = FAMILY_DIR / "transfer.json"


# -- reference implementations (independent of any model) ---------------

def ref_epoch_to_iso(text):
    out = []
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for line in text.splitlines():
        seconds = int(line.strip())
        # Timedelta arithmetic (not fromtimestamp: Windows rejects
        # pre-1970 values).
        out.append((epoch + timedelta(seconds=seconds)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"))
    return "\n".join(out)


def ref_12h_est_to_iso(text):
    # All inputs are EST (UTC-5), January dates only: no DST ambiguity.
    out = []
    for line in text.splitlines():
        naive = datetime.strptime(line.strip(), "%m/%d/%Y %I:%M:%S %p")
        out.append((naive + timedelta(hours=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"))
    return "\n".join(out)


def ref_tsv_to_json(text):
    rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
    header, body = rows[0], rows[1:]
    return json.dumps([dict(zip(header, row)) for row in body])


def ref_pipe_to_json(text):
    rows = []
    for line in text.splitlines():
        fields, current, escaped = [], "", False
        for char in line:
            if escaped:
                current += char
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "|":
                fields.append(current)
                current = ""
            else:
                current += char
        fields.append(current)
        rows.append(fields)
    header, body = rows[0], rows[1:]
    return json.dumps([dict(zip(header, row)) for row in body])


def ref_unflatten(text):
    flat = json.loads(text)
    root = {}
    for dotted, value in flat.items():
        node = root
        keys = dotted.split(".")
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value
    return json.dumps(root)


def ref_group_count(text):
    rows = json.loads(text)
    counts = {}
    for row in rows:
        key = row["status"]
        counts[key] = counts.get(key, 0) + 1
    return json.dumps(counts)


_APACHE_RE = re.compile(
    r'(\S+) \S+ (\S+) \[([^\]]+)\] "(\S+) (\S+) \S+" (\d{3}) (\S+)')


def ref_apache_to_json(text):
    out = []
    for line in text.splitlines():
        match = _APACHE_RE.match(line)
        host, user, _, method, path, status, size = match.groups()
        out.append({"host": host, "user": None if user == "-" else user,
                    "method": method, "path": path,
                    "status": int(status),
                    "bytes": 0 if size == "-" else int(size)})
    return json.dumps(out)


def ref_kv_to_json(text):
    out = {}
    for token in shlex.split(text):
        key, _, value = token.partition("=")
        out[key] = value
    return json.dumps(out)


# -- task definitions (inputs only; expected computed above) -------------

TASKS = [
    {
        "id": "A-TRN-09", "family": "A", "split": "transfer",
        "category": "dates",
        "prompt": ("Each input line is Unix epoch seconds (integer, may be "
                   "negative for pre-1970). Convert each line to UTC ISO-8601 "
                   "'YYYY-MM-DDTHH:MM:SSZ', one per output line, same order. "
                   "Output only the converted datetimes."),
        "notes": ("Held-out: raw integer epochs incl. a negative (pre-1970) "
                  "value; no weekday/offset decoration unlike A-TRN-01."),
        "checks": [
            {"input": "0\n946684800", "compare": "exact",
             "ref": ref_epoch_to_iso},
            {"input": "-86400\n1728000000", "compare": "exact",
             "ref": ref_epoch_to_iso},
        ],
    },
    {
        "id": "A-TRN-10", "family": "A", "split": "transfer",
        "category": "dates",
        "prompt": ("Each input line is 'MM/DD/YYYY HH:MM:SS AM|PM' in US "
                   "Eastern Standard Time (UTC-5; all dates are January so "
                   "no daylight saving applies). Convert each line to UTC "
                   "ISO-8601 'YYYY-MM-DDTHH:MM:SSZ', one per output line, "
                   "same order. Output only the converted datetimes."),
        "notes": ("Held-out: 12-hour clock plus named-zone offset in one "
                  "step; midnight/noon meridiem edges included."),
        "checks": [
            {"input": "01/15/2026 12:00:00 AM\n01/15/2026 12:00:00 PM",
             "compare": "exact", "ref": ref_12h_est_to_iso},
            {"input": "01/02/2026 11:59:59 PM\n01/31/2026 01:02:03 AM",
             "compare": "exact", "ref": ref_12h_est_to_iso},
        ],
    },
    {
        "id": "A-TRN-11", "family": "A", "split": "transfer",
        "category": "csv",
        "prompt": ("Input is tab-separated values: first line is the header, "
                   "fields may be double-quoted (quotes may contain literal "
                   "tabs). Output a JSON array of objects, one per data row, "
                   "keys from the header. Output only the JSON."),
        "notes": ("Held-out: tab delimiter with quoted-tab fields; goes "
                  "beyond semicolon/comment variants in TRN-03/04."),
        "checks": [
            {"input": "name\tcity\namy\toslo",
             "compare": "json", "ref": ref_tsv_to_json},
            {"input": "a\tb\n\"x\ty\"\tz",
             "compare": "json", "ref": ref_tsv_to_json},
        ],
    },
    {
        "id": "A-TRN-12", "family": "A", "split": "transfer",
        "category": "csv",
        "prompt": ("Input is pipe-separated values: first line is the "
                   "header. A backslash escapes the next character (\\| is "
                   "a literal pipe, \\\\ is a literal backslash). Output a "
                   "JSON array of objects, one per data row. Output only "
                   "the JSON."),
        "notes": ("Held-out: backslash-escape discipline instead of "
                  "quoting; the escape char itself appears in check 2."),
        "checks": [
            {"input": "a|b\nx|y",
             "compare": "json", "ref": ref_pipe_to_json},
            {"input": "p|q\nv\\|w|x\\\\y",
             "compare": "json", "ref": ref_pipe_to_json},
        ],
    },
    {
        "id": "A-TRN-13", "family": "A", "split": "transfer",
        "category": "records",
        "prompt": ("Input is one flat JSON object whose keys are dot paths "
                   "(e.g. {\"a.b\": 1}). Output the nested JSON object it "
                   "denotes. Output only the JSON."),
        "notes": ("Held-out: inverse direction of the flatten tasks in "
                  "exposure; shared-prefix merging required."),
        "checks": [
            {"input": "{\"a.b\": 1, \"a.c\": 2}",
             "compare": "json", "ref": ref_unflatten},
            {"input": "{\"x\": 0, \"a.b.c\": 3, \"a.b.d\": 4}",
             "compare": "json", "ref": ref_unflatten},
        ],
    },
    {
        "id": "A-TRN-14", "family": "A", "split": "transfer",
        "category": "records",
        "prompt": ("Input is a JSON array of objects, each with a \"status\" "
                   "string field. Output one JSON object mapping each "
                   "distinct status to its count. Output only the JSON."),
        "notes": ("Held-out: aggregation over records (group-by-count); "
                  "exposure aggregates logs, never record arrays."),
        "checks": [
            {"input": "[{\"status\": \"ok\"}, {\"status\": \"err\"}, "
                      "{\"status\": \"ok\"}]",
             "compare": "json", "ref": ref_group_count},
            {"input": "[{\"status\": \"x\", \"n\": 1}, {\"status\": \"y\"}, "
                      "{\"status\": \"x\", \"n\": 2}]",
             "compare": "json", "ref": ref_group_count},
        ],
    },
    {
        "id": "A-TRN-15", "family": "A", "split": "transfer",
        "category": "logs",
        "prompt": ("Each input line is an Apache combined log line. Output "
                   "a JSON array with one object per line: {\"host\", "
                   "\"user\", \"method\", \"path\", \"status\" (number), "
                   "\"bytes\" (number, 0 when the field is '-')}. "
                   "A '-' user means absent: emit null. "
                   "Ignore ident, timestamp, referrer, and user-agent. "
                   "Output only the JSON."),
        "notes": ("Held-out: combined format with quoted request plus "
                  "two trailing quoted fields; '-' byte counts included."),
        "checks": [
            {"input": "127.0.0.1 - amy [10/Oct/2026:13:55:36 +0000] "
                      "\"GET /a HTTP/1.1\" 200 123",
             "compare": "json", "ref": ref_apache_to_json},
            {"input": "10.0.0.2 - - [10/Oct/2026:13:56:01 +0000] "
                      "\"POST /b?c=d HTTP/1.1\" 404 -",
             "compare": "json", "ref": ref_apache_to_json},
        ],
    },
    {
        "id": "A-TRN-16", "family": "A", "split": "transfer",
        "category": "logs",
        "prompt": ("Input is one line of space-separated key=value pairs; "
                   "values may be single- or double-quoted and quoted "
                   "values may contain spaces and '='. Output one JSON "
                   "object of the pairs (quotes removed). Output only "
                   "the JSON."),
        "notes": ("Held-out: quoted values with interior spaces and '='; "
                  "naive whitespace splitting fails."),
        "checks": [
            {"input": "a=1 b=2",
             "compare": "json", "ref": ref_kv_to_json},
            {"input": "msg=\"hello world\" k='a=b' n=3",
             "compare": "json", "ref": ref_kv_to_json},
        ],
    },
]


def build_tasks():
    """Render task dicts (schema shape) with computed expected outputs."""
    rendered = []
    for task in TASKS:
        checks = []
        for check in task["checks"]:
            checks.append({
                "input": check["input"],
                "expected": check["ref"](check["input"]),
                "compare": check["compare"],
            })
        rendered.append({
            "id": task["id"],
            "family": task["family"],
            "split": task["split"],
            "category": task["category"],
            "prompt": task["prompt"],
            "checks": checks,
            "notes": task["notes"],
        })
    return rendered


def main(argv):
    with open(TRANSFER_PATH, encoding="utf-8") as handle:
        existing = json.load(handle)
    rendered = build_tasks()
    have = {t["id"]: t for t in existing
            if t.get("id", "").startswith("A-TRN-")}
    if "--check" in argv:
        for task in rendered:
            current = have.get(task["id"])
            if current != task:
                print("MISMATCH or missing: %s" % task["id"])
                return 1
        print("transfer-2 tasks match (%d)" % len(rendered))
        return 0
    for task in rendered:
        if task["id"] in {t.get("id") for t in existing}:
            print("already present, refusing to duplicate: %s"
                  % task["id"])
            return 1
    # Textual append: the frozen tasks must stay byte-identical, so
    # never re-serialize the whole file (formatting would drift).
    with open(TRANSFER_PATH, encoding="utf-8") as handle:
        raw = handle.read().rstrip()
    if not raw.endswith("]"):
        print("unexpected transfer.json tail; refusing to append")
        return 1
    blobs = []
    for task in rendered:
        blob = json.dumps(task, indent=2, ensure_ascii=False)
        blobs.append("\n".join("  " + line for line in blob.splitlines()))
    new_raw = (raw[:-1].rstrip() + ",\n" + ",\n".join(blobs) + "\n]")
    with open(TRANSFER_PATH, "w", encoding="utf-8", newline="") as handle:
        handle.write(new_raw)
    print("appended %d tasks to %s" % (len(rendered), TRANSFER_PATH))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
