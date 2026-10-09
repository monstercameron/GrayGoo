"""What a saved function touches: its effects and the kinds of latency it incurs.

Every tool the agent builds is a pure Lisp function, so "effects" here are not
side effects inside the function. They say what the function's inputs and
outputs MEAN once the harness runs the app: does it read the request, read or
replace the stored state, produce the HTTP response, depend on the time or the
random nonce the harness passes in. That is what the graph's icons show.

Pure functions of text: ``describe`` and ``describe_all`` read definitions and
return plain dicts. Nothing here runs Lisp.
"""
import re

import lispstyle

ENTRY_POINTS = ("handle-request", "handle-command")

LABELS = {
    "pure": "pure: result depends only on its arguments",
    "reads-request": "reads the incoming request",
    "reads-state": "reads the app's stored data",
    "writes-state": "returns new app data, which the harness saves to disk",
    "http-response": "produces the HTTP response",
    "sets-cookie": "sets a browser cookie",
    "uses-time": "depends on the time passed in with the request",
    "uses-random": "depends on the random nonce passed in with the request",
    "entry-point": "called by the harness for every request or command",
    "kit": "supplied by the harness (web kit), not written by the model",
}

# effect -> the kind of latency it implies when the app runs
LATENCY_OF = {"writes-state": "disk", "http-response": "network", "entry-point": "network",
              "sets-cookie": "network"}

_ORDER = ("entry-point", "http-response", "writes-state", "reads-state", "reads-request",
          "sets-cookie", "uses-time", "uses-random", "kit", "pure")


def _sym(name):
    """Regex for NAME used as a whole Lisp symbol."""
    return re.compile(r"(?<![^\s('#])%s(?![^\s)])" % re.escape(name), re.I)


def calls(tool, tools):
    """Names of the other TOOLS this tool's definition calls, in registry order."""
    code = lispstyle.code_only(tool.get("definition") or "")
    return [t["name"] for t in tools
            if t["name"] != tool["name"] and _sym(t["name"]).search(code)]


def own_effects(tool):
    """Effect keys visible in this tool's own definition (callees not followed)."""
    name = tool.get("name") or ""
    code = lispstyle.code_only(tool.get("definition") or "")
    parts = lispstyle.defun_parts(tool.get("definition") or "")
    params = parts[1] if parts else []
    out = []

    def has(*patterns):
        return any(re.search(p, code, re.I) for p in patterns)

    if name in ENTRY_POINTS:
        out.append("entry-point")
    if tool.get("kit") or tool.get("session") == "web-kit":
        out.append("kit")
    if has(r":state\b") and has(r"\(\s*(list|append|cons)\b"):
        out.append("writes-state")
    if has(r":status\b", r":body\b", r":output\b"):
        out.append("http-response")
    if re.search(r"set-cookie", tool.get("definition") or "", re.I):   # the header name is a string
        out.append("sets-cookie")
    if has(r":now\b"):
        out.append("uses-time")
    if has(r":nonce\b"):
        out.append("uses-random")
    if "request" in params and has(r"\(\s*getf\s+request\b", r"\brequest\b.*:(?:form|query|cookies|path|method)"):
        out.append("reads-request")
    # reading means looking INSIDE the state; merely passing it along is not a read
    if "state" in params and has(
            r"\(\s*(assoc|second|first|car|cdr|cadr|getf|find|find-if|remove|remove-if|"
            r"mapcar|length|member|nth|elt)\b[^()]*\bstate\b"):
        out.append("reads-state")
    return out


def describe_all(tools):
    """``{name: meta}`` for every tool, with effects inherited through callees."""
    graph = {t["name"]: calls(t, tools) for t in tools}
    own = {t["name"]: own_effects(t) for t in tools}
    callers = {n: [m for m, ds in graph.items() if n in ds] for n in graph}
    memo = {}

    def reach(name, seen):
        """{effect: via} for NAME: its own (via None) plus what its callees do."""
        if name in memo:
            return memo[name]
        result = {k: None for k in own[name]}
        for callee in graph[name]:
            if callee in seen:
                continue
            for key in reach(callee, seen | {name}):
                if key in ("kit", "entry-point"):
                    continue                      # these describe the callee itself
                result.setdefault(key, callee)
        memo[name] = result
        return result

    out = {}
    for t in tools:
        name = t["name"]
        eff = reach(name, frozenset())
        if not any(k not in ("kit", "entry-point") for k in eff):
            eff = dict(eff, pure=None)
        keys = sorted(eff, key=_ORDER.index)
        kinds = ["cpu"] + sorted({LATENCY_OF[k] for k in keys if k in LATENCY_OF})
        out[name] = {
            "effects": [{"key": k, "label": LABELS[k], "via": eff[k]} for k in keys],
            "latency": {"kinds": kinds, "cpu_ms": t.get("eval_ms")},
            "calls": graph[name], "callers": callers[name],
        }
    return out
