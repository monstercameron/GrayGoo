"""Provenance audit: which saved Lisp functions the model wrote and which the harness supplied.

Every saved tool in a project's registry is classified by who wrote it:

- HARNESS_PRIMITIVE: supplied ready-made by the harness (the web kit in webkit.py).
- AGENT_GENERATED: written by the model; it stands alone or leans only on
  harness primitives and Common Lisp.
- AGENT_COMPOSED: written by the model and calling at least one other saved
  function that is not a harness primitive.
- IMPORTED: copied in from elsewhere (nothing writes this yet).

audit() turns the classification into the numbers a reviewer asks for: how
much of the application the model wrote (by function count and by characters
of definition), how often the model's functions reach into the harness, and
which functions count toward what the system learned.

Public API: classify, audit, project_tools, render, main.

Usage: uv run python provenance.py [project] [--mode live|demo] [--json]
"""

import argparse
import collections
import json
import sys
from pathlib import Path

import agent_session
import projects
import s_expr
import webkit

HARNESS_PRIMITIVE = "HARNESS_PRIMITIVE"
AGENT_GENERATED = "AGENT_GENERATED"
AGENT_COMPOSED = "AGENT_COMPOSED"
IMPORTED = "IMPORTED"

PROVENANCES = (HARNESS_PRIMITIVE, AGENT_GENERATED, AGENT_COMPOSED, IMPORTED)
AGENT_KINDS = (AGENT_GENERATED, AGENT_COMPOSED)
NOT_LEARNED_KINDS = (HARNESS_PRIMITIVE, IMPORTED)
ENTRY_NAMES = ("handle-request", "handle-command", "initial-state")
BUILTIN_PROJECT = projects.BUILTIN

# The shipped kit definitions with whitespace collapsed, keyed by kit name.
_KIT = {t["name"]: " ".join(t["definition"].split()) for t in webkit.KIT}


# -- classification ------------------------------------------------------------

def _collapse(text):
    return " ".join(text.split()) if isinstance(text, str) else None


def _is_kit_copy(name, definition):
    """True when DEFINITION is the shipped kit definition of the kit name NAME."""
    return name in webkit.NAMES and _collapse(definition) == _KIT.get(name)


def _symbol(x):
    """True for a symbol the reader returns: a plain str that is neither a string literal nor a keyword."""
    return isinstance(x, str) and not isinstance(x, s_expr.SString) and not x.startswith(":")


def _is_quote(item):
    return bool(item) and _symbol(item[0]) and item[0].lower() == "quote"


def _list_at(node, i):
    """NODE[i] when it is a list, else an empty list."""
    return node[i] if len(node) > i and isinstance(node[i], list) else []


def _push_code(items, refs, stack):
    """Queue the list items of ITEMS that are code; quoted and backquoted data is skipped.

    ``#'name`` is a use of NAME and is appended to REFS; ``#'(lambda ...)`` is code.
    The reader drops commas, so a backquoted form cannot be split into data and
    code; it is treated as data, the same way interfaces.py treats it.
    """
    for i, item in enumerate(items):
        if not isinstance(item, list) or not item:
            continue
        before = items[i - 1] if i else None
        if _symbol(before) and before == "#" and _is_quote(item):
            target = item[1] if len(item) > 1 else None
            if _symbol(target):
                refs.append(target.lower())
            elif isinstance(target, list):
                stack.append(target)
        elif (_symbol(before) and before == "`") or _is_quote(item):
            continue
        else:
            stack.append(item)


def _references(form):
    """Lower-cased names that FORM calls or uses as a function, one entry per site.

    Names bound by flet, labels or macrolet inside the definition are left out,
    because calls to them never reach a saved function.
    """
    refs, local = [], set()
    stack = [form]
    while stack:
        node = stack.pop()
        head = node[0] if node else None
        if not _symbol(head):
            _push_code(node, refs, stack)
            continue
        h = head.lower()
        if h in ("quote", "declare"):
            continue
        if h == "#":
            _push_code(node, refs, stack)
        elif h == "function":
            if len(node) > 1 and _symbol(node[1]):
                refs.append(node[1].lower())
            else:
                _push_code(node[1:], refs, stack)
        elif h == "defun":
            _push_code(node[3:], refs, stack)
        elif h in ("lambda", "multiple-value-bind", "destructuring-bind"):
            _push_code(node[2:], refs, stack)
        elif h in ("let", "let*", "do", "do*"):
            for binding in _list_at(node, 1):
                if isinstance(binding, list):
                    _push_code(binding[1:], refs, stack)
            _push_code(node[2:], refs, stack)
        elif h in ("flet", "labels", "macrolet"):
            for binding in _list_at(node, 1):
                if isinstance(binding, list) and binding and _symbol(binding[0]):
                    local.add(binding[0].lower())
                    _push_code(binding[2:], refs, stack)
            _push_code(node[2:], refs, stack)
        elif h in ("dolist", "dotimes"):
            _push_code(_list_at(node, 1)[1:], refs, stack)
            _push_code(node[2:], refs, stack)
        elif h == "cond":
            for clause in node[1:]:
                if isinstance(clause, list) and clause:
                    if isinstance(clause[0], list):
                        stack.append(clause[0])
                    _push_code(clause[1:], refs, stack)
        elif h in ("case", "ecase", "typecase", "etypecase", "ccase", "ctypecase"):
            _push_code(node[1:2], refs, stack)
            for clause in node[2:]:
                if isinstance(clause, list):
                    _push_code(clause[1:], refs, stack)
        elif h == "handler-case":
            _push_code(node[1:2], refs, stack)
            for clause in node[2:]:
                if isinstance(clause, list):
                    _push_code(clause[2:], refs, stack)
        else:
            refs.append(h)
            _push_code(node[1:], refs, stack)
    return [r for r in refs if r not in local]


def _parse(definition):
    """The parsed form of DEFINITION, or None when the reader rejects it."""
    if not isinstance(definition, str):
        return None
    try:
        form = s_expr.parse(definition)
    except Exception:  # the reader raises SExprError; any failure means: unparsed
        return None
    return form if isinstance(form, list) and form else None


def _explicit_provenance(tool, name, definition):
    """The provenance the record fixes by itself, or None when the model's code decides."""
    if tool.get("kit") or _is_kit_copy(name, definition):
        return HARNESS_PRIMITIVE
    explicit = tool.get("provenance")
    if isinstance(explicit, str) and explicit in PROVENANCES:
        return explicit
    if tool.get("imported") or tool.get("imported_from"):
        return IMPORTED
    return None


def _fixes(tool):
    for key in ("auto_fixes", "fixes"):
        value = tool.get(key)
        if isinstance(value, list):
            return list(value)
    return []


def _analyse(tools):
    """``(records, sites)`` for the live tools among TOOLS.

    ``records`` maps each name to its record (see classify). ``sites`` maps each
    name to a Counter of the harness primitives it refers to, one count per site.
    """
    live = {}
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("retired"):
            continue
        name = tool.get("name")
        if isinstance(name, str) and name:
            live[name] = tool              # a later entry replaces an earlier one, as the registry does
    by_lower = {}
    for name in live:
        by_lower.setdefault(name.lower(), name)

    forms, base, refs = {}, {}, {}
    for name, tool in live.items():
        definition = tool.get("definition")
        forms[name] = _parse(definition)
        base[name] = _explicit_provenance(tool, name, definition)
        refs[name] = _references(forms[name]) if forms[name] else []

    records, sites = {}, {}
    for name, tool in live.items():
        # A function's own name is not "another" saved function, so a self-call is skipped.
        callees = [by_lower[r] for r in refs[name] if r in by_lower and by_lower[r] != name]
        harness = collections.Counter(c for c in callees if base[c] == HARNESS_PRIMITIVE)
        agent_calls = sorted({c for c in callees if base[c] != HARNESS_PRIMITIVE})
        provenance = base[name] or (AGENT_COMPOSED if agent_calls else AGENT_GENERATED)
        definition = tool.get("definition")
        tests = tool.get("tests")
        record = {
            "provenance": provenance,
            "calls_agent": agent_calls,
            "calls_harness": sorted(harness),
            "chars": len(definition) if isinstance(definition, str) else 0,
            "tests": len(tests) if isinstance(tests, list) else 0,
            "harness_fixes": _fixes(tool),
            "modified_kit": (provenance in AGENT_KINDS and name in webkit.NAMES
                             and _collapse(definition) != _KIT.get(name)),
        }
        if forms[name] is None:
            record["unparsed"] = True
        records[name] = record
        sites[name] = harness
    return records, sites


def classify(tools):
    """Map each live tool name to its provenance record.

    A record holds: provenance, calls_agent and calls_harness (sorted names of
    the saved functions it calls), chars, tests, harness_fixes, modified_kit,
    and unparsed (only when the definition does not parse).
    """
    return _analyse(tools)[0]


# -- the audit -----------------------------------------------------------------

def _verb(n):
    return "was" if n == 1 else "were"


def _plural(n, word):
    return "%d %s" % (n, word if n == 1 else word + "s")


def _statement(functions, agent_chars_share, calls):
    total = functions["total"]
    if not total:
        return "This app has no saved functions yet."
    agent = sum(functions[k] for k in AGENT_KINDS)
    harness = functions[HARNESS_PRIMITIVE]
    imported = functions[IMPORTED]
    share = "" if agent_chars_share is None else \
        " (%d%% of the code by length)" % round(100 * agent_chars_share)
    text = "Of %s in this app, %d %s written by the model%s, %d %s supplied by the harness" % (
        _plural(total, "function"), agent, _verb(agent), share, harness, _verb(harness))
    if imported:
        text += " and %d %s imported from elsewhere" % (imported, _verb(imported))
    return text + "; the model's functions call the supplied ones %s." % _plural(calls, "time")


def _audit(records, sites):
    functions = dict.fromkeys(("total",) + PROVENANCES, 0)
    chars = dict.fromkeys(("total",) + PROVENANCES, 0)
    for record in records.values():
        functions[record["provenance"]] += 1
        chars[record["provenance"]] += record["chars"]
    functions["total"] = len(records)
    chars["total"] = sum(chars[k] for k in PROVENANCES)

    agent_functions = sum(functions[k] for k in AGENT_KINDS)
    agent_chars = sum(chars[k] for k in AGENT_KINDS)
    share = {
        "functions": agent_functions / functions["total"] if functions["total"] else None,
        "chars": agent_chars / chars["total"] if chars["total"] else None,
    }

    found = {name.lower(): name for name in records}
    entry_points = [{"name": found[e], "provenance": records[found[e]]["provenance"]}
                    for e in ENTRY_NAMES if e in found]

    agent_names = sorted(n for n, r in records.items() if r["provenance"] in AGENT_KINDS)
    by_primitive = collections.Counter()
    for name in agent_names:
        by_primitive.update(sites[name])
    ordered = sorted(by_primitive.items(), key=lambda kv: (-kv[1], kv[0]))
    reach = {"calls": sum(by_primitive.values()), "by_primitive": dict(ordered)}

    using_harness = sum(1 for n in agent_names if records[n]["calls_harness"])
    standing_alone = sum(1 for n in agent_names
                         if not records[n]["calls_agent"] and not records[n]["calls_harness"])
    not_learned = sorted(n for n, r in records.items() if r["provenance"] in NOT_LEARNED_KINDS)

    return {
        "functions": functions,
        "chars": chars,
        "agent_share": share,
        "entry_points": entry_points,
        "harness_reach": reach,
        "agent_functions_using_harness": using_harness,
        "agent_functions_standing_alone": standing_alone,
        "learned": agent_names,
        "not_learned": not_learned,
        "statement": _statement(functions, share["chars"], reach["calls"]),
    }


def audit(tools):
    """The provenance numbers for the live tools among TOOLS (see module docstring)."""
    return _audit(*_analyse(tools))


# -- reading projects ----------------------------------------------------------

def project_tools(project=None, mode="live", agent_dir=None):
    """The tools of PROJECT (None or "scratch" is the built-in registry), filtered by MODE.

    Read through ToolRegistry, so mode filtering matches the rest of the system.
    AGENT_DIR defaults to artifacts/agent. Raises ValueError for an unknown project.
    """
    base = Path(agent_dir) if agent_dir else agent_session.AGENT_DIR
    store = projects.ProjectStore(base)
    project_id = BUILTIN_PROJECT if project in (None, "") else project
    if not store.exists(project_id):
        raise ValueError("no such project: %s" % project)
    registry = agent_session.ToolRegistry(store.tools_path(project_id))
    return registry.for_mode(mode).load()


# -- rendering -----------------------------------------------------------------

def _describe(name, record):
    bits = ["%d chars" % record["chars"], _plural(record["tests"], "test")]
    if record["calls_harness"]:
        bits.append("harness: " + ", ".join(record["calls_harness"]))
    if record["calls_agent"]:
        bits.append("saved: " + ", ".join(record["calls_agent"]))
    if record["modified_kit"]:
        bits.append("rewrites a kit helper")
    if record.get("unparsed"):
        bits.append("does not parse")
    if record["harness_fixes"]:
        bits.append("%d harness fixes" % len(record["harness_fixes"]))
    return "%-32s %s" % (name, "; ".join(bits))


def render(audit_dict, classified):
    """Plain text: the statement, the provenance table, then the functions grouped by provenance."""
    functions, chars = audit_dict["functions"], audit_dict["chars"]
    lines = [audit_dict["statement"], ""]

    rows = [("provenance", "functions", "share of code")]
    for kind in PROVENANCES:
        share = "n/a" if not chars["total"] else "%d%%" % round(100 * chars[kind] / chars["total"])
        rows.append((kind, str(functions[kind]), share))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        lines.append("%-*s  %*s  %*s" % (widths[0], row[0], widths[1], row[1], widths[2], row[2]))

    reach = audit_dict["harness_reach"]
    top = ", ".join("%s (%d)" % kv for kv in list(reach["by_primitive"].items())[:5])
    lines.append("")
    lines.append("Harness calls by the model's functions: %d%s" % (
        reach["calls"], " (most used: %s)" % top if top else ""))
    if audit_dict["entry_points"]:
        lines.append("Entry points: " + ", ".join(
            "%s (%s)" % (e["name"], e["provenance"]) for e in audit_dict["entry_points"]))

    for kind in PROVENANCES:
        names = sorted(n for n, r in classified.items() if r["provenance"] == kind)
        lines.append("")
        lines.append("%s (%d)" % (kind, len(names)))
        for name in names:
            lines.append("  " + _describe(name, classified[name]))
    return "\n".join(lines)


# -- command line --------------------------------------------------------------

def _report(tools):
    records, sites = _analyse(tools)
    return {"audit": _audit(records, sites), "functions": records}


def _summary_line(meta, report):
    a = report["audit"]
    share = a["agent_share"]["chars"]
    pct = "n/a" if share is None else "%d%%" % round(100 * share)
    fn = a["functions"]
    return "%-28s %-24s %4d functions  model %4d (%s of code)  harness %4d  imported %3d" % (
        meta["id"], meta["name"][:24], fn["total"],
        fn[AGENT_GENERATED] + fn[AGENT_COMPOSED], pct, fn[HARNESS_PRIMITIVE], fn[IMPORTED])


def main(argv=None, agent_dir=None):
    parser = argparse.ArgumentParser(
        prog="provenance.py",
        description="Who wrote each saved function: the model or the harness.")
    parser.add_argument("project", nargs="?", default=None,
                        help="project id (default: one summary line per project)")
    parser.add_argument("--mode", choices=("live", "demo"), default="live")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    base = Path(agent_dir) if agent_dir else agent_session.AGENT_DIR

    if args.project is None:
        metas = projects.ProjectStore(base).list()
        if args.json:
            out = {m["id"]: _report(project_tools(m["id"], args.mode, base)) for m in metas}
            print(json.dumps(out, indent=2))
        else:
            for meta in metas:
                print(_summary_line(meta, _report(project_tools(meta["id"], args.mode, base))))
        return 0

    try:
        tools = project_tools(args.project, args.mode, base)
    except ValueError as exc:
        print("provenance: %s" % exc, file=sys.stderr)
        return 2
    report = _report(tools)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report["audit"], report["functions"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
