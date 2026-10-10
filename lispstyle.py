"""House style for the Lisp tools the agent builds: small, pure, single-purpose.

Pure Python and pure functions: text in, findings out. No model, no SBCL.

* ``STYLE_GUIDE``      - the paragraph added to the build prompt.
* ``ensure_docstring`` - gives every ``defun`` a docstring (from the plan's
  description) so each tool states its own interface.
* ``purity_problems``  - hard rules; a tool that breaks one is not saved.
* ``case_problems``    - hard rule: CASE can never match a string key.
* ``security_problems``- hard rules for tools that compare secrets.
* ``trust_problems``    - hard rules: no redefining, removing or shadowing trusted
  functions, no building code at run time, no package games.
* ``style_notes``      - soft observations kept with the tool, never blocking.
"""
import re

STYLE_GUIDE = (
    "STYLE\n"
    "- Every tool is a PURE function of its arguments: no globals, no printing, "
    "no clock or random numbers, and never modify an argument (REMOVE, REVERSE, "
    "(sort (copy-list xs) ...), not DELETE, NREVERSE or SORT on a parameter).\n"
    "- One purpose per function, a few lines; compose small tools.\n"
    "- Prefer MAPCAR, REDUCE, REMOVE-IF, FIND, SOME, EVERY or recursion to index "
    "loops that SETF an accumulator.\n"
    "- Join a LIST of strings with (apply #'concatenate 'string (mapcar ...)) or "
    "(format nil \"~{~a~}\" list); never pass the list itself to CONCATENATE.\n"
    "- A line break comes from FORMAT: (format nil \"~{~a~^~%~}\" lines); in an "
    "expected test value write it as ~% inside the quoted string.\n"
    "- Keep each argument's shape the same across tools: records are plain "
    "lists, tables are lists of records.\n"
    "- STRING= and CHAR= take no :TEST; pass :test #'string= to ASSOC, FIND, "
    "MEMBER and REMOVE when keys are strings. CASE cannot match strings: use "
    "COND with STRING=.\n"
    "- In expected values symbols and keywords may be lower case, but STRINGS "
    "keep their exact case and their double quotes.\n"
    "- Compare passwords and other secrets exactly (STRING=). Name predicates "
    "with a -p suffix."
)

_STRINGS = re.compile(r'"(?:[^"\\]|\\.)*"')
_COMMENTS = re.compile(r";[^\n]*")


def code_only(definition):
    """DEFINITION with string contents and comments blanked, for safe matching."""
    return _COMMENTS.sub("", _STRINGS.sub('""', definition or ""))


def _form_end(text, start):
    """Index of the ``)`` closing the list that opens at START, or -1."""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def defun_parts(definition):
    """``(name, params, body_start)`` of a ``(defun name (params) ...)``, else None.

    ``body_start`` is the index just past the lambda list.
    """
    m = re.match(r"\s*\(defun\s+(\S+)\s*", definition or "", re.I)
    if not m or definition[m.end():m.end() + 1] != "(":
        return None
    end = _form_end(definition, m.end())
    if end < 0:
        return None
    params = [p for p in definition[m.end() + 1:end].split() if not p.startswith("&")]
    return m.group(1).lower(), [p.strip("()").lower() for p in params], end + 1


def has_docstring(definition):
    """True when the first body form is a string followed by more body."""
    parts = defun_parts(definition)
    if not parts:
        return False
    rest = definition[parts[2]:].lstrip()
    m = _STRINGS.match(rest)
    return bool(m) and rest[m.end():].strip() not in ("", ")")


def ensure_docstring(definition, description):
    """DEFINITION with DESCRIPTION as its docstring when it has none."""
    parts = defun_parts(definition)
    text = " ".join(str(description or "").split())
    if not parts or not text or has_docstring(definition):
        return definition
    doc = '"%s"' % text.replace("\\", "\\\\").replace('"', "'")
    return "%s\n  %s%s" % (definition[:parts[2]], doc,
                           definition[parts[2]:] if definition[parts[2]:].startswith(("\n", " "))
                           else " " + definition[parts[2]:])


_GLOBALS = re.compile(r"\(\s*(defvar|defparameter|defconstant|defglobal)\b", re.I)
_SET_SPECIAL = re.compile(r"\(\s*(?:setf|setq|incf|decf|push)\s+(\*[^\s()]+\*)", re.I)
_PRINTS = re.compile(r"\(\s*(print|princ|prin1|terpri|write-line|write-string|pprint)\b"
                     r"|\(\s*format\s+t\b", re.I)
_DESTRUCTIVE = ("sort", "stable-sort", "nreverse")
_IMPURE = re.compile(r"\(\s*(get-universal-time|get-decoded-time|get-internal-real-time|"
                     r"get-internal-run-time|random|make-random-state|gensym|gentemp|sleep)\b",
                     re.I)


def purity_problems(definition):
    """Reasons this tool is not a pure function (empty when it is)."""
    code = code_only(definition)
    out = []
    m = _GLOBALS.search(code)
    if m:
        out.append("uses global state (%s): pass the value in as an argument instead"
                   % m.group(1).lower())
    m = _SET_SPECIAL.search(code)
    if m:
        out.append("modifies the global %s: return a new value instead" % m.group(1))
    m = _IMPURE.search(code)
    if m:
        out.append("%s reads the clock or random numbers, so the result changes from "
                   "call to call and cannot be tested: take the time (or a seed) as an "
                   "ARGUMENT instead; the harness passes the real time in"
                   % m.group(1).upper())
    if _PRINTS.search(code):
        out.append("prints output: return the value (use (format nil ...) for strings)")
    parts = defun_parts(definition)
    for param in (parts[1] if parts else []):
        m = re.search(r"\(\s*(%s)\s+%s(?![^\s()])" % ("|".join(_DESTRUCTIVE), re.escape(param)),
                      code, re.I)
        if m:
            op = m.group(1).lower()
            out.append("%s destroys its argument %s: use %s"
                       % (op.upper(), param,
                          "(reverse %s)" % param if op == "nreverse"
                          else "(%s (copy-list %s) ...)" % (op, param)))
    return out


_CASE_FORM = re.compile(r"\(\s*(?:e|c)?case\s", re.I)
_CASE_STRING = ("CASE compares with EQL and can never match a string: use "
                '(cond ((string= x "add") ...) ...)')


def case_problems(definition):
    """Reasons a CASE/ECASE/CCASE clause tests a string (empty when none does).

    CASE compares keys with EQL, so a string key such as ("add") never matches.
    Strings are blanked by ``code_only``, so a string key reads as ``""``.
    """
    code = code_only(definition)
    for m in _CASE_FORM.finditer(code):
        for begin, end in _top_items(code, m.start())[2:]:
            clause = code[begin:end]
            inner = _top_items(clause, 0) if clause.startswith("(") else []
            if not inner:
                continue
            key = clause[inner[0][0]:inner[0][1]]
            keys = [key[b:e] for b, e in _top_items(key, 0)] if key.startswith("(") else [key]
            if '""' in keys:
                return [_CASE_STRING]
    return []


_SECRET_NAME = re.compile(r"(?<![a-z])(pw|pass|passwd|password|secret|token|credential)s?(?![a-z])", re.I)
_LOOSE_COMPARE = re.compile(r"\(\s*(string-equal|char-equal|equalp|string-not-equal)\b", re.I)


def security_problems(name, description, definition):
    """Reasons a secret-handling tool is unsafe (empty for other tools)."""
    if not _SECRET_NAME.search("%s %s" % (name or "", description or "")):
        return []
    m = _LOOSE_COMPARE.search(code_only(definition))
    if m:
        return ["%s ignores letter case, so \"SECRET\" would match \"secret\": compare "
                "passwords and other secrets exactly with STRING=, and do not write a "
                "test that expects different case to match" % m.group(1).upper()]
    return []


_ACCUMULATE = re.compile(r"\(\s*setf\s+(\S+)\s+\(\s*(concatenate|append|cons|\+)\b[^()]*\s\1[\s)]", re.I)


def style_notes(definition):
    """Soft, non-blocking observations about how lispy the tool is."""
    code = code_only(definition)
    notes = []
    if _ACCUMULATE.search(code):
        notes.append("builds its result by SETF in a loop; MAPCAR/REDUCE would say it in one expression")
    if re.search(r"\(\s*(aref|char|elt|nth)\s+\S+\s+i\)", code, re.I) and \
            re.search(r"\(\s*(dotimes|loop)\b", code, re.I):
        notes.append("walks by index; iterating the sequence itself is clearer")
    lines = [ln for ln in (definition or "").splitlines() if ln.strip()]
    if len(lines) > 14:
        notes.append("is %d lines long; consider splitting it" % len(lines))
    if not has_docstring(definition):
        notes.append("has no docstring")
    return notes


def _top_items(text, start):
    """``[(begin, end)]`` of the top-level items inside the list opening at START."""
    out, i, n = [], start + 1, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == ")":
            break
        begin = i
        while i < n and text[i] in "'#`,":
            i += 1
        if i < n and text[i] == "(":
            end = _form_end(text, i)
            i = (end + 1) if end >= 0 else n
        elif i < n and text[i] == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
        else:
            while i < n and text[i] not in " \t\r\n()":
                i += 1
        out.append((begin, i))
    return out


def let_to_let_star(definition):
    """Turn ``LET`` into ``LET*`` where a binding uses an earlier one of the same LET.

    ``(let ((tasks (f s)) (id (g tasks))) ...)`` fails with "TASKS is unbound":
    LET evaluates its bindings in parallel. The rewrite is made only when the
    earlier name is not a parameter of the function, so the code could not have
    meant an outer variable of that name.
    """
    parts = defun_parts(definition)
    params = set(parts[1]) if parts else set()
    out, pos = [], 0
    for m in re.finditer(r"\(\s*let(?=\s*\()", definition, re.I):
        open_bindings = definition.index("(", m.end())
        seen = []
        needs_star = False
        for begin, end in _top_items(definition, open_bindings):
            item = definition[begin:end]
            if not item.startswith("("):
                seen.append(item.lower())
                continue
            inner = _top_items(item, 0)
            if not inner:
                continue
            name = item[inner[0][0]:inner[0][1]].lower()
            init = code_only(item[inner[0][1]:])
            if any(v not in params and re.search(
                    r"(?<![^\s('])%s(?![^\s)])" % re.escape(v), init, re.I) for v in seen):
                needs_star = True
                break
            seen.append(name)
        if needs_star:
            out.append(definition[pos:m.end()] + "*")
            pos = m.end()
    out.append(definition[pos:])
    return "".join(out)


# ---- escape_problems: raw request or record data written into HTML ----------

_HTML_TAG = re.compile(
    r"</?(?:html|head|body|title|table|tr|td|th|ul|ol|li|h[1-6]|p|a|div|span|input|"
    r"option|select|label|button|form|img|pre|section|header|footer|nav|main|article)"
    r"(?=[\s/>])", re.I)
_FORMAT_NIL = re.compile(r'\(\s*format\s+nil\s+(?=")', re.I)
_CONCATENATE = re.compile(r"\(\s*concatenate\s+'?string\b", re.I)
_RAW_HEADS = frozenset(("first", "second", "third", "fourth", "nth", "car", "cadr", "caddr",
                        "elt", "form-value", "query-value", "cookie-value", "request-field",
                        "pair-value"))
_RISKY_PARAMS = frozenset(("title", "name", "body", "text", "description", "username", "user",
                           "message", "comment", "content", "label", "value", "query", "email",
                           "price"))


def _masked(definition):
    """DEFINITION with string contents and comments blanked to spaces, same length.

    Positions line up with DEFINITION, so a match in the masked text is sliced from
    the original, and parentheses inside strings and comments are invisible.
    """
    text = definition or ""
    n, i, out = len(text), 0, []
    while i < n:
        c = text[i]
        if c == '"':
            out.append('"')
            i += 1
            while i < n and text[i] != '"':
                step = min(2 if text[i] == "\\" else 1, n - i)
                out.append(" " * step)
                i += step
            if i < n:
                out.append('"')
                i += 1
        elif c == ";":
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _is_raw_form(text, masked, begin):
    """True when the form at BEGIN reads raw data: an accessor or a request reader."""
    if masked[begin:begin + 1] != "(":
        return False
    items = _top_items(masked, begin)
    if not items:
        return False
    head = masked[items[0][0]:items[0][1]].lower()
    if head == "getf":
        return len(items) > 1 and text[items[1][0]:items[1][1]].lower() == "request"
    return head in _RAW_HEADS


def _let_bindings(text, masked):
    """``{name: (is_raw, init)}`` for each LET and LET* binding in the function."""
    found = {}
    for m in re.finditer(r"\(\s*let\*?(?=[\s(])", masked, re.I):
        items = _top_items(masked, m.start())
        if len(items) < 2 or masked[items[1][0]:items[1][0] + 1] != "(":
            continue
        for b, _ in _top_items(masked, items[1][0]):
            if masked[b:b + 1] != "(":
                continue
            pair = _top_items(masked, b)
            if len(pair) < 2:
                continue
            (name_b, name_e), (init_b, init_e) = pair[0], pair[1]
            found[masked[name_b:name_e].lower()] = (
                _is_raw_form(text, masked, init_b),
                " ".join(text[init_b:init_e].split()))
    return found


def _html_calls(text, masked):
    """``[(begin, first_arg)]`` of each FORMAT or CONCATENATE call that builds HTML.

    A FORMAT call needs an HTML tag in its control string; a CONCATENATE call needs a
    string argument with one. FIRST_ARG is the index of the first real argument.
    """
    calls = []
    for m in _FORMAT_NIL.finditer(masked):
        quote = m.end()
        close = masked.find('"', quote + 1)
        if close >= 0 and _HTML_TAG.search(text[quote:close + 1]):
            calls.append((m.start(), 3))
    for m in _CONCATENATE.finditer(masked):
        items = _top_items(masked, m.start())
        if any(masked[b:b + 1] == '"' and _HTML_TAG.search(text[b:e]) for b, e in items[2:]):
            calls.append((m.start(), 2))
    return calls


def _escape_reason(src, binding=None):
    """The reason a value written as SRC goes into HTML unescaped (BINDING: its let form)."""
    if binding is None:
        return ("puts %s into HTML without escaping: wrap it as (html-escape %s) "
                "so text like <script> cannot run" % (src, src))
    return ("puts %s into HTML without escaping: it is bound to %s, so wrap that as "
            "(html-escape %s) so text like <script> cannot run" % (src, binding, binding))


def escape_problems(definition):
    """Reasons this function puts raw data into HTML without html-escape (empty when fine)."""
    text = definition or ""
    masked = _masked(text)
    parts = defun_parts(text)
    params = set(parts[1]) if parts else set()
    bound = _let_bindings(text, masked)
    out = []
    for begin, first_arg in _html_calls(text, masked):
        for b, e in _top_items(masked, begin)[first_arg:]:
            src = " ".join(text[b:e].split())
            if masked[b:b + 1] == '"':
                continue
            if masked[b:b + 1] == "(":
                if _is_raw_form(text, masked, b):
                    out.append(_escape_reason(src))
                continue
            name = masked[b:e].lower()
            if name in bound:
                if bound[name][0]:
                    out.append(_escape_reason(src, bound[name][1]))
            elif name in params and name in _RISKY_PARAMS:
                out.append(_escape_reason(src))
    reasons = []
    for reason in out:
        if reason not in reasons:
            reasons.append(reason)
    return reasons[:3]


# The comma after a CSS rule. One plain repetition only: this pattern, and every
# check made around a match, runs in time linear in the text. (The first version
# described a whole rule with nested repetitions; on one long stylesheet from a
# live build it backtracked for good and froze the server.)
_CSS_COMMA = re.compile(r"\}\s*,")
_CSS_PSEUDO = re.compile(r":[\w-]+\([^()]*\)")          # :not(.a), :nth-child(2)
_NOT_A_SELECTOR = '()=;"\\'


def _css_selector(text):
    """True when TEXT can be the selector of a CSS rule (``a:hover, .b > c``)."""
    text = _CSS_PSEUDO.sub("", text).strip()
    return bool(text) and not text.endswith(":") and not text.startswith("@") and \
        not any(c in text for c in _NOT_A_SELECTOR)


def _css_rule_ends(body, close):
    """True when the ``}`` at CLOSE ends ``selector { property: value }``."""
    opened = body.rfind("{", 0, close)
    if opened < 0:
        return False
    block = body[opened + 1:close]
    if ":" not in block or "}" in block:
        return False
    start = max(body.rfind("}", 0, opened), body.rfind("{", 0, opened),
                body.rfind(";", 0, opened)) + 1
    return _css_selector(body[start:opened])


def _css_rule_starts(body, at):
    """True when a CSS rule starts at AT, or the string ends there (the next rule is the next string)."""
    if not body[at:].strip():
        return True
    opened = body.find("{", at)
    return opened >= 0 and _css_selector(body[at:opened])


def _drop_css_commas(body):
    out, last = [], 0
    for m in _CSS_COMMA.finditer(body):
        if _css_rule_ends(body, m.start()) and _css_rule_starts(body, m.end()):
            out.append(body[last:m.start() + 1])       # up to and including the "}"
            last = m.end()                             # ...and on after the comma
    out.append(body[last:])
    return "".join(out)


def fix_css_commas(definition):
    """Drop the commas a model puts BETWEEN CSS rules: ``a{x:1},b{y:2}`` is not CSS.

    A browser that meets ``},`` throws away the rule after it, and since every
    rule then starts with a comma, the whole stylesheet is ignored: the page
    renders with no styling at all although every test passes. Seen live: a
    stylesheet function built as ``"a{..}," "b{..},"`` left an app unstyled
    through two rounds of screenshot fixes. Only a comma directly after a
    rule's closing brace is removed, where no valid CSS has one, and only when
    what stands before and after it reads as CSS rules: a script's
    ``{a:1},{b:2}`` or ``{k: {a:1}, m: 2}`` is left alone.
    """
    if "}" not in definition:
        return definition
    return _STRINGS.sub(lambda m: '"%s"' % _drop_css_commas(m.group(0)[1:-1]), definition)


# ---- trust_problems: a tool may not change, or reach around, trusted code ----------

# The reader's own rules, one token at a time, so that a quote inside a comment opens
# nothing, a semicolon inside a string starts nothing, and a character such as #\"
# is a character. (code_only is not enough here: it blanks strings first, so a quote
# inside a comment would hide the code after it from the checks.) Each alternative is
# a single linear scan; the only repetitions are plain character runs.
_LEX = re.compile(
    r'(?:"(?:[^"\\]|\\.)*(?:"|\Z))+'  # strings in a row, unterminated: to the end of the text
    r"|;[^\n]*"                       # a line comment
    r"|#\|.*?(?:\|#|\Z)"              # a block comment
    r"|#\\."                          # a character: #\( or #\"
    r"|(?:\|(?:[^|\\]|\\.)*(?:\||\Z))+"  # |symbol names| in a row, with their escapes
    r"|(?:\\.)+"                      # backslash-escaped symbol characters in a row
    r"|#+(?![\\|])"                   # hash marks that begin no comment or character
    r'|[^"#;|\\]+'                    # ordinary code, in one run
    r"|.", re.S)

_NAME_END = r"(?![^\s()'`,;\"])"      # the name ends here
_NAME_START = r"(?<![^\s()'`,])"      # the name starts here

_DEFINER_NOUNS = {
    "defun": "function", "defmacro": "macro", "defgeneric": "generic function",
    "defmethod": "method", "defsetf": "setf expander", "define-setf-expander": "setf expander",
    "define-modify-macro": "macro", "define-compiler-macro": "compiler macro",
    "define-symbol-macro": "symbol macro", "deftype": "type", "defstruct": "structure",
    "defclass": "class", "defpackage": "package", "in-package": "package",
}


def _alternatives(words):
    return "|".join(sorted(words, key=len, reverse=True))


_DEFINER = re.compile(r"\(\s*(" + _alternatives(_DEFINER_NOUNS) + ")" + _NAME_END, re.I)
_NAME_AFTER = re.compile(r"\s*([^\s()'`,\"]*)")
_REDEFINE_SETF = re.compile(r"\(\s*p?setf(?:\s*\(\s*|\s+)(symbol-function|fdefinition|"
                            r"macro-function|compiler-macro-function|symbol-value|get)"
                            + _NAME_END, re.I)
_REDEFINE_NAMED = re.compile(_NAME_START + r"(fmakunbound|makunbound|unintern)" + _NAME_END, re.I)
# A call has an argument: (load) with none is a lambda list naming a parameter LOAD.
_HAS_ARG = r"(?=\s*[^\s)])"
_TRACE = re.compile(r"\(\s*(trace|untrace)" + _NAME_END + _HAS_ARG, re.I)
_SET_VALUE = re.compile(r"\(\s*set" + _NAME_END + _HAS_ARG, re.I)
_LOCAL_FORM = re.compile(r"\(\s*(flet|labels|macrolet)" + _NAME_END, re.I)
_SPACE = re.compile(r"\s*")
_BINDING = re.compile(r"\(\s*([^\s()'`,\"]+)")
_RUN_OPS = _alternatives(("eval", "compile", "compile-file", "load", "read", "read-from-string",
                          "intern", "find-symbol", "symbol-function", "fdefinition",
                          "macroexpand", "macroexpand-1"))
_RUN_CALL = re.compile(r"\(\s*(" + _RUN_OPS + ")" + _NAME_END + _HAS_ARG, re.I)
_RUN_QUOTED = re.compile(r"(?:'|\(\s*(?:quote|function)\s+)(" + _RUN_OPS + ")" + _NAME_END, re.I)
_FUNCALL_COMPUTED = re.compile(r"\(\s*(funcall|apply)\s*\(\s*(?!(?:lambda|function)" + _NAME_END + ")",
                               re.I)
_COERCE_LAMBDA = re.compile(r"\(\s*coerce\s+['`]\(\s*lambda" + _NAME_END, re.I)
_TOKEN = re.compile(r"[^\s()'`,\"]+")


def _trust_code(definition):
    """``(code, escapes)``: DEFINITION as the reader sees it, for the trust rules.

    Strings and comments are blanked; a character such as ``#\\(`` becomes ``#x``.
    A symbol written with ``|`` or a backslash keeps its length, with ``x`` in place
    of its letters, so no parenthesis or quote hides in it; ESCAPES lists those
    symbol names as written.
    """
    escapes = []

    def blank(m):
        tok = m.group(0)
        if tok[0] == '"':
            return '""'
        if tok[0] == ";":
            return ""
        if tok.startswith("#|"):
            return " "
        if tok.startswith("#\\"):
            return "#x"
        if tok[0] in "|\\":
            escapes.append(tok)
            return "x" * len(tok)
        return tok

    return _LEX.sub(blank, definition or ""), escapes


def _close_table(code):
    """``{open: close}`` for every parenthesis pair in CODE, found in one pass."""
    stack, table = [], {}
    for i, c in enumerate(code):
        if c == "(":
            stack.append(i)
        elif c == ")" and stack:
            table[stack.pop()] = i
    return table


def _keyword_lookup(code, start, close):
    """True for ``(intern x :keyword)`` or ``(find-symbol x :keyword)``, the call at START.

    Only keywords are reached that way: a keyword is data, not a function a tool could call.
    """
    end = close.get(start)
    if end is None:
        return False
    last = end
    while last > 0 and code[last - 1].isspace():
        last -= 1
    first = last
    while first > 0 and not code[first - 1].isspace() and code[first - 1] not in "()":
        first -= 1
    return code[first:last] == ":keyword"


def _local_names(code):
    """``[(name, position)]`` of each function or macro that FLET, LABELS or MACROLET binds."""
    close = _close_table(code)
    found = []
    for m in _LOCAL_FORM.finditer(code):
        i = _SPACE.match(code, m.end()).end()
        if code[i:i + 1] != "(":
            continue
        i += 1
        while True:
            i = _SPACE.match(code, i).end()
            if code[i:i + 1] != "(":
                break
            name = _BINDING.match(code, i)
            if name:
                found.append((name.group(1).lower(), i))
            end = close.get(i)
            if end is None:
                break
            i = end + 1
    return found


def trust_problems(definition, reserved=()):
    """Why a candidate may not run next to the harness's trusted functions: a list of sentences.

    RESERVED lists the function names the candidate does not own; names starting with
    ``gg-`` are always reserved. Each rule contributes at most one sentence. Text inside
    strings, comments and characters never triggers a rule or hides code from one.
    """
    code, escapes = _trust_code(definition)
    taken = frozenset(str(name).lower() for name in reserved)

    def reserved_name(name):
        return name.startswith("gg-") or name in taken

    out = []

    # 1. The tool's own name.
    head = re.match(r"\s*\(\s*defun\s+([^\s()'`,\"]+)", code, re.I)
    top = len(code) - len(code.lstrip())          # where the top-level form opens
    if head and head.group(1).lower().startswith("gg-"):
        out.append("the name %s is reserved for the harness: pick another name"
                   % head.group(1).lower())

    # 2. Any other definition inside the tool.
    for m in _DEFINER.finditer(code):
        kind = m.group(1).lower()
        if m.start() == top and kind == "defun":
            continue
        if kind == "in-package":
            out.append("a tool is one function: it may not change the package inside itself")
        else:
            name = _NAME_AFTER.match(code, m.end()).group(1)
            out.append("a tool is one function: it may not define a %s inside itself"
                       % ("%s %s" % (_DEFINER_NOUNS[kind], name)).strip())
        break

    # 3. Redefining, removing or shadowing a function.
    rule3 = []
    m = _REDEFINE_SETF.search(code)
    if m:
        rule3.append((m.start(), "it may not redefine or remove functions (setf %s)"
                      % m.group(1).lower()))
    m = _REDEFINE_NAMED.search(code) or _TRACE.search(code)
    if m:
        rule3.append((m.start(), "it may not redefine or remove functions (%s)"
                      % m.group(1).lower()))
    m = _SET_VALUE.search(code)
    if m:
        rule3.append((m.start(), "it may not change the value of a global symbol (set)"))
    for name, pos in (_local_names(code) if _LOCAL_FORM.search(code) else []):
        if reserved_name(name):
            rule3.append((pos, "it may not shadow the reserved function %s with a local "
                          "definition" % name))
    if rule3:
        out.append(min(rule3)[1])

    # 4. Building or looking up code at run time.
    rule4 = []
    closes = None
    for m in _RUN_CALL.finditer(code):
        op = m.group(1).lower()
        if op in ("intern", "find-symbol"):
            closes = _close_table(code) if closes is None else closes
            if _keyword_lookup(code, m.start(), closes):
                continue
        rule4.append((m.start(), op))
        break
    m = _RUN_QUOTED.search(code)
    if m:
        rule4.append((m.start(), "'" + m.group(1).lower()))
    m = _FUNCALL_COMPUTED.search(code)
    if m:
        rule4.append((m.start(), m.group(1).lower()))
    m = _COERCE_LAMBDA.search(code)
    if m:
        rule4.append((m.start(), "coerce of a quoted lambda"))
    if rule4:
        out.append("it may not build or look up code at run time (%s)" % min(rule4)[1])

    # 5. Package-qualified names, escaped symbol names, reader evaluation.
    rule5 = None
    for m in _TOKEN.finditer(code):
        if ":" in m.group(0)[1:]:
            rule5 = "it may not use package-qualified names (%s)" % m.group(0)
            break
    if rule5 is None and escapes:
        rule5 = ("it may not use escaped symbol names (%s): write the plain name"
                 % escapes[0][:40])
    if rule5 is None and "#." in code:
        rule5 = "reader evaluation (#.) is not allowed"
    if rule5:
        out.append(rule5)
    return out
