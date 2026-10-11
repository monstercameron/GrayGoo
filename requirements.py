"""Requirements the user states, checked against observable behaviour only.

A requirements file is written by a person, one requirement per line. The
harness never rewrites these lines; it only runs them against the app and
reports what it saw. Keywords are case-insensitive. Quoted text is written in
double quotes, and a backslash escapes a quote or a backslash inside quotes.

Web lines (checked through an app's handle(method, target, headers, body)):

    GET /path shows "text"
    GET /path does not show "text"
    GET /path answers 200
    GET /path redirects to /other
    POST /path with field=value, other="two words" shows "text"
    POST /path with a=b answers 303
    POST /path with a=b then GET /other shows "text"
    POST /path with a=b then GET /other does not show "text"

A POST sent with "shows" or "then" has its redirects followed before the
page is checked. "answers" and "redirects to" look at the response to that
one request, with no redirect followed.

Command lines (checked through the command-line app's run_words):

    run "add Buy milk" prints "Buy milk"
    run "list" does not print "x"
    run "add a" then run "list" prints "a"
    run "add a" then run "list" works        (the commands run without an error)

Call lines (checked through a saved Lisp project's functions). A call line
evaluates one parenthesised Lisp form and checks what it returns:

    call (battery-life '(:type :aa :count 2 :power-mw 200)) gives "Battery life: 36.00 hours"
    call (square 12) gives 144
    call (battery-life '(:type :aa)) shows "hours"
    call (battery-life '(:type :aa)) does not show "error"
    call (square 12) works                  (the call completes without a Lisp error)

"gives" compares the printed value (as PRIN1 shows it) with printed_matches,
which ignores case outside strings and treats numbers as equal within 1e-4.

Sequencing: each line starts from the app's initial state and no cookies. A
block of lines indented under a line "scenario: some name" shares state and
cookies in order and counts as one requirement, which passes only when every
step passes. Comments start with "#" and blank lines are ignored.

    parse(text) -> (requirements, errors)
    run(requirements, app=None, command=None, call=None) -> results
    summarize(results) -> counts
    fingerprint(text) -> sha256 hex of the text with line endings normalised
    main(argv)        -> runs a file against a saved project

    uv run python requirements.py <file> --project <id> [--mode live]
"""
import argparse
import hashlib
import itertools
import re
import shutil
import sys
import tempfile
from fractions import Fraction
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

import visualcheck

MAX_DETAIL = 200
SNIPPET = 120
WEB_EXAMPLE = 'GET /notes shows "Buy milk"'
RUN_EXAMPLE = 'run "list" prints "Buy milk"'
CALL_EXAMPLE = 'call (square 12) gives 144'
SCENARIO_EXAMPLE = ('scenario: add then list, with the steps indented under it, for example '
                    'run "add Buy milk" then run "list" prints "Buy milk"')
WEB_WORDS = ("get", "post")
TEXT_SKIPPED = ("script", "style")
CHECK_WORDS = ("then", "shows", "does", "answers", "redirects")
REDIRECTS = (301, 302, 303, 307, 308)
CALL_WORD = re.compile(r"call\b", re.IGNORECASE)
GIVES_WORD = re.compile(r"\s*gives(?=\s|$)", re.IGNORECASE)
KIND_NAMES = {"web": "GET or POST", "command": "run", "call": "call"}
FACTORY_NAMES = {"web": "web app", "command": "command-line app", "call": "Lisp evaluator"}
MISSING = {
    "web": "could not be checked: there is no web app to send GET or POST lines to.",
    "command": "could not be checked: there is no command-line app to run run lines against.",
    "call": "could not be checked: there is no Lisp evaluator to run call lines against.",
}
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eEdDfFsSlL][+-]?\d{1,3})?")
RATIO = re.compile(r"[+-]?\d+/\d+")
STRING_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"?', re.DOTALL)
NUMBER_TOLERANCE = Fraction(1, 10000)


class _Bad(Exception):
    """A line (or part of one) that does not follow the grammar. The message is the reason."""


# -- reading one line into words ---------------------------------------------

def _read_quoted(line, start):
    """``(next_index, text)`` for the quoted string whose opening quote is at START."""
    buf, j, n = [], start + 1, len(line)
    while j < n:
        ch = line[j]
        if ch == "\\" and j + 1 < n and line[j + 1] in '"\\':
            buf.append(line[j + 1])
            j += 2
        elif ch == '"':
            return j + 1, "".join(buf)
        else:
            buf.append(ch)
            j += 1
    raise _Bad("a quoted string is missing its closing double quote")


def _args(line):
    """The words of LINE, in order. A word is a list of ``(quoted, text)`` pieces.

    Pieces with no space between them form one word, so name="two words" is a
    single word. A "#" that starts a word begins a comment up to the end of
    the line. Runs in one pass over the line, so a long line costs no more
    than a short one.
    """
    out, i, n = [], 0, len(line)
    while i < n:
        if line[i].isspace():
            i += 1
            continue
        if line[i] == "#":
            break
        pieces = []
        while i < n and not line[i].isspace():
            if line[i] == '"':
                i, text = _read_quoted(line, i)
                pieces.append((True, text))
            else:
                j = i
                while j < n and not line[j].isspace() and line[j] != '"':
                    j += 1
                pieces.append((False, line[i:j]))
                i = j
        out.append(pieces)
    return out


def _arg(args, k):
    return args[k] if 0 <= k < len(args) else None


def _word(arg, word):
    """True when ARG is the bare keyword WORD in any letter case."""
    return arg is not None and len(arg) == 1 and not arg[0][0] and arg[0][1].lower() == word


def _bare(arg):
    """The text of ARG when it has no quoted piece, else None."""
    if arg is None or any(quoted for quoted, _ in arg):
        return None
    return "".join(text for _, text in arg)


def _quoted_arg(arg):
    """The text of ARG when it is exactly one quoted string, else None."""
    if arg is not None and len(arg) == 1 and arg[0][0]:
        return arg[0][1]
    return None


def _is_path(arg):
    text = _bare(arg)
    return bool(text) and text.startswith("/")


def _is_keyword(arg):
    return any(_word(arg, word) for word in CHECK_WORDS)


def _quoted_text(args, k):
    text = _quoted_arg(_arg(args, k))
    if text is None:
        raise _Bad('the text to look for must be in double quotes')
    if not text.strip():
        raise _Bad("the text to look for is empty")
    return text


def _without_comma(arg):
    """ARG with one separator comma at its end removed (fields may be written "a=b, c=d").

    Returns None when nothing is left, which is a lone comma between two fields.
    A value that really ends in a comma has to be quoted: a=\"x,\".
    """
    if arg and not arg[-1][0] and arg[-1][1].endswith(","):
        arg = arg[:-1] + [(False, arg[-1][1][:-1])]
        if not arg[-1][1]:
            arg = arg[:-1]
    return arg or None


def _field(arg):
    """``[name, value]`` for a word such as a=b or other="two words"."""
    name, value, in_value = "", "", False
    for quoted, text in arg:
        if in_value:
            value += text
        elif not quoted and "=" in text:
            key, _, rest = text.partition("=")
            name += key
            value += rest
            in_value = True
        else:
            name += text
    if not in_value:
        raise _Bad("a field must look like name=value, for example a=b or text=\"two words\"")
    if not name:
        raise _Bad("a field needs a name before the equals sign")
    return [name, value]


# -- one step ------------------------------------------------------------------

def _parse_web(args):
    method = _bare(args[0]).upper()
    if not _is_path(_arg(args, 1)):
        raise _Bad("after %s comes the page path, which starts with a slash" % method)
    path = _bare(args[1])
    i, fields, then = 2, [], None
    if _word(_arg(args, i), "with"):
        if method == "GET":
            raise _Bad("GET takes no fields, so drop the 'with' or use POST")
        i += 1
        while i < len(args) and not _is_keyword(args[i]):
            word = _without_comma(args[i])
            if word is not None:
                fields.append(_field(word))
            i += 1
        if not fields:
            raise _Bad("'with' needs at least one name=value field")
    if _word(_arg(args, i), "then"):
        if method != "POST":
            raise _Bad("only a POST can be followed by 'then'")
        if not _word(_arg(args, i + 1), "get") or not _is_path(_arg(args, i + 2)):
            raise _Bad("'then' must be followed by GET and a page path")
        then = _bare(args[i + 2])
        i += 3
    head = _arg(args, i)
    if _word(head, "shows"):
        expect, value, i = "shows", _quoted_text(args, i + 1), i + 2
    elif _word(head, "does") and _word(_arg(args, i + 1), "not") and _word(_arg(args, i + 2), "show"):
        expect, value, i = "not_shows", _quoted_text(args, i + 3), i + 4
    elif _word(head, "answers"):
        if then is not None:
            raise _Bad("'answers' checks the response to the POST itself, so it cannot follow 'then'")
        code = _bare(_arg(args, i + 1)) or ""
        if not (code.isascii() and code.isdigit()) or not 100 <= int(code) <= 599:
            raise _Bad("'answers' needs an HTTP status number such as 200")
        expect, value, i = "status", int(code), i + 2
    elif _word(head, "redirects") and _word(_arg(args, i + 1), "to"):
        if then is not None:
            raise _Bad("'redirects to' checks the response to the request itself, so it cannot follow 'then'")
        if not _is_path(_arg(args, i + 2)):
            raise _Bad("'redirects to' must be followed by a page path starting with a slash")
        expect, value, i = "redirects", _bare(args[i + 2]), i + 3
    else:
        raise _Bad("after the page path comes what to check: shows \"text\", does not show "
                   "\"text\", answers 200 or redirects to /path")
    if i != len(args):
        raise _Bad("the line has words after the check that it does not use")
    return {"kind": "web", "method": method, "path": path, "fields": fields, "then": then,
            "expect": expect, "value": value}


def _command_words(args, k):
    text = _quoted_arg(_arg(args, k))
    if text is None:
        raise _Bad('the command must be in double quotes, as in run "add Buy milk"')
    words = text.split()
    if not words:
        raise _Bad("the command in quotes is empty")
    return words


def _parse_command(args):
    words = _command_words(args, 1)
    i, then = 2, None
    if _word(_arg(args, i), "then"):
        if not _word(_arg(args, i + 1), "run"):
            raise _Bad("'then' must be followed by run")
        then = _command_words(args, i + 2)
        i += 3
    head = _arg(args, i)
    if _word(head, "prints"):
        expect, value, i = "prints", _quoted_text(args, i + 1), i + 2
    elif _word(head, "does") and _word(_arg(args, i + 1), "not") and _word(_arg(args, i + 2), "print"):
        expect, value, i = "not_prints", _quoted_text(args, i + 3), i + 4
    elif _word(head, "works"):
        expect, value, i = "works", "", i + 1
    else:
        raise _Bad("after the command comes prints \"text\", does not print \"text\" or works")
    if i != len(args):
        raise _Bad("the line has words after the check that it does not use")
    return {"kind": "command", "words": words, "then": then, "expect": expect, "value": value}


def _call_form(line, start):
    """``(form, end)``: the balanced Lisp form that follows START, and the index just after it.

    Parentheses inside a double-quoted string do not count, and a backslash
    escapes the character after it inside a string. A ";" is not a comment.
    """
    i, n = start, len(line)
    while i < n and line[i].isspace():
        i += 1
    if i >= n or line[i] != "(":
        raise _Bad("after call comes one Lisp form in parentheses, for example (square 12)")
    depth, j, quoted = 0, i, False
    while j < n:
        ch = line[j]
        if quoted:
            if ch == "\\":
                j += 1                 # the escaped character is part of the string
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return line[i:j + 1], j + 1
        j += 1
    if quoted:
        raise _Bad("a string inside the Lisp form is missing its closing double quote")
    raise _Bad("the Lisp form after call is missing a closing parenthesis")


def _parse_call(line):
    form, end = _call_form(line, CALL_WORD.match(line).end())
    rest = line[end:]
    gives = GIVES_WORD.match(rest)
    if gives:
        value = rest[gives.end():].strip()
        if not value:
            raise _Bad("gives must be followed by the printed value, for example gives 144")
        return {"kind": "call", "form": form, "expect": "gives", "value": value}
    args = _args(rest)
    head = _arg(args, 0)
    if _word(head, "works"):
        expect, value, i = "works", "", 1
    elif _word(head, "shows"):
        expect, value, i = "shows", _quoted_text(args, 1), 2
    elif _word(head, "does") and _word(_arg(args, 1), "not") and _word(_arg(args, 2), "show"):
        expect, value, i = "not_shows", _quoted_text(args, 3), 4
    else:
        raise _Bad("after the Lisp form comes gives VALUE, shows \"text\", does not show \"text\" "
                   "or works")
    if i != len(args):
        raise _Bad("the line has words after the check that it does not use")
    return {"kind": "call", "form": form, "expect": expect, "value": value}


def _problem(reason, head):
    if head in WEB_WORDS:
        example = WEB_EXAMPLE
    elif head == "run":
        example = RUN_EXAMPLE
    elif head == "call":
        example = CALL_EXAMPLE
    else:
        example = "%s, %s, or %s" % (WEB_EXAMPLE, RUN_EXAMPLE, CALL_EXAMPLE)
    return "%s. Example of a valid line: %s." % (reason[:1].upper() + reason[1:], example)


def _step(line):
    """The step that LINE describes. Raises _Bad with a full problem sentence."""
    words = line.split()
    is_call = CALL_WORD.match(line) is not None
    head = "call" if is_call else (words[0].lower() if words else "")
    if line.lower().startswith("scenario:"):
        raise _Bad(_problem("a scenario cannot sit inside another scenario", head))
    try:
        if is_call:
            return _parse_call(line)
        args = _args(line)
        first = _bare(args[0]) if args else None
        if first and first.lower() in WEB_WORDS:
            return _parse_web(args)
        if first and first.lower() == "run":
            return _parse_command(args)
        raise _Bad("a line must start with GET, POST, run or call")
    except _Bad as exc:
        raise _Bad(_problem(str(exc), head))


# -- the file ------------------------------------------------------------------

def parse(text):
    """``(requirements, errors)`` for a requirements file.

    Each requirement is ``{"id", "text", "steps", "line"}``. A line that does
    not parse gives an error and a requirement whose steps are None, so it is
    reported as not checked rather than dropped.
    """
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    reqs, errors = [], []
    block = None                  # the scenario being read

    def fail(number, line, problem):
        errors.append({"line": number, "text": line, "problem": problem})

    def add(number, line, steps):
        reqs.append({"id": "R%d" % (len(reqs) + 1), "text": line, "steps": steps, "line": number})

    def close():
        nonlocal block
        if block is None:
            return
        if not block["broken"] and not block["steps"]:
            fail(block["line"], block["head"], "The scenario has no steps. Indent the checks "
                 "under its line. Example of a valid block: %s." % SCENARIO_EXAMPLE)
            block["broken"] = True
        text_lines = block["lines"]
        add(block["line"], "\n".join(text_lines), None if block["broken"] else block["steps"])
        block = None

    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        indented = raw[:1] in (" ", "\t")
        if block is not None and indented:
            block["lines"].append(line)
            try:
                step = _step(line)
            except _Bad as exc:
                fail(number, line, str(exc))
                block["broken"] = True
                continue
            if block["kind"] is None:
                block["kind"] = step["kind"]
            elif block["kind"] != step["kind"]:
                fail(number, line, "A scenario cannot mix %s lines with %s lines. "
                     "Example of a valid block: %s." % (KIND_NAMES[block["kind"]],
                                                        KIND_NAMES[step["kind"]], SCENARIO_EXAMPLE))
                block["broken"] = True
                continue
            block["steps"].append(step)
            continue
        close()
        if line.lower().startswith("scenario:"):
            name = line[len("scenario:"):].strip()
            block = {"line": number, "head": line, "lines": [line], "steps": [], "kind": None,
                     "broken": False}
            if not name:
                fail(number, line, "A scenario line needs a name after the colon. Example of a "
                     "valid block: %s." % SCENARIO_EXAMPLE)
                block["broken"] = True
            continue
        try:
            add(number, line, [_step(line)])
        except _Bad as exc:
            fail(number, line, str(exc))
            add(number, line, None)
    close()
    return reqs, errors


# -- looking at what the app answered ----------------------------------------

class _Visible(HTMLParser):
    """The text of a page with its tags removed; script and style text is skipped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skipping = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in TEXT_SKIPPED:
            self._skipping += 1

    def handle_endtag(self, tag):
        if tag in TEXT_SKIPPED and self._skipping:
            self._skipping -= 1

    def handle_data(self, data):
        if not self._skipping:
            self.parts.append(data)


def visible_text(html):
    """HTML as the visitor reads it: tags removed, whitespace collapsed."""
    reader = _Visible()
    try:
        reader.feed(str(html or ""))
        reader.close()
    except Exception:  # noqa: BLE001 - broken markup keeps the text read so far
        pass
    return " ".join("".join(reader.parts).split())


def _collapse(text):
    return " ".join(str(text).split())


def _shows(html, text):
    """True when TEXT occurs in the visible text, or in the raw HTML when TEXT has a tag in it."""
    if _collapse(text) in visible_text(html):
        return True
    return "<" in text and text in str(html)


def _clip(text):
    """A detail sentence: whitespace collapsed, at most MAX_DETAIL characters."""
    text = _collapse(text)
    return text if len(text) <= MAX_DETAIL else text[:MAX_DETAIL - 3].rstrip() + "..."


def _plain(text):
    """A detail sentence: tags removed, whitespace collapsed, at most MAX_DETAIL characters."""
    text = str(text)
    if "<" in text:
        text = visible_text(text)
    return _clip(text)


def _printed_form(text):
    """TEXT with whitespace runs collapsed and letters lowered outside string literals.

    Text inside a double-quoted string keeps its letter case and its escapes.
    """
    parts, last = [], 0
    for match in STRING_LITERAL.finditer(text):
        parts.append(text[last:match.start()].lower())
        parts.append(match.group())
        last = match.end()
    parts.append(text[last:].lower())
    return " ".join("".join(parts).split())


def _as_number(text):
    """The exact value of TEXT when it is one integer, decimal or ratio, else None."""
    if len(text) > 80:
        return None
    try:
        if NUMBER.fullmatch(text):
            return Fraction(re.sub("[a-zA-Z]", "e", text))
        if RATIO.fullmatch(text):
            return Fraction(text)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return None


def printed_matches(expected, actual):
    """True when the printed value ACTUAL is the printed value EXPECTED.

    Both are compared after collapsing whitespace and lowering letter case
    outside string literals. Two single numbers (integers, decimals or ratios
    such as 200/3) also match when they differ by at most 1e-4 of the larger
    magnitude, so 36 matches 36.0 and 66.6667 matches 200/3.
    """
    want, got = _printed_form(str(expected)), _printed_form(str(actual))
    if want == got:
        return True
    x, y = _as_number(want), _as_number(got)
    if x is None or y is None:
        return False
    return abs(x - y) <= NUMBER_TOLERANCE * max(abs(x), abs(y))


def _once(app, method, target, jar, fields):
    """One request with the cookies in JAR; JAR takes the cookies the answer sets."""
    headers = {"Cookie": "; ".join("%s=%s" % kv for kv in jar.items())} if jar else {}
    body = urlencode([tuple(f) for f in fields]).encode("utf-8") \
        if (fields and method == "POST") else b""
    status, out_headers, text = app.handle(method, target, headers, body)
    for name, value in out_headers:
        if name.lower() == "set-cookie" and "=" in value:
            key, val = value.split(";", 1)[0].split("=", 1)
            if val:
                jar[key.strip()] = val.strip()
            else:
                jar.pop(key.strip(), None)
    return status, out_headers, text


def _do_web(app, step, jar):
    """``(ok, detail)`` for one web step, sharing JAR with the other steps of its requirement."""
    label = "%s %s" % (step["method"], step["path"]) + \
        (" then GET %s" % step["then"] if step["then"] else "")
    if step["expect"] in ("status", "redirects"):
        status, out_headers, _ = _once(app, step["method"], step["path"], jar, step["fields"])
        if step["expect"] == "status":
            ok = status == step["value"]
            return ok, "%s answered %d%s." % (label, status, "" if ok else ", not %d" % step["value"])
        where = next((v for k, v in out_headers if k.lower() == "location"), None)
        if status not in REDIRECTS or where is None:
            return False, "%s answered %d, which is not a redirect." % (label, status)
        nxt = urlsplit(urljoin(step["path"], where))
        target = (nxt.path or "/") + ("?" + nxt.query if nxt.query else "")
        want = step["value"]
        ok = target == want or ("?" not in want and nxt.path == want)
        return ok, ("%s redirects to %s." % (label, target) if ok else
                    "%s redirects to %s, not %s." % (label, target, want))
    first = visualcheck.fetch(app, step["method"], step["path"], jar,
                              form=[tuple(f) for f in step["fields"]])
    page = first
    if step["then"]:
        page = visualcheck.fetch(app, "GET", step["then"], jar)
    found = _shows(page["html"], step["value"])
    ends = " (ends on %s)" % page["path"] if page["path"] != (step["then"] or step["path"]) else ""
    if step["expect"] == "shows":
        if found:
            return True, '%s%s shows "%s".' % (label, ends, step["value"])
        return False, '%s%s does not show "%s"; the page reads "%s".' % (
            label, ends, step["value"], visible_text(page["html"])[:SNIPPET])
    if not found:
        return True, '%s%s does not show "%s".' % (label, ends, step["value"])
    return False, '%s%s shows "%s", which it must not.' % (label, ends, step["value"])


def _do_command(run_words, step):
    """``(ok, detail)`` for one command step.

    With "then", the first command runs for its effect and the second one is
    the one whose output is checked.
    """
    label = 'run "%s"' % " ".join(step["words"]) + \
        (' then run "%s"' % " ".join(step["then"]) if step["then"] else "")
    words = step["words"]
    if step["then"] is not None:
        run_words(step["words"])
        words = step["then"]
    output = str(run_words(words) or "")
    if step["expect"] == "works":            # an error in a command is raised by run_words
        return True, '%s ran without an error; it printed "%s".' % (label, _collapse(output)[:SNIPPET])
    found = _collapse(step["value"]) in _collapse(output)
    if step["expect"] == "prints":
        if found:
            return True, '%s printed "%s".' % (label, step["value"])
        return False, '%s did not print "%s"; it printed "%s".' % (
            label, step["value"], _collapse(output)[:SNIPPET])
    if not found:
        return True, '%s did not print "%s".' % (label, step["value"])
    return False, '%s printed "%s", which it must not.' % (label, step["value"])


def _do_call(evaluate, step):
    """``(ok, detail)`` for one call step. EVALUATE runs the form and returns its printed value.

    A Lisp error raised by EVALUATE fails the step, and its message is reported.
    """
    label = "call %s" % step["form"]
    try:
        printed = str(evaluate(step["form"]))
    except Exception as exc:  # noqa: BLE001 - a Lisp error fails the step and is reported
        message = str(exc) or type(exc).__name__
        return False, "%s raised: %s%s" % (label, message, "" if message.endswith(".") else ".")
    shown = _collapse(printed)
    if step["expect"] == "works":
        return True, "%s ran without an error; it gave %s." % (label, shown)
    if step["expect"] == "gives":
        if printed_matches(step["value"], printed):
            return True, "%s gave %s." % (label, shown)
        return False, "%s gave %s, not %s." % (label, shown, step["value"])
    found = _collapse(step["value"]) in shown
    if step["expect"] == "shows":
        if found:
            return True, '%s gave %s, which shows "%s".' % (label, shown, step["value"])
        return False, '%s gave %s, which does not show "%s".' % (label, shown, step["value"])
    if not found:
        return True, '%s gave %s, which does not show "%s".' % (label, shown, step["value"])
    return False, '%s gave %s, which shows "%s", but it must not.' % (label, shown, step["value"])


def _run_one(req, app, command, call=None):
    base = {"id": req["id"], "text": req["text"]}
    steps = req.get("steps")
    if steps is None:
        return dict(base, ok=None, steps=[], detail=(
            "could not be checked: the line does not follow the grammar (see the error for "
            "line %d)." % req["line"]))
    kind = steps[0]["kind"]
    factory = {"web": app, "command": command, "call": call}[kind]
    if factory is None:
        return dict(base, ok=None, steps=[], detail=MISSING[kind])
    try:
        session = factory()
    except Exception as exc:  # noqa: BLE001 - a factory that fails fails the requirement
        return dict(base, ok=False, steps=[], detail=_plain(
            "could not start a fresh %s: %s: %s." % (FACTORY_NAMES[kind], type(exc).__name__, exc)))
    tidy = _clip if kind == "call" else _plain
    jar, results = {}, []
    for step in steps:
        try:
            if kind == "web":
                ok, detail = _do_web(session, step, jar)
            elif kind == "command":
                ok, detail = _do_command(session, step)
            else:
                ok, detail = _do_call(session, step)
        except Exception as exc:  # noqa: BLE001 - a step that raises is a failed step
            ok, detail = False, "the step raised %s: %s." % (type(exc).__name__, exc)
        results.append({"ok": ok, "detail": tidy(detail)})
        if not ok:
            break
    failed = not results[-1]["ok"]
    if len(steps) == 1:
        detail = results[0]["detail"]
    elif failed:
        detail = "Step %d of %d failed: %s" % (len(results), len(steps), results[-1]["detail"])
    else:
        detail = "All %d steps passed." % len(steps)
    return dict(base, ok=not failed, detail=tidy(detail), steps=results)


def run(requirements, app=None, command=None, call=None):
    """Check each requirement and return one result per requirement, in order.

    APP is a zero-argument factory returning a fresh web app (anything with
    ``handle(method, target, headers, body)``); COMMAND is a zero-argument
    factory returning a fresh ``run_words(list_of_words) -> output text``;
    CALL is a zero-argument factory returning a fresh ``evaluate(form_source)
    -> printed value text``, which raises when the Lisp call signals an error.
    A fresh one is made for every requirement, so no line sees another's
    state. Passing an app object instead of a factory is refused, because it
    would carry state from one requirement to the next.
    """
    for name, factory in (("app", app), ("command", command), ("call", call)):
        if factory is not None and not callable(factory):
            raise TypeError("%s must be a factory such as make_%s, not an object: an object "
                            "keeps its state from one requirement to the next" % (name, name))
    return [_run_one(req, app, command, call) for req in requirements]


def summarize(results):
    """``{"total", "met", "unmet", "unchecked", "unmet_ids", "unmet_texts"}``."""
    unmet = [r for r in results if r["ok"] is False]
    return {"total": len(results),
            "met": sum(1 for r in results if r["ok"] is True),
            "unmet": len(unmet),
            "unchecked": sum(1 for r in results if r["ok"] is None),
            "unmet_ids": [r["id"] for r in unmet],
            "unmet_texts": [r["text"] for r in unmet]}


def fingerprint(text):
    """sha256 hex of TEXT with CRLF and CR turned into LF, so a caller can prove it was not altered."""
    normal = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normal.encode("utf-8")).hexdigest()


# -- command line ----------------------------------------------------------------

LABELS = {True: "MET", False: "UNMET", None: "NOT CHECKED"}


def main(argv=None):
    import agent_session as ag          # loads the agent and its Lisp worker
    import mount
    import projects

    ap = argparse.ArgumentParser(description="Check a requirements file against a saved project.")
    ap.add_argument("file", help="the requirements file")
    ap.add_argument("--project", required=True, help="the saved project id")
    ap.add_argument("--mode", choices=("demo", "live"), default="live")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except OSError as exc:
        print("cannot read %s: %s" % (args.file, exc), file=sys.stderr)
        return 2
    store = projects.ProjectStore(ag.AGENT_DIR)
    if not store.exists(args.project):
        print("no saved project called %s" % args.project, file=sys.stderr)
        return 2
    registry = ag.ToolRegistry(store.tools_path(args.project)).for_mode(args.mode)
    names = {t["name"] for t in registry.load()}
    saved, _ = mount.paths_for(ag.AGENT_DIR, args.project)
    workdir = Path(tempfile.mkdtemp(prefix="gg-req-"))
    counter = itertools.count(1)

    def fresh_store():
        """A state store on a copy of the project's saved state, so the project is never written."""
        dest = workdir / ("state-%d.sqlite" % next(counter))
        if saved.exists():
            shutil.copyfile(saved, dest)
        return mount.StateStore(dest)

    def make_app():
        return mount.MountedApp(registry, fresh_store())

    def make_command():
        app = mount.MountedApp(registry, fresh_store())

        def run_words(words):
            out = app.run_command(list(words))
            if not out["ok"]:
                raise RuntimeError(out["error"])
            return out["output"]
        return run_words

    def make_call():
        app = mount.MountedApp(registry, fresh_store())

        def evaluate(form):
            return app._eval("(let ((*print-pretty* nil)) %s)" % form)["return_value"]
        return evaluate

    try:
        requirements, errors = parse(text)
        results = run(requirements,
                      app=make_app if mount.HANDLER in names else None,
                      command=make_command if mount.COMMAND in names else None,
                      call=make_call if names else None)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    for err in errors:
        print("ERROR line %d: %s" % (err["line"], err["problem"]))
    for r in results:
        print("%-11s %-4s %s | %s" % (LABELS[r["ok"]], r["id"],
                                      " / ".join(r["text"].split("\n")), r["detail"]))
    if not results:
        print("no requirements in %s" % args.file)
        return 1
    s = summarize(results)
    print("%d met, %d unmet, %d not checked, of %d requirement(s)."
          % (s["met"], s["unmet"], s["unchecked"], s["total"]))
    if s["unmet_ids"]:
        print("unmet: %s" % ", ".join(s["unmet_ids"]))
    return 0 if not s["unmet"] and not s["unchecked"] else 1


if __name__ == "__main__":
    sys.exit(main())
