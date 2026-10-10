"""The qualifier: what has to be shown before a build may be called done.

"Done" used to mean that nothing was flagged: every function passed tests the
model wrote for it, and the app answered one request. A live build ended that
way while its main command printed "Invalid input", three of its commands
answered "Unknown command" and the functions it had just built were called by
nothing. Passing unit tests proves the parts; it says nothing about the whole.

The qualifier collects proofs about the whole, each from behaviour or from the
saved code, none from what the model says about its own work:

  answers       the entry point answers a request without an error
  wired         every planned function that was built is reached from the entry point
  commands      every command the app advertises is one it recognises (command-line apps)
  replay        each test of a command's handler still holds when the command is
                typed, i.e. when the call goes through the entry point
  state         the rows the tests of the functions assume are the rows the app really keeps
  integration   the integration tests written for the project from its goal pass: whole
                paths through the entry point, such as storing something and seeing it
  visitor       the app works when tried like a visitor (web apps, acceptance.py)
  fit           the saved functions fit together (interfaces.py)
  requirements  the user's own requirements are met (requirements.py)
  screens       the screenshots show what was asked for, when they were looked at

The verdict is "disproven" when a proof fails, "proven" when none fails and at
least one of replay, visitor or requirements shows the app doing its job, and
"unproven" otherwise: nothing went wrong, but nothing showed it working either.

Everything here is zero-token. No rule assumes what the app is for: the
commands, their handlers and the expected results are read from the app's own
dispatcher and from the tests saved with its functions.
"""
import re

import s_expr

ENTRY_POINTS = ("handle-request", "handle-command", "initial-state")
COMMAND_WORD = re.compile(r"[a-z][a-z0-9_-]{0,23}")
WORD_IN_TEXT = re.compile(r"[a-z][a-z0-9_-]{0,23}")
UNKNOWN_PROBE = "zzqx-no-such-command"
MAX_TRANSCRIPT = 12
MAX_REPLAYS = 40
BEHAVIOUR = ("replay", "visitor", "requirements", "integration")   # proofs that show the app doing its job
HELP_NOISE = frozenset(("commands", "command", "usage", "help", "available", "options", "and", "or", "the",
                        "for", "with", "list", "of", "a", "an", "to", "type", "use", "try", "see"))


# ------------------------------------------------------------------ reading and writing Lisp

def to_source(node):
    """Lisp source for a tree made by ``s_expr.parse``; None when it cannot be written back.

    The reader drops commas, so a backquoted form cannot be reproduced and is refused.
    """
    try:
        text = _source(node)
        return text if s_expr.parse(text) == node else None
    except (s_expr.SExprError, ValueError, TypeError, RecursionError):
        return None


def _source(node):
    if isinstance(node, s_expr.SString):
        return '"%s"' % str(node).replace("\\", "\\\\").replace('"', '\\"')
    if isinstance(node, bool):
        raise ValueError("no boolean in Lisp source")
    if isinstance(node, (int, float)):
        return repr(node)
    if isinstance(node, str):
        if node == "`":
            raise ValueError("backquote cannot be written back")
        return node
    if isinstance(node, list):
        if len(node) == 2 and node[0] == "quote" and not isinstance(node[0], s_expr.SString):
            return "'" + _source(node[1])
        parts, glue = [], False
        for item in node:
            text = _source(item)
            if glue:
                parts[-1] += text            # "#" belongs to what follows: #'car, #(1 2)
            else:
                parts.append(text)
            glue = item == "#" and not isinstance(item, s_expr.SString)
        return "(%s)" % " ".join(parts)
    raise ValueError("cannot write %r" % (node,))


def _parse(text):
    try:
        return s_expr.parse(text or "")
    except (s_expr.SExprError, ValueError, TypeError, RecursionError):
        return None


def _is_symbol(node, name=None):
    return isinstance(node, str) and not isinstance(node, s_expr.SString) and \
        (name is None or node.lower() == name)


def _defun(tool):
    """``(params, body_forms)`` of a saved function, or None."""
    tree = _parse(tool.get("definition"))
    if not (isinstance(tree, list) and len(tree) >= 3 and _is_symbol(tree[0], "defun")
            and isinstance(tree[2], list)):
        return None
    body = tree[3:]
    if body and isinstance(body[0], s_expr.SString) and len(body) > 1:
        body = body[1:]                      # the docstring is not code
    return [p.lower() for p in tree[2] if _is_symbol(p)], body


def _walk(node):
    """Every node of a tree in the order it is read."""
    yield node
    if isinstance(node, list):
        for item in node:
            for sub in _walk(item):
                yield sub


# ------------------------------------------------------------------ what calls what

BINDERS = ("let", "let*", "flet", "labels", "macrolet", "symbol-macrolet", "multiple-value-bind",
           "destructuring-bind", "do", "do*", "dolist", "dotimes", "lambda")


def _uses(node, names, found):
    """Add to FOUND the saved functions that the CODE in NODE calls or hands on.

    Quoted data is not code: a name inside ``'(a b)`` is a word in a list. The
    variable of a binding is not a call either, even when it is spelled like a
    saved function. ``'name``, ``#'name`` and ``(function name)`` are uses,
    because that is how a function is handed to FUNCALL or MAPCAR.
    """
    if not isinstance(node, list) or not node:
        return
    head = node[0]
    if _is_symbol(head, "quote") or _is_symbol(head, "function"):
        if len(node) == 2 and _is_symbol(node[1]) and node[1].lower() in names:
            found.add(node[1].lower())
        return                                   # anything else under QUOTE is data
    if _is_symbol(head) and head.lower() in BINDERS and len(node) >= 2 and isinstance(node[1], list):
        if head.lower() in ("let", "let*", "do", "do*", "symbol-macrolet"):
            for binding in node[1]:              # (var init ...): only the init forms are code
                if isinstance(binding, list):
                    for part in binding[1:]:
                        _uses(part, names, found)
        elif head.lower() in ("flet", "labels", "macrolet"):
            for binding in node[1]:              # (name (params) body...)
                if isinstance(binding, list):
                    for part in binding[2:]:
                        _uses(part, names, found)
        elif head.lower() in ("dolist", "dotimes"):
            for part in node[1][1:]:             # (var list-form [result])
                _uses(part, names, found)
        # lambda, multiple-value-bind and destructuring-bind: node[1] is a list of variables
        rest = node[3:] if head.lower() in ("multiple-value-bind", "destructuring-bind") else node[2:]
        if head.lower() in ("multiple-value-bind", "destructuring-bind") and len(node) > 2:
            _uses(node[2], names, found)
        for part in rest:
            _uses(part, names, found)
        return
    if _is_symbol(head) and head.lower() in names:
        found.add(head.lower())
    for item in node:
        _uses(item, names, found)


def call_graph(tools):
    """``{name: set of saved functions its code calls or hands on}``."""
    names = {t["name"].lower() for t in tools}
    graph = {}
    for tool in tools:
        found = set()
        made = _defun(tool)
        for form in (made[1] if made else []):
            _uses(form, names, found)
        found.discard(tool["name"].lower())
        graph[tool["name"].lower()] = found
    return graph


def reachable(tools, roots=ENTRY_POINTS):
    graph = call_graph(tools)
    seen, todo = set(), [r for r in roots if r in graph]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo.extend(graph.get(name, ()))
    return seen


def unwired(tools, planned_and_built):
    """Planned functions of this build that nothing reaches from an entry point, in order."""
    names = {t["name"].lower() for t in tools}
    if not names & {"handle-request", "handle-command"}:
        return []
    reached = reachable(tools)
    return [n for n in dict.fromkeys(x.lower() for x in planned_and_built)
            if n in names and n not in reached and n not in ENTRY_POINTS]


# ------------------------------------------------------------------ the dispatcher of a command-line app

def _first_call_after(form, marker, names):
    """The first call of a saved function that is read after MARKER inside FORM."""
    seen = False
    for node in _walk(form):
        if node is marker:
            seen = True
        elif seen and isinstance(node, list) and node and _is_symbol(node[0]) \
                and node[0].lower() in names:
            return node
    return None


def _handler_of(path, names):
    """The call that handles the command whose literal sits at the end of PATH (root first)."""
    literal = path[-1]
    for depth in range(len(path) - 2, max(len(path) - 8, -1), -1):
        form, inner = path[depth], path[depth + 1]
        if not isinstance(form, list) or not form:
            continue
        if all(isinstance(x, s_expr.SString) for x in form):
            continue                                     # a list of words: its clause is further out
        if _is_symbol(form[0], "if") and len(form) >= 3 and form[1] is inner:
            return _first_in(form[2], names)             # the THEN branch of this test
        if isinstance(form[0], (list, s_expr.SString)):          # a COND or CASE clause
            return _first_call_after(form, literal, names)
    return None


def _first_in(form, names):
    for node in _walk(form):
        if isinstance(node, list) and node and _is_symbol(node[0]) and node[0].lower() in names:
            return node
    return None


COMPARISONS = ("string=", "string-equal", "equal", "equalp", "eql", "eq")
MEMBERSHIPS = ("member", "find", "position", "assoc")


def _is_compared(path):
    """True when the string at the end of PATH is something the code compares a value with.

    A table name passed to a helper, or text that is printed, is a string in
    the dispatcher too; only strings in a comparison, in a list a value is
    looked up in, or at the head of a CASE-like clause are command words.
    """
    if len(path) < 2 or not isinstance(path[-2], list) or not path[-2]:
        return False
    parent = path[-2]
    if _is_symbol(parent[0]) and parent[0].lower() in COMPARISONS:
        return True
    grand = path[-3] if len(path) >= 3 else None
    if all(isinstance(x, s_expr.SString) for x in parent):         # a list of words
        if isinstance(grand, list) and len(grand) == 2 and _is_symbol(grand[0], "quote"):
            outer = path[-4] if len(path) >= 4 else None
            return isinstance(outer, list) and bool(outer) and _is_symbol(outer[0]) and \
                outer[0].lower() in MEMBERSHIPS
        return isinstance(grand, list) and bool(grand) and grand[0] is parent    # (("a" "b") ...) clause keys
    if parent[0] is path[-1] and isinstance(grand, list) and grand and _is_symbol(grand[0]) and \
            "case" in grand[0].lower():
        return True                                                    # ("calc" (cmd-calc ...)) in a CASE
    return False


def _paths_to_strings(node, path=()):
    here = path + (node,)
    if isinstance(node, s_expr.SString):
        yield here
    elif isinstance(node, list):
        for item in node:
            for found in _paths_to_strings(item, here):
                yield found


def _bindings(body):
    """Every ``(variable init)`` of the LET forms in BODY: ``{variable: init}`` (the first one wins)."""
    env = {}
    for node in _walk(body):
        if isinstance(node, list) and len(node) >= 2 and _is_symbol(node[0]) and \
                node[0].lower() in ("let", "let*") and isinstance(node[1], list):
            for binding in node[1]:
                if isinstance(binding, list) and len(binding) == 2 and _is_symbol(binding[0]):
                    env.setdefault(binding[0].lower(), binding[1])
    return env


def _shape(form, args_var, env, depth=0):
    """How FORM relates to the typed words: "all" of them, the "rest" after the first, or None.

    A dispatcher rarely writes ``(rest args)`` in the call itself: it binds the
    words to a variable first, often after tidying them (``(mapcar ...)``) or
    guarding against an empty line (``(if (null words) nil (rest words))``).
    """
    if depth > 8:
        return None
    if _is_symbol(form):
        if form.lower() == args_var:
            return "all"
        if form.lower() in env:
            return _shape(env[form.lower()], args_var, dict(env, **{form.lower(): None}), depth + 1)
        return None
    if not isinstance(form, list) or not form or not _is_symbol(form[0]):
        return None
    head = form[0].lower()
    if head in ("rest", "cdr") and len(form) == 2:
        return "rest" if _shape(form[1], args_var, env, depth + 1) == "all" else None
    if head in ("if", "when", "and", "or", "unless") and len(form) >= 3:
        shapes = {_shape(x, args_var, env, depth + 1) for x in form[2:] if not _is_symbol(x, "nil")}
        shapes.discard(None)
        return shapes.pop() if len(shapes) == 1 else None
    if head in ("mapcar", "remove", "remove-if", "remove-if-not", "copy-list") and len(form) >= 2:
        return _shape(form[-1], args_var, env, depth + 1)       # the same words, tidied
    return None


def dispatch_map(tools):
    """``{command word: {"handler": name or None, "shape": "rest"|"all"|None}}`` read from the app.

    The words are the short lower-case strings its dispatcher compares the first
    typed word with; the handler is the saved function called for that word.
    ``shape`` says how the dispatcher passes the typed words on: without the
    command word (``(rest args)``), with it (``args``), or in a way this reader
    does not follow (None).
    """
    by_name = {t["name"].lower(): t for t in tools}
    entry = by_name.get("handle-command")
    made = _defun(entry) if entry else None
    if not made:
        return {}
    names = set(by_name) - {"handle-command"}
    sources = [made]
    graph = call_graph(tools)
    for callee in sorted(graph.get("handle-command", ())):      # a dispatcher one call away
        sub = _defun(by_name[callee])
        if sub:
            sources.append(sub)
    out = {}
    for index, (params, body) in enumerate(sources):
        if index and out:
            break                            # the entry point dispatches itself: look no further
        args_var = params[0] if params else "args"
        env = _bindings(body)
        for path in _paths_to_strings(body):
            word = str(path[-1])
            if not COMMAND_WORD.fullmatch(word) or word in out or not _is_compared(path):
                continue
            call = _handler_of(list(path), names)
            shape = _shape(call[1], args_var, env) if call is not None and len(call) >= 2 else None
            out[word] = {"handler": call[0].lower() if call is not None else None, "shape": shape}
    return out


PLACEHOLDER = re.compile(r"<[^<>]{0,60}>|\[[^\[\]]{0,60}\]")
LIST_JOINERS = ("and", "or")


def _list_items(segment):
    """The single words of a comma-separated list of commands, or None when it is prose."""
    items = []
    for part in segment.split(","):
        words = [w for w in PLACEHOLDER.sub(" ", part).split() if w not in LIST_JOINERS]
        words = [w.strip(".;:()'\"`") for w in words]
        if len(words) != 1 or not COMMAND_WORD.fullmatch(words[0]):
            return None
        items.append(words[0])
    return items


def advertised(help_text, known=()):
    """The command words a help text offers, in the order it names them.

    Two layouts are read. A list: ``Commands: calc, save, list`` offers every
    word in it, whether the dispatcher knows the word or not (an offered command
    the app does not know is exactly what the proof looks for). An entry per
    line: ``calc <amount> <rate>  - what it does`` offers its first word.
    Placeholders in ``<...>`` or ``[...]`` are arguments, never commands, and a
    sentence with commas in it is not a list.
    """
    known = set(known)
    out = []
    for line in (help_text or "").replace("~%", "\n").splitlines():
        low = line.strip().lower()
        if not low:
            continue
        label, colon, after = low.partition(":")
        if colon and after.strip():
            items = _list_items(after)
            if items and (len(items) >= 2 or "command" in label or items[0] in known):
                out += items
                continue
        elif "," in low:
            items = _list_items(low)
            if items and len(items) >= 2 and any(w in known for w in items):
                out += items
                continue
        head = PLACEHOLDER.sub(" ", low).split()
        if not head or not COMMAND_WORD.fullmatch(head[0]):
            continue
        described = " - " in low or "<" in low or "  " in line.strip() or (colon and not after.strip() == "")
        if head[0] in known or (described and head[0] not in HELP_NOISE and not colon):
            out.append(head[0])
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ replaying handler tests through the entry point

def _typed_words(node):
    """The words of a quoted list of strings, or None."""
    if isinstance(node, list) and len(node) == 2 and _is_symbol(node[0], "quote") and \
            isinstance(node[1], list) and all(isinstance(x, s_expr.SString) for x in node[1]):
        return [str(x) for x in node[1]]
    return None


def _rewrite(node, handler, word, shape):
    """NODE with every ``(handler args state now)`` turned into the typed command; count of changes.

    The typed command is the command word followed by the words the test gives
    the handler. Only when the dispatcher hands the handler ALL the typed words
    and the test's words already start with the command word are they typed as
    they stand. A handler tested without the command word but called with it is
    exactly the mismatch this replay is there to find.
    """
    if not isinstance(node, list):
        return node, 0
    if node and _is_symbol(node[0], handler) and len(node) == 4:
        words = _typed_words(node[1])
        whole = shape == "all" and words is not None and words[:1] == [word]
        args = node[1] if whole else ["cons", s_expr.SString(word), node[1]]
        return ["handle-command", args, node[2], node[3]], 1
    out, changed = [], 0
    for item in node:
        new, n = _rewrite(item, handler, word, shape)
        out.append(new)
        changed += n
    return out, changed


def _says_something(tree, handler, expect):
    """True when a test pins down what the handler returns.

    An expected value other than T does. A test that expects T does only when it
    compares the result with something: it has a string or a number in it
    outside the handler's own arguments. ``(cmd-calc '("1") nil 0) => T`` and
    ``(listp (cmd-calc ...)) => T`` hold for any result at all.
    """
    if (expect or "").strip().upper() not in ("T", "NIL"):
        return True

    def literal(node):
        if isinstance(node, list):
            if node and _is_symbol(node[0], handler):
                return False                     # the call under test: its arguments are inputs
            return any(literal(x) for x in node)
        return isinstance(node, s_expr.SString) or (isinstance(node, (int, float)) and not isinstance(node, bool))
    return literal(tree)


def replay_checks(tools, dispatch=None):
    """Checks that re-run the tests of each command's handler through ``handle-command``.

    Each is ``{"command", "handler", "call", "expect", "original", "typed"}``. A
    test is used only when it calls the handler the way the dispatcher does, so
    that typing the command must give the very result the test expects.
    """
    dispatch = dispatch_map(tools) if dispatch is None else dispatch
    by_name = {t["name"].lower(): t for t in tools}
    out = []
    for word, info in sorted(dispatch.items()):
        tool = by_name.get(info.get("handler") or "")
        if not tool or info.get("shape") not in ("rest", "all"):
            continue
        for test in tool.get("tests") or []:
            call, expect = test.get("call"), test.get("expect")
            tree = _parse(call)
            if tree is None or not isinstance(expect, str) or not expect.strip():
                continue
            new, changed = _rewrite(tree, info["handler"], word, info["shape"])
            source = to_source(new) if changed else None
            if not source:
                continue
            typed = None
            for node in _walk(tree):
                if isinstance(node, list) and node and _is_symbol(node[0], info["handler"]) and len(node) == 4:
                    words = _typed_words(node[1])
                    if words is not None:
                        typed = words if info["shape"] == "all" and words[:1] == [word] else [word] + words
                    break
            out.append({"command": word, "handler": info["handler"], "call": source, "shape": info["shape"],
                        "expect": expect.strip(), "original": call, "typed": typed,
                        "informative": _says_something(tree, info["handler"], expect)})
            if len(out) >= MAX_REPLAYS:
                return out
    return out


def run_replays(checks, prelude, checker, evaluate):
    """Evaluate CHECKS in one Lisp run: ``[True | False | None]`` with a detail for each.

    CHECKER is the source of the harness's comparison function (``gg-check``),
    EVALUATE runs Lisp source and returns the worker's envelope. None means the
    run could not be read, so nothing is concluded from it.
    """
    if not checks:
        return []
    forms = ["(handler-case (gg-check %s '%s) (error (e) (list :raised (princ-to-string e))))"
             % (c["call"], c["expect"]) for c in checks]
    try:
        env = evaluate("%s\n%s\n(let ((*print-pretty* nil)) (list %s))" % (checker, prelude, "\n".join(forms)))
    except Exception:  # noqa: BLE001 - a run that fails proves nothing either way
        env = None
    tree = _parse(env.get("return_value")) if isinstance(env, dict) and env.get("ok") else None
    if not isinstance(tree, list) or len(tree) != len(checks):
        return [(None, "the replay could not be run")] * len(checks)
    out = []
    for item in tree:
        if _is_symbol(item, "t"):
            out.append((True, ""))
        elif isinstance(item, list) and item and _is_symbol(item[0], ":raised"):
            out.append((False, "it raised: %s" % " ".join(str(x) for x in item[1:])[:160]))
        elif isinstance(item, list) and item and _is_symbol(item[0], ":got"):
            got = to_source(item[1]) if len(item) > 1 else ""
            out.append((False, "got %s" % ((got or "something else")[:200])))
        else:
            out.append((False, "it does not hold"))
    return out


# ------------------------------------------------------------------ one layout of the data for all functions

def row_shape(row):
    """What a row of a table looks like: one letter per column (s text, n number, l list, y symbol)."""
    def tag(x):
        if isinstance(x, s_expr.SString):
            return "s"
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            return "n"
        return "l" if isinstance(x, list) else "y"
    return "".join(tag(x) for x in row) if isinstance(row, list) else "?"


def _tables(node):
    """Every ``("name" (row row ...))`` pair found anywhere in NODE, as ``(name, rows)``."""
    for sub in _walk(node):
        if isinstance(sub, list) and len(sub) == 2 and isinstance(sub[0], s_expr.SString) and \
                isinstance(sub[1], list) and sub[1] and all(isinstance(r, list) and r for r in sub[1]):
            yield str(sub[0]), sub[1]


def describe_shape(shape):
    words = {"s": "text", "n": "number", "l": "list", "y": "symbol"}
    return "%d column%s (%s)" % (len(shape), "" if len(shape) == 1 else "s",
                                 ", ".join(words.get(c, "?") for c in shape))


def fixture_shapes(tools):
    """The rows the saved tests use: ``{table: {shape: {"example": source, "functions": [names]}}}``.

    Both the data a test passes in and the state it expects back are read.
    """
    out = {}
    for tool in tools:
        if tool.get("kit"):
            continue
        for test in tool.get("tests") or []:
            for text in (test.get("call"), test.get("expect")):
                tree = _parse(text)
                if tree is None:
                    continue
                for table, rows in _tables(tree):
                    for row in rows:
                        entry = out.setdefault(table, {}).setdefault(
                            row_shape(row), {"example": to_source(row) or "", "functions": []})
                        if tool["name"] not in entry["functions"]:
                            entry["functions"].append(tool["name"])
    return out


def real_shapes(state_src):
    """The rows the app itself keeps: ``{table: {shape: example source}}`` (tables without rows: {})."""
    tree = _parse(state_src)
    out = {}
    if not isinstance(tree, list):
        return out
    for item in tree:
        if isinstance(item, list) and len(item) == 2 and isinstance(item[0], s_expr.SString) and \
                isinstance(item[1], list):
            shapes = out.setdefault(str(item[0]), {})
            for row in item[1]:
                if isinstance(row, list) and row:
                    shapes.setdefault(row_shape(row), to_source(row) or "")
    return out


def merge_shapes(into, more):
    """Add the tables and row shapes of MORE to INTO (both as ``real_shapes`` returns them)."""
    for table, shapes in (more or {}).items():
        mine = into.setdefault(table, {})
        for shape, example in shapes.items():
            mine.setdefault(shape, example)
    return into


def state_facts(real):
    """One sentence per table of the app's real data, for a prompt; empty when it keeps none.

    REAL is what ``real_shapes`` returns, or the Lisp source of a state.
    """
    real = real_shapes(real) if isinstance(real, str) else (real or {})
    said = []
    for table, shapes in sorted(real.items()):
        if shapes:
            said.append('"%s" rows look like %s' % (table, " or ".join(list(shapes.values())[:2])))
        else:
            said.append('"%s" has no rows yet' % table)
    return "; ".join(said)


def state_proof(tools, real, writers=()):
    """Do the functions agree on what a row of each table looks like? ``(proof or None, functions)``.

    Every function was tested alone, on data its own test made up. One wrote
    loans as three texts, the others looked loans up by an id in front of
    four, and each passed (live project mortgage-calc: a saved loan could
    never be found again). The rows the app itself wrote while its commands
    were typed are the truth; where it wrote none, the tests have at least to
    agree with each other.
    """
    fixtures = fixture_shapes(tools)
    real = real_shapes(real) if isinstance(real, str) else (real or {})
    label = "The functions agree on what the stored data looks like"
    if not fixtures:
        return None, []
    said, drifted, compared = [], [], False
    for table, shapes in sorted(fixtures.items()):
        actual = real.get(table) or {}
        if actual:
            compared = True
            off = {shape: info for shape, info in shapes.items() if shape not in actual}
            if off:
                names = sorted({n for info in off.values() for n in info["functions"]})
                shape, info = next(iter(off.items()))
                truth_shape, truth = next(iter(actual.items()))
                said.append('the tests of %s use "%s" rows of %s such as %s, but the app itself stores '
                            'rows of %s such as %s%s' % (
                                ", ".join(names[:6]), table, describe_shape(shape), info["example"],
                                describe_shape(truth_shape), truth,
                                " (written by %s)" % ", ".join(writers[:3]) if writers else ""))
                drifted += names
        elif real and table not in real and len(real) >= 1:
            names = sorted({n for info in shapes.values() for n in info["functions"]})
            said.append('the tests of %s use a table "%s", which the app does not keep (it keeps %s)'
                        % (", ".join(names[:6]), table, ", ".join('"%s"' % t for t in sorted(real))))
            drifted += names
        elif len(shapes) > 1:
            ranked = sorted(shapes.items(), key=lambda kv: -len(kv[1]["functions"]))
            (shape_a, a), (shape_b, b) = ranked[0], ranked[1]
            said.append('the functions disagree about "%s" rows: %s are tested with %s such as %s, %s with '
                        '%s such as %s' % (table, ", ".join(a["functions"][:4]), describe_shape(shape_a),
                                           a["example"], ", ".join(b["functions"][:4]),
                                           describe_shape(shape_b), b["example"]))
            tie = len(a["functions"]) == len(b["functions"])
            drifted += [n for _, info in ranked[0 if tie else 1:] for n in info["functions"]]
    if said:
        return _proof("state", label, False, "; ".join(said[:3]) +
                      ". The functions that read a table and the one that writes it need ONE row layout."), \
            sorted(set(drifted))
    if compared:
        return _proof("state", label, True, "the rows in the tests match the rows the app stores"), []
    return _proof("state", label, None, "the app stored no rows while it was tried, so there was nothing "
                  "to compare the tests with"), []


# ------------------------------------------------------------------ quick checks as the functions connect

MAX_CHAINS = 40                 # pairs tried for one function, in one Lisp run
PRINTED_NIL = re.compile(r"(?<![A-Za-z0-9-])NIL(?![A-Za-z0-9-])")


def _handler_kind(tool):
    """``"command"`` for a function of (args state now), ``"request"`` for (request state), else None."""
    made = _defun(tool)
    if not made:
        return None
    params = made[0]
    if len(params) == 3 and params[-2:] == ["state", "now"]:
        return "command"
    return "request" if params == ["request", "state"] else None


def _test_call(tool, kind):
    """The first direct call of a handler in its own tests, as a parsed form, or None."""
    want, name = (4 if kind == "command" else 3), tool["name"].lower()
    for test in tool.get("tests") or []:
        tree = _parse(test.get("call"))
        for node in (_walk(tree) if tree is not None else ()):
            if isinstance(node, list) and len(node) == want and _is_symbol(node[0], name) and \
                    all(to_source(x) for x in node[1:]):
                return node
    return None


def chain_checks(tools, name):
    """Connection tests the harness makes for the handler NAME: ``[{"writer", "reader", "form"}]``.

    Every handler was tested on a state its own test made up. Here one handler
    is run the way its own test runs it, and the state it REALLY returns is
    handed to another handler in place of the made-up one. That needs no entry
    point, so it can be done the moment the second of two handlers is built.
    """
    name = (name or "").lower()
    handlers = []
    for tool in tools:
        low = tool["name"].lower()
        if tool.get("kit") or low in ENTRY_POINTS:
            continue
        kind = _handler_kind(tool)
        call = _test_call(tool, kind) if kind else None
        if call is not None:
            handlers.append((low, kind, call))
    mine = next((h for h in handlers if h[0] == name), None)
    if mine is None:
        return []
    others = [h for h in handlers if h is not mine and h[1] == mine[1]]
    pairs = [(mine, mine)] + [(w, mine) for w in others] + [(mine, r) for r in others]
    out = []
    for writer, reader in pairs[:MAX_CHAINS]:
        call, key = reader[2], (":output" if reader[1] == "command" else ":body")
        again = "(%s %s gg-st%s)" % (reader[0], to_source(call[1]),
                                     " " + to_source(call[3]) if len(call) == 4 else "")
        out.append({"writer": writer[0], "reader": reader[0], "form": (
            "(let* ((gg-in %s) (gg-w (handler-case %s (error () nil))) "
            "(gg-st (handler-case (if (member :state gg-w) (getf gg-w :state) gg-in) (error () gg-in)))) "
            "(if (equal gg-st gg-in) (list :same) "
            "(handler-case (let ((gg-r %s)) (list :ran (princ-to-string (or (ignore-errors (getf gg-r %s)) \"\")))) "
            "(error (e) (list :raised (princ-to-string e))))))"
            % (to_source(writer[2][2]), to_source(writer[2]), again, key))})
    return out


def run_chains(checks, prelude, evaluate):
    """Run CHECKS in one Lisp run: ``[(True | False | None, detail)]``.

    None: the first handler stored nothing (or failed by itself), so there was
    nothing to hand on. False: the second one raises an error on the data the
    first really stores, or prints NIL to the user.
    """
    if not checks:
        return []
    try:
        env = evaluate("%s\n(let ((*print-pretty* nil) (*print-length* nil) (*print-level* nil)) (list %s))"
                       % (prelude, "\n".join(c["form"] for c in checks)))
    except Exception:  # noqa: BLE001 - a run that fails proves nothing either way
        env = None
    tree = _parse(env.get("return_value")) if isinstance(env, dict) and env.get("ok") else None
    if not isinstance(tree, list) or len(tree) != len(checks):
        return [(None, "the check could not be run")] * len(checks)
    out = []
    for check, item in zip(checks, tree):
        head = item[0].lower() if isinstance(item, list) and item and _is_symbol(item[0]) else ""
        text = " ".join(str(x) for x in item[1:]) if isinstance(item, list) else ""
        whose = "its own data" if check["writer"] == check["reader"] else \
            "the data %s really stores" % check["writer"]
        if head == ":raised":
            out.append((False, "%s raises an error when given %s (%s)" % (check["reader"], whose, _clip(text, 120))))
        elif head == ":ran" and PRINTED_NIL.search(text):
            out.append((False, "%s prints NIL when given %s: it answered \"%s\"" % (
                check["reader"], whose, _clip(text, 120))))
        else:
            out.append((True if head == ":ran" else None, ""))
    return out


def layout_faults(tools, name, real=None):
    """Where the tests of NAME assume other rows than the app stores or the other functions use."""
    name, real, out = (name or "").lower(), real or {}, []
    for table, shapes in sorted(fixture_shapes(tools).items()):
        mine = {s: i for s, i in shapes.items() if name in (f.lower() for f in i["functions"])}
        if not mine:
            continue
        actual = real.get(table) or {}
        if actual:
            off = [s for s in mine if s not in actual]
            if off:
                truth_shape, truth = next(iter(actual.items()))
                out.append('its tests use "%s" rows of %s such as %s, but the app really stores rows of %s '
                           'such as %s' % (table, describe_shape(off[0]), mine[off[0]]["example"],
                                           describe_shape(truth_shape), truth))
            continue
        others = {s: [f for f in i["functions"] if f.lower() != name] for s, i in shapes.items()}
        others = {s: who for s, who in others.items() if who}
        off = [s for s in mine if s not in others]
        if others and off:
            shape, who = max(others.items(), key=lambda kv: len(kv[1]))
            out.append('its tests use "%s" rows of %s such as %s, but %s %s tested with rows of %s such as %s'
                       % (table, describe_shape(off[0]), mine[off[0]]["example"], ", ".join(who[:4]),
                          "is" if len(who) == 1 else "are", describe_shape(shape), shapes[shape]["example"]))
    return out


def layout_note(tools):
    """The row layouts the tests of the saved functions use, in a sentence; empty when they use none."""
    said = []
    for table, shapes in sorted(fixture_shapes(tools).items()):
        shape, info = max(shapes.items(), key=lambda kv: len(kv[1]["functions"]))
        said.append('"%s" rows such as %s (%s)' % (table, info["example"], ", ".join(info["functions"][:3])))
    return "; ".join(said[:6])


def typed_commands(lines):
    """The first word of every command typed in LINES of the requirements language."""
    return {m.split()[0].lower() for line in lines for m in re.findall(r'run\s+"([^"]+)"', line) if m.split()}


DIRECTIVE = re.compile(r"~[%&]|~\d{0,3}[aAsSdD]")


# ------------------------------------------------------------------ the proofs

def _proof(pid, label, ok, detail):
    return {"id": pid, "label": label, "ok": ok, "detail": " ".join(str(detail).split())[:420]}


def _clip(text, n=160):
    text = " | ".join(x.strip() for x in str(text or "").replace("~%", "\n").splitlines() if x.strip())
    return text if len(text) <= n else text[:n - 1] + "…"


MAX_TYPED = 40                  # commands typed in one qualification, shown or not
REPLAY_COVERAGE = 1.0           # every command with a handler needs a telling test that holds when typed


def command_proofs(tools, run_command, evaluate, prelude, checker, read_state=None):
    """The proofs only a command-line app can give: ``(proofs, transcript, counts)``.

    RUN_COMMAND takes a list of words and returns ``{"ok", "output", "error"}``
    on a throwaway state. COUNTS (``replay_failed``, ``replay_total``,
    ``commands_bad``) let a caller tell whether a round of fixes made things worse.
    """
    dispatch = dispatch_map(tools)
    checks = replay_checks(tools, dispatch)
    results = run_replays(checks, prelude, checker, evaluate)

    def state_now():
        try:
            return read_state() if read_state else None
        except Exception:  # noqa: BLE001 - the data cannot be read: nothing is concluded from it
            return None

    writers, rows_seen = [], {}
    merge_shapes(rows_seen, real_shapes(state_now() or ""))

    def typed(words):
        before = state_now()
        try:
            out = run_command(list(words))
            if not isinstance(out, dict):
                out = {"ok": False, "output": "", "error": "the command gave no result"}
        except Exception as exc:  # noqa: BLE001 - a broken command is evidence, not a crash
            out = {"ok": False, "output": "", "error": "%s: %s" % (type(exc).__name__, exc)}
        raw = str((out.get("output") if out.get("ok") else out.get("error")) or "")
        after = state_now() if read_state else None
        if after is not None:
            merge_shapes(rows_seen, real_shapes(after))      # also rows a later command removes again
            if before is not None and after != before and words[0] not in writers:
                writers.append(words[0])         # this command changed the stored data
        return {"typed": " ".join(words), "ok": bool(out.get("ok")), "printed": _clip(raw), "raw": raw}

    unknown = typed([UNKNOWN_PROBE])
    helped = typed(["help"])
    offered = advertised(helped["raw"], dispatch) if helped["ok"] else []
    examples = {}
    for check in checks:
        if check["typed"] and check["command"] not in examples:
            examples[check["command"]] = check["typed"]
    rows, unrecognised, crashed, untyped = [helped], [], [], []
    for word in list(dict.fromkeys(list(dispatch) + offered)):
        if word == "help":
            continue
        if len(rows) >= MAX_TYPED:
            untyped.append(word)
            continue
        shown = typed(examples.get(word) or [word])      # typed once: a command may change the data
        rows.append(shown)
        # "Unknown command: compare" names the word typed: compare the answers without it
        if unknown["ok"] and shown["ok"] and \
                shown["raw"].replace(word, "?") == unknown["raw"].replace(UNKNOWN_PROBE, "?"):
            unrecognised.append(word)
        elif not shown["ok"]:
            # with no example known the word is typed alone: a command that answers a missing
            # argument with a Lisp error instead of a usage line is a defect a user would meet
            crashed.append("%s%s (%s)" % (shown["typed"], "" if word in examples else " typed alone",
                                          shown["printed"][:80]))
    literal = [row["typed"] for row in rows if row["ok"] and DIRECTIVE.search(row["raw"])]
    label = "Every command the app offers is one it recognises"
    said = []
    if literal:
        said.append("these commands print FORMAT directives such as ~%% as plain characters instead of what "
                    "they stand for: %s" % ", ".join(literal[:5]))
    if not helped["ok"]:
        said.append("help ends in an error (%s)" % helped["printed"][:80])
    if unrecognised:
        said.append("the app offers %s, but typing %s gets the same answer as an unknown command"
                    % (", ".join(unrecognised), "it" if len(unrecognised) == 1 else "them"))
    if crashed:
        said.append("these commands end in an error: " + "; ".join(crashed[:4]))
    notes = []
    if untyped:
        notes.append("%d more commands were not typed" % len(untyped))
    proofs = []
    if not dispatch and not offered:
        proofs.append(_proof("commands", label, None if helped["ok"] else False,
                             "; ".join(said) or "no commands could be read from the app"))
    elif said:
        proofs.append(_proof("commands", label, False, "; ".join(said + notes)))
    elif untyped:
        proofs.append(_proof("commands", label, None, "%d commands typed, all recognised; %s"
                             % (len(rows) - 1, "; ".join(notes))))
    else:
        proofs.append(_proof("commands", label, True, "; ".join(
            ["%d commands typed, all recognised" % (len(rows) - 1)] + notes)))
    held = [c for c, (ok, _) in zip(checks, results) if ok is True]
    broke = [(c, why) for c, (ok, why) in zip(checks, results) if ok is False]
    with_handler = sorted({w for w, info in dispatch.items() if info.get("handler")})
    covered = sorted({c["command"] for c in held if c.get("informative")})
    label = "What each command's own tests expect also happens when the command is typed"
    if broke:
        first = "; ".join("typing '%s': the test of %s expects %s, %s" % (
            " ".join(c["typed"]) if c["typed"] else c["command"], c["handler"], _clip(c["expect"], 90), why)
            for c, why in broke[:3])
        passes_word = sorted({c["handler"] for c, _ in broke if c.get("shape") == "all"})
        hint = (" handle-command passes the command word on to %s as the first of its words, and the tests "
                "of %s call it without that word: one of the two has to change."
                % (", ".join(passes_word[:4]), "that function" if len(passes_word) == 1 else "those functions")
                if passes_word else "")
        proofs.append(_proof("replay", label, False, "%d of %d tests of command handlers fail when the "
                             "command is typed: %s.%s" % (len(broke), len(checks), first, hint)))
    elif not checks:
        proofs.append(_proof("replay", label, None, "no test of a command handler could be replayed "
                             "(the dispatcher does not hand the typed words on in a way that can be followed)"))
    elif len(covered) >= max(1, REPLAY_COVERAGE * len(with_handler)):
        proofs.append(_proof("replay", label, True, "%d tests hold when typed; %d of %d commands have a test "
                             "that checks what is printed" % (len(held), len(covered), len(with_handler))))
    else:
        bare = sorted(set(with_handler) - set(covered))
        proofs.append(_proof("replay", label, None, "%d tests hold when typed, but only %d of %d commands have "
                             "a test that checks what is printed; the tests of %s would pass whatever the "
                             "command printed" % (len(held), len(covered), len(with_handler), ", ".join(bare[:6]))))
    by_word = {w: info.get("handler") for w, info in dispatch.items()}
    blamed = {c["handler"] for c, _ in broke} | {by_word.get(w) for w in unrecognised} | \
        {by_word.get(row["typed"].split()[0]) for row in rows if not row["ok"] or row["typed"] in literal}
    counts = {"replay_failed": len(broke), "replay_total": len(checks),
              "commands_bad": len(unrecognised) + len(crashed) + len(literal) + (0 if helped["ok"] else 1),
              "rows": rows_seen if read_state else None, "writers": list(writers),
              "blamed": sorted(n for n in blamed if n),
              # commands whose handler has no test that checks what it prints
              "uncovered": sorted(set(with_handler) - set(covered)) if checks and not broke else []}
    transcript = [{k: v for k, v in row.items() if k != "raw"} for row in rows[:MAX_TRANSCRIPT]]
    return proofs, transcript, counts


def qualify(tools, planned_built=(), smoke_ok=None, accept=None, iface=(), reqs=None, screens=None,
            command=None, state=None, integ=None):
    """The proofs and the verdict for a finished build.

    COMMAND, for a command-line app, is ``(proofs, transcript)`` from
    ``command_proofs``. ACCEPT is the summary of the visitor checks, IFACE the
    list of interface problems, REQS the summary of the user's requirements,
    SCREENS the screenshot verdict (True, False or None).
    """
    names = {t["name"].lower() for t in tools}
    is_app = bool(names & {"handle-request", "handle-command"})
    proofs = []
    if smoke_ok is not None:
        proofs.append(_proof("answers", "The app answers a request", bool(smoke_ok),
                             "the entry point answered" if smoke_ok else "the entry point did not answer"))
    loose = unwired(tools, planned_built)
    if is_app and planned_built:
        proofs.append(_proof(
            "wired", "Every planned function that was built is used by the app", not loose,
            "nothing reaches %s from the entry point: built, tested, and never called" % ", ".join(loose)
            if loose else "all %d are reached from the entry point" % len(set(planned_built))))
    transcript, counts = [], {}
    if command is not None:
        proofs += command[0]
        transcript = command[1]
        counts = dict(command[2]) if len(command) > 2 else {}
    real = counts.pop("rows", None)
    if state is not None:
        real = merge_shapes(dict(real or {}), real_shapes(state))
    drifted = []
    if real is not None:
        reach = reachable(tools) if is_app else None
        live = tools if reach is None else [t for t in tools if t["name"].lower() in reach]
        proof, drifted = state_proof(live, real, counts.get("writers") or ())
        if proof:
            proofs.append(proof)
    counts["state_facts"] = state_facts(real) if real is not None else ""
    if accept is not None:
        failed, passed = int(accept.get("failed") or 0), int(accept.get("passed") or 0)
        proofs.append(_proof(
            "visitor", "The app works when tried like a visitor", False if failed else (True if passed >= 2 else None),
            "%d checks failed: %s" % (failed, "; ".join(accept.get("failed_labels") or [])[:300]) if failed
            else "%d checks passed" % passed if passed >= 2 else "too little could be tried to say"))
    real = [p for p in iface if p.get("kind") != "unrouted-handler"]
    if is_app:
        proofs.append(_proof("fit", "The saved functions fit together", not real,
                             "; ".join(p.get("detail", "") for p in real[:3]) if real else "no mismatch found"))
    if reqs is not None and reqs.get("total"):
        unmet, unchecked = int(reqs.get("unmet") or 0), int(reqs.get("unchecked") or 0)
        proofs.append(_proof(
            "requirements", "Your own requirements are met", False if unmet or unchecked else True,
            "%d of %d not met%s" % (unmet, reqs["total"], ", %d could not be checked" % unchecked if unchecked else "")
            if unmet or unchecked else "all %d met, checked as written" % reqs["total"]))
    if integ is not None and integ.get("total"):
        unmet, unchecked, met = (int(integ.get(k) or 0) for k in ("unmet", "unchecked", "met"))
        proofs.append(_proof(
            "integration", "The app passes its integration tests", False if unmet else (True if met else None),
            "%d of %d fail: %s" % (unmet, integ["total"], "; ".join(integ.get("unmet_texts") or [])[:300]) if unmet
            else "all %d pass%s" % (met, ", %d could not be run" % unchecked if unchecked else "") if met
            else "none could be run"))
    if screens is not None:
        proofs.append(_proof("screens", "The screenshots show what was asked for", bool(screens),
                             "they match" if screens else "they still show problems"))
    failed = [p for p in proofs if p["ok"] is False]
    shown = [p for p in proofs if p["id"] in BEHAVIOUR and p["ok"] is True]
    verdict = "disproven" if failed else "proven" if shown or not is_app else "unproven"
    if failed:
        basis = "Not done: %d of %d proofs failed (%s)." % (
            len(failed), len(proofs), "; ".join(p["label"] for p in failed))
    elif verdict == "proven":
        basis = ("Proven by: " + "; ".join(p["label"] for p in shown) + "."
                 if shown else "Its tests passed and the call returned; there is no app to try.")
    else:
        basis = ("Nothing failed, but nothing showed the app doing its job either: no test that checks "
                 "what a command prints held when typed, no visitor check applied, and you wrote no "
                 "requirements.")
    by_word = {w: info.get("handler") for w, info in dispatch_map(tools).items()}
    implicated = set(loose) | set(drifted) | set(counts.get("blamed") or ()) | \
        {by_word.get(w) for w in counts.get("uncovered") or ()}
    if implicated - {None} and {p["id"] for p in failed} & {"wired", "commands", "replay"}:
        # the entry point may be what is wrong: it hands the commands on. Rows that differ and
        # integration tests that fail behind a command are the handlers' business alone.
        implicated |= names & set(ENTRY_POINTS)
    return {"verdict": verdict, "proofs": proofs, "transcript": transcript[:MAX_TRANSCRIPT],
            "failed": [p["id"] for p in failed], "basis": basis, "counts": counts,
            "implicated": sorted(n for n in implicated if n), "real_rows": real or {}}


def failures(qualification, ids=("wired", "commands", "replay", "state")):
    """Plain sentences for the failed proofs a round of fixes can act on."""
    return ["%s: %s" % (p["label"], p["detail"])
            for p in (qualification or {}).get("proofs", []) if p["ok"] is False and p["id"] in ids]
