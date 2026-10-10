"""Cross-function checks for the saved Lisp tools of a harness-built web app.

Every saved tool passes its own tests, yet tools are written one model call at
a time, so they drift apart: a page posts a form to a path the router does not
route, one function calls another with the wrong number of arguments, a page
reads a state table that initial-state never creates. The functions here find
those problems from the saved definitions alone.

Definitions are read with the repo's reader, ``s_expr.parse``: lists become
Python lists, string literals ``s_expr.SString`` (a ``str`` subclass), symbols
and keywords plain ``str`` (keywords start with a colon), ``'x`` becomes
``["quote", x]`` and ``#'f`` becomes the symbol ``"#"`` followed by
``["quote", f]``. A definition the reader rejects is skipped. Every scan is
linear in the text: plain string search and character loops, no backtracking
patterns.

Public API: ``check``, ``call_arity_problems`` and ``routes``.
"""

import collections
import functools

import s_expr

KINDS = ("arity", "dead-route", "method-mismatch", "unrouted-handler", "unknown-table",
         "missing-helper")

HELPER_PREFIXES = ("render-", "handle-", "find-", "add-", "update-", "delete-", "make-")
HELPER_SUFFIXES = ("-html", "-page", "-row")
TABLE_CALLS = ("table-rows", "with-table-rows")
SPACE = " \t\r\n"

# Common Lisp functions whose names look like the app's helpers but are not.
COMMON_LISP = frozenset((
    "make-hash-table", "make-array", "make-string", "make-list", "make-symbol",
    "make-instance", "make-sequence", "make-package", "make-pathname",
    "make-random-state", "make-condition", "make-load-form",
    "make-string-input-stream", "make-string-output-stream",
    "make-broadcast-stream", "make-echo-stream", "make-concatenated-stream",
    "make-two-way-stream", "make-synonym-stream",
    "find-if", "find-if-not", "find-class", "find-symbol", "find-package",
    "find-restart", "find-method", "find-all-symbols",
    "delete-if", "delete-if-not", "delete-duplicates", "delete-file",
    "delete-package", "add-method", "update-instance-for-redefined-class",
    "update-instance-for-different-class",
))

_Row = collections.namedtuple("_Row", "name kit form facts")


# -- reading -----------------------------------------------------------------

def _sym(x):
    """True for a symbol: a plain str that is neither a string literal nor a keyword."""
    return isinstance(x, str) and not isinstance(x, s_expr.SString) and not x.startswith(":")


def _keyword(x):
    return isinstance(x, str) and not isinstance(x, s_expr.SString) and x.startswith(":")


def _pre_order(form):
    """Every list inside FORM, FORM first, parents before their children."""
    stack = [form]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(x for x in reversed(node) if isinstance(x, list))


def _strings(form):
    for node in _pre_order(form):
        for x in node:
            if isinstance(x, s_expr.SString):
                yield str(x)


def _second(node):
    return node[1] if len(node) > 1 and isinstance(node[1], list) else []


def _defun(form):
    """(name, parameters) of a (defun name (parameters) ...) form, or None."""
    if len(form) >= 3 and _sym(form[0]) and form[0].lower() == "defun" \
            and _sym(form[1]) and isinstance(form[2], list):
        return form[1], form[2]
    return None


def _bound(lambda_list):
    """Lower-cased names that a lambda list or a binding list introduces."""
    out = set()
    for item in lambda_list:
        if isinstance(item, list):
            if item and isinstance(item[0], list):
                out |= _bound(item)
            elif item and _sym(item[0]):
                out.add(item[0].lower())
        elif _sym(item) and not item.startswith("&"):
            out.add(item.lower())
    return out


@functools.lru_cache(maxsize=1024)
def _analysed(text):
    """``(form, facts)`` of one definition's text, or None when the reader rejects it."""
    try:
        form = s_expr.parse(text)
    except Exception:  # the reader raises SExprError; any failure means: skip this tool
        return None
    if not isinstance(form, list) or not form:
        return None
    return form, _facts(form)


def _name(tool):
    name = tool.get("name")
    return name.lower() if isinstance(name, str) else ""


def _rows(tools, exclude=None):
    """One row per live tool: name, kit flag, parsed form (or None) and facts (or None)."""
    rows = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("retired"):
            continue
        name = _name(tool)
        if not name or name == exclude:
            continue
        text = tool.get("definition")
        got = _analysed(text) if isinstance(text, str) else None
        form, facts = got if got else (None, None)
        rows.append(_Row(name, bool(tool.get("kit")), form, facts))
    return rows


def _find(rows, name):
    return next((row for row in rows if row.name == name), None)


def _unique(items):
    return list(dict.fromkeys(items))


def _problem(kind, where, detail):
    return {"kind": kind, "where": where, "detail": detail}


# -- code facts: calls, binders, symbols -------------------------------------

def _code(items, stack):
    """Push the list items of ITEMS that are code (not quoted data) onto STACK."""
    marker = None
    for item in items:
        if isinstance(item, list):
            quoted = bool(item) and _sym(item[0]) and item[0].lower() == "quote"
            if marker == "#" and quoted:
                # #'(lambda ...) is code; #'name is a reference, not a call
                if len(item) > 1 and isinstance(item[1], list):
                    stack.append(item[1])
            elif not quoted and marker != "`":
                stack.append(item)
        marker = item if _sym(item) and item in ("#", "`") else None


def _facts(form):
    """Calls in code position, names bound or defined locally, and every symbol.

    ``calls`` is a list of ``(name, argument_forms)`` with lower-cased names.
    ``shadowed`` holds every name that some parameter or binding introduces
    anywhere in the definition; ``local`` holds the flet/labels/macrolet names.
    """
    facts = {"calls": [], "shadowed": set(), "local": set(), "symbols": set()}
    calls, shadowed, local = facts["calls"], facts["shadowed"], facts["local"]
    stack = []
    defun = _defun(form)
    if defun:
        shadowed |= _bound(defun[1])
        _code(form[3:], stack)
    else:
        stack.append(form)
    while stack:
        node = stack.pop()
        head = node[0] if node else None
        if not _sym(head):
            _code(node, stack)
            continue
        h = head.lower()
        if h == "quote":
            continue
        if h == "function":
            _code(node[1:], stack)
        elif h == "lambda":
            shadowed |= _bound(_second(node))
            _code(node[2:], stack)
        elif h == "defun":
            shadowed |= _bound(node[2]) if len(node) > 2 and isinstance(node[2], list) else set()
            _code(node[3:], stack)
        elif h in ("let", "let*"):
            for b in _second(node):
                if _sym(b):
                    shadowed.add(b.lower())
                elif isinstance(b, list) and b:
                    if _sym(b[0]):
                        shadowed.add(b[0].lower())
                    _code(b[1:], stack)
            _code(node[2:], stack)
        elif h in ("flet", "labels", "macrolet"):
            for b in _second(node):
                if isinstance(b, list) and b and _sym(b[0]):
                    local.add(b[0].lower())
                    shadowed.add(b[0].lower())
                    if len(b) > 1 and isinstance(b[1], list):
                        shadowed |= _bound(b[1])
                    _code(b[2:], stack)
            _code(node[2:], stack)
        elif h in ("multiple-value-bind", "destructuring-bind"):
            shadowed |= _bound(_second(node))
            _code(node[2:], stack)
        elif h in ("dolist", "dotimes"):
            spec = _second(node)
            if spec and _sym(spec[0]):
                shadowed.add(spec[0].lower())
            _code(spec[1:], stack)
            _code(node[2:], stack)
        elif h in ("do", "do*"):
            for b in _second(node):
                if isinstance(b, list) and b and _sym(b[0]):
                    shadowed.add(b[0].lower())
                    _code(b[1:], stack)
                elif _sym(b):
                    shadowed.add(b.lower())
            _code(node[2:], stack)
        elif h == "loop":
            items = node[1:]
            for k in range(len(items) - 1):
                if _sym(items[k]) and items[k].lower() in ("for", "as", "with", "into") \
                        and _sym(items[k + 1]):
                    shadowed.add(items[k + 1].lower())
            _code(items, stack)
        elif h == "cond":
            for clause in node[1:]:
                if isinstance(clause, list) and clause:
                    if isinstance(clause[0], list):
                        stack.append(clause[0])  # a test is code; a bare symbol is not a call
                    _code(clause[1:], stack)
        elif h in ("case", "ecase", "typecase", "etypecase", "ccase", "ctypecase"):
            _code(node[1:2], stack)
            for clause in node[2:]:
                if isinstance(clause, list):
                    _code(clause[1:], stack)
        elif h == "handler-case":
            _code(node[1:2], stack)
            for clause in node[2:]:
                if isinstance(clause, list):
                    shadowed |= _bound(_second(clause))
                    _code(clause[2:], stack)
        else:
            calls.append((h, node[1:]))
            _code(node[1:], stack)
    for node in _pre_order(form):
        for x in node:
            if _sym(x):
                facts["symbols"].add(x.lower())
    return facts


def _table_names(form):
    """Table names the state literal of a definition creates, in order of appearance.

    A table is ``(name rows)``, with ROWS a list or NIL; a keyword name counts
    too. ``(list "name" ...)`` and ``(cons "name" ...)`` count as well. Anything
    else is ignored, so an unreadable state gives no names at all.
    """
    names = []
    for node in _pre_order(form):
        if not node:
            continue
        first = node[0]
        if len(node) == 2 and isinstance(first, s_expr.SString) and \
                (isinstance(node[1], list) or (_sym(node[1]) and node[1].lower() == "nil")):
            names.append(str(first))
        elif len(node) == 2 and _keyword(first) and isinstance(node[1], list):
            names.append(first[1:].lower())
        elif _sym(first) and first.lower() in ("list", "cons") and len(node) > 1:
            if isinstance(node[1], s_expr.SString):
                names.append(str(node[1]))
            elif _keyword(node[1]):
                names.append(node[1][1:].lower())
    return _unique(names)


# -- arity -------------------------------------------------------------------

def _arity_table(rows):
    """name -> (count, parameters) for tools with only required parameters, else None.

    A name saved twice with different lambda lists is left unchecked (None).
    """
    table = {}
    for row in rows:
        info = None
        defun = _defun(row.form) if row.form is not None else None
        if defun and all(_sym(p) and not p.startswith("&") for p in defun[1]):
            info = (len(defun[1]), list(defun[1]))
        if row.name in table and table[row.name] != info:
            table[row.name] = None
        else:
            table.setdefault(row.name, info)
    return table


def _words(n, noun):
    return "%d %s" % (n, noun if n == 1 else noun + "s")


def _arity_found(rows, table):
    out = []
    for row in rows:
        if row.facts is None:
            continue
        for callee, args in row.facts["calls"]:
            info = table.get(callee)
            if callee == row.name or callee in row.facts["shadowed"] or info is None \
                    or len(args) == info[0]:
                continue
            count, params = info
            takes = "no arguments" if count == 0 else "%d (%s)" % (count, " ".join(params))
            out.append(_problem("arity", row.name, "%s calls %s with %s, but %s takes %s." % (
                row.name, callee, _words(len(args), "argument"), callee, takes)))
    return out


def call_arity_problems(definition, tools):
    """Problems of ONE candidate definition against the saved TOOLS (used before a candidate is run)."""
    got = _analysed(definition) if isinstance(definition, str) else None
    if got is None:
        return []
    form, facts = got
    defun = _defun(form)
    where = defun[0].lower() if defun else "candidate"
    # the candidate may replace a saved tool of the same name, so that tool is left out
    rows = _rows(tools, exclude=where)
    return _arity_found([_Row(where, False, form, facts)], _arity_table(rows))


# -- routes ------------------------------------------------------------------

def _clean(path):
    """PATH without its query string or a trailing slash (the root keeps its slash)."""
    cut = path.split("?", 1)[0]
    if len(cut) > 1 and cut.endswith("/"):
        cut = cut.rstrip("/") or "/"
    return cut


def _key(path):
    """``(prefix, dynamic)``: PATH cut at a query string and at the first ~ directive."""
    cut = path.split("?", 1)[0]
    dynamic = "~" in cut
    if dynamic:
        cut = cut[:cut.index("~")]
    return (_clean(cut) if cut else "/"), dynamic


def _covered(key, keys):
    """True when a handled route (one of KEYS) answers the linked path KEY."""
    path, dynamic = key
    for prefix, route_dynamic in keys:
        if route_dynamic and prefix == "/":
            return True
        if path == prefix:
            return True
        if prefix != "/" and path.startswith(prefix + "/"):
            return True
        if dynamic and prefix.startswith(path):
            return True  # a dynamic link may be any of several routes: stay quiet
    return False


def _attr(text, k):
    """The value of an attribute whose value starts at K, quoted or bare (None if unclosed)."""
    if k < len(text) and text[k] in "\"'":
        end = text.find(text[k], k + 1)
        return None if end < 0 else text[k + 1:end]
    end = k
    while end < len(text) and text[end] not in SPACE + ">":
        end += 1
    return text[k:end]


def _method_of(tag):
    """The method a form tag declares, upper-cased; GET when it names none."""
    low = tag.lower()
    k = low.find("method")
    while k >= 0:
        j = k + 6
        while j < len(low) and low[j] in SPACE:
            j += 1
        if low[k - 1] in SPACE and j < len(low) and low[j] == "=":
            j += 1
            while j < len(low) and low[j] in SPACE:
                j += 1
            if j < len(low) and low[j] in "\"'":
                j += 1
            e = j
            while e < len(low) and low[e].isalpha():
                e += 1
            return low[j:e].upper() or "GET"
        k = low.find("method", k + 6)
    return "GET"


def _link_targets(text):
    """``(method, path)`` for each href and each form action in TEXT that starts with a slash, in order."""
    found = []
    i = text.find("href=")
    while i >= 0:
        if i == 0 or text[i - 1] in SPACE:
            value = _attr(text, i + 5)
            if value and value.startswith("/"):
                found.append((i, "GET", value))
        i = text.find("href=", i + 5)
    i = text.find("action=")
    while i >= 0:
        if i == 0 or text[i - 1] in SPACE:
            value = _attr(text, i + 7)
            if value and value.startswith("/"):
                start = text.rfind("<", 0, i)
                end = text.find(">", i)
                tag = text[start:end if end >= 0 else len(text)] if start >= 0 else ""
                if tag[:5].lower() == "<form":
                    found.append((i, _method_of(tag), value))
        i = text.find("action=", i + 7)
    return [(method, value) for _pos, method, value in sorted(found, key=lambda f: f[0])]


def _links(form):
    out = []
    for text in _strings(form):
        out.extend((method, _clean(path)) for method, path in _link_targets(text))
    return out


_EQUALITY = ("string=", "equal", "string-equal")


def _other_binders(form):
    """Lower-cased names that a binder other than let or let* introduces anywhere in FORM."""
    out = set()
    for node in _pre_order(form):
        if not node or not _sym(node[0]):
            continue
        h = node[0].lower()
        if h in ("lambda", "multiple-value-bind", "destructuring-bind"):
            out |= _bound(_second(node))
        elif h == "defun" and len(node) > 2 and isinstance(node[2], list):
            out |= _bound(node[2])
        elif h in ("flet", "labels"):
            for b in _second(node):
                if isinstance(b, list) and b and _sym(b[0]):
                    out.add(b[0].lower())
                    out |= _bound(_second(b))
        elif h in ("dolist", "dotimes"):
            spec = _second(node)
            if spec and _sym(spec[0]):
                out.add(spec[0].lower())
        elif h in ("do", "do*"):
            for b in _second(node):
                if isinstance(b, list) and b and _sym(b[0]):
                    out.add(b[0].lower())
        elif h == "handler-case":
            for clause in node[2:]:
                if isinstance(clause, list):
                    out |= _bound(_second(clause))
    return out


def _is_method_read(x):
    """True for a form that reads the request method: (getf r :method) or (request-field r :method)."""
    return isinstance(x, list) and len(x) == 3 and _sym(x[0]) \
        and x[0].lower() in ("getf", "request-field") \
        and _keyword(x[2]) and x[2].lower() == ":method"


def _method_vars(form):
    """Names that always hold the request method: every let or let* binding of the name reads it."""
    reads, other = set(), _other_binders(form)
    for node in _pre_order(form):
        if not node or not _sym(node[0]) or node[0].lower() not in ("let", "let*"):
            continue
        for b in _second(node):
            if isinstance(b, list) and b and _sym(b[0]):
                if len(b) > 1 and _is_method_read(b[1]):
                    reads.add(b[0].lower())
                else:
                    other.add(b[0].lower())
            elif _sym(b):
                other.add(b.lower())
    return reads - other


def _reads_method(x, method_vars):
    return _is_method_read(x) or (_sym(x) and x.lower() in method_vars)


def _test_methods(test, method_vars):
    """The GET or POST methods that a test requires: a set, empty when the test requires none.

    Only ``and`` conjuncts and the equality tests (string=, equal, string-equal) between
    the method and a literal count; anything else requires no method.
    """
    out = set()
    if not isinstance(test, list) or not test or not _sym(test[0]):
        return out
    h = test[0].lower()
    if h == "and":
        for part in test[1:]:
            out |= _test_methods(part, method_vars)
    elif h in _EQUALITY and len(test) == 3:
        for read, word in ((test[1], test[2]), (test[2], test[1])):
            if _reads_method(read, method_vars) and isinstance(word, s_expr.SString):
                literal = str(word).upper() if h == "string-equal" else str(word)
                if literal in ("GET", "POST"):
                    out.add(literal)
    return out


def _narrow(scope, required):
    """The method that holds inside a test requiring REQUIRED within SCOPE: one method, or None (any)."""
    if not required:
        return scope
    if len(required) == 1 and (scope is None or scope in required):
        return next(iter(required))
    return None


def _handled(form):
    """``(method_or_None, literal)`` for each path literal of handle-request, in order.

    A literal inside a test (a cond clause test, or the test of an if or when) is paired
    with the one method that the test and every enclosing test require, when that is GET
    or POST. Any other literal is paired with None, which means every method.
    """
    method_vars = _method_vars(form)
    out = []
    stack = [(form, None, False)]
    while stack:
        node, scope, in_test = stack.pop()
        if isinstance(node, s_expr.SString):
            if node.startswith("/"):
                out.append((scope if in_test else None, str(node)))
            continue
        if not isinstance(node, list):
            continue
        h = node[0].lower() if node and _sym(node[0]) else ""
        if h == "cond":
            pairs = []
            for clause in node[1:]:
                if isinstance(clause, list) and clause:
                    inner = _narrow(scope, _test_methods(clause[0], method_vars))
                    pairs.append((clause[0], inner, True))
                    pairs.extend((x, inner, False) for x in clause[1:])
        elif h in ("if", "when") and len(node) > 1:
            inner = _narrow(scope, _test_methods(node[1], method_vars))
            pairs = [(node[1], inner, True)]
            pairs.extend((x, inner if h == "when" or i == 0 else scope, False)
                         for i, x in enumerate(node[2:]))
        else:
            pairs = [(child, scope, in_test) for child in node]
        stack.extend(reversed(pairs))
    return _unique(out)


def _routes(rows):
    handle = _find(rows, "handle-request")
    handled = _handled(handle.form) if handle is not None and handle.form is not None else []
    linked = []
    for row in rows:
        if row.kit or row.name == "handle-request" or row.form is None:
            continue
        linked.extend((method, path, row.name) for method, path in _links(row.form))
    return handled, _unique(linked)


def routes(tools):
    """{"handled": [(method_or_None, path)], "linked": [(method, path, tool_name)]}"""
    handled, linked = _routes(_rows(tools))
    return {"handled": handled, "linked": linked}


def _dead_route_found(handled, linked):
    if not handled:
        return []
    keys = [_key(path) for _method, path in handled]
    listed = ", ".join(_unique(path for _method, path in handled))
    out = []
    for method, path, where in linked:
        if _covered(_key(path), keys):
            continue
        verb = "links to" if method == "GET" else "posts a form to"
        out.append(_problem("dead-route", where, "%s %s %s, but handle-request has no route "
                            "for it (it routes: %s)." % (where, verb, path, listed)))
    return out


def _method_found(handled, linked):
    """Links and forms to a path that handle-request routes, but never for the method they use.

    A path is covered exactly as in the dead-route check. A static path is compared with
    the routes of the same path; a dynamic one with every route under its prefix, so one
    route that takes the method is enough to stay quiet. A route with any method, or a
    path with no method test at all, is never reported.
    """
    table = {}
    for method, path in handled:
        prefix, dynamic = _key(path)
        table.setdefault(prefix, set()).add(None if dynamic else method)
    out = []
    for method, path, where in linked:
        if method not in ("GET", "POST"):
            continue
        prefix, dynamic = _key(path)
        if dynamic:
            seen = {m for route, methods in table.items() if route.startswith(prefix)
                    for m in methods}
        else:
            seen = table.get(prefix, set())
        if not seen or None in seen or method in seen:
            continue
        verb = "links to" if method == "GET" else "posts a form to"
        out.append(_problem("method-mismatch", where, "%s %s %s with %s, but handle-request only "
                            "handles %s %s." % (where, verb, path, method,
                                                " and ".join(sorted(seen)), prefix)))
    return out


# -- unrouted handlers, state tables, missing helpers ------------------------

def _unrouted_found(rows):
    if _find(rows, "handle-request") is None:
        return []
    out = []
    for row in rows:
        if row.kit or row.name == "handle-request":
            continue
        if not (row.name.startswith("handle-")
                or (row.name.startswith("render-") and row.name.endswith("-page"))):
            continue
        used = any(other.name != row.name and other.facts is not None
                   and row.name in other.facts["symbols"] for other in rows)
        if not used:
            out.append(_problem("unrouted-handler", row.name, "%s is never called: "
                                "handle-request does not route to it." % row.name))
    return out


def _unknown_table_found(rows):
    init = _find(rows, "initial-state")
    if init is None or init.form is None:
        return []
    created = _table_names(init.form)
    if not created:
        return []
    out = []
    for row in rows:
        if row.facts is None:
            continue
        for callee, args in row.facts["calls"]:
            if callee not in TABLE_CALLS or len(args) < 2 \
                    or not isinstance(args[1], s_expr.SString):
                continue
            table = str(args[1])
            if table in created:
                continue
            verb = "reads" if callee == "table-rows" else "writes"
            out.append(_problem("unknown-table", row.name, '%s %s the table "%s", which '
                                "initial-state does not create (it creates: %s)." % (
                                    row.name, verb, table, ", ".join(created))))
    return out


def _missing_found(rows):
    saved = {row.name for row in rows}
    out = []
    for row in rows:
        if row.kit or row.facts is None:
            continue
        for callee, _args in row.facts["calls"]:
            if callee in saved or callee in row.facts["local"] or callee in COMMON_LISP:
                continue
            if not (callee.startswith(HELPER_PREFIXES) or callee.endswith(HELPER_SUFFIXES)):
                continue
            out.append(_problem("missing-helper", row.name,
                                "%s calls %s, which is not saved." % (row.name, callee)))
    return out


# -- the check ---------------------------------------------------------------

def check(tools):
    """Every cross-function problem found in TOOLS: a list of {"kind", "where", "detail"} dicts."""
    rows = _rows(tools)
    found = _arity_found(rows, _arity_table(rows))
    handled, linked = _routes(rows)
    found += _dead_route_found(handled, linked)
    found += _method_found(handled, linked)
    if _find(rows, "handle-request") is not None:
        found += _unrouted_found(rows)
    found += _unknown_table_found(rows)
    found += _missing_found(rows)
    order = {kind: i for i, kind in enumerate(KINDS)}
    unique = {}
    for problem in found:
        unique.setdefault((problem["kind"], problem["where"], problem["detail"]), problem)
    return sorted(unique.values(),
                  key=lambda p: (order[p["kind"]], p["where"], p["detail"]))
