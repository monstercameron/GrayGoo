"""House style for the Lisp tools the agent builds: small, pure, single-purpose.

Pure Python and pure functions: text in, findings out. No model, no SBCL.

* ``STYLE_GUIDE``      - the paragraph added to the build prompt.
* ``ensure_docstring`` - gives every ``defun`` a docstring (from the plan's
  description) so each tool states its own interface.
* ``purity_problems``  - hard rules; a tool that breaks one is not saved.
* ``case_problems``    - hard rule: CASE can never match a string key.
* ``security_problems``- hard rules for tools that compare secrets.
* ``style_notes``      - soft observations kept with the tool, never blocking.
"""
import re

STYLE_GUIDE = (
    "STYLE - write idiomatic, clean Common Lisp. "
    "Every tool is a PURE function: its result depends only on its arguments; "
    "no global variables, no printing, and never modify an argument (use "
    "REMOVE, REVERSE, (sort (copy-list xs) ...) rather than DELETE, NREVERSE or "
    "SORT on a parameter). "
    "One purpose per function, a few lines long; compose small tools rather "
    "than growing one. "
    "Prefer expressions over step-by-step mutation: MAPCAR, REDUCE, REMOVE-IF, "
    "FIND, ASSOC, SOME, EVERY, or recursion, rather than index loops that SETF "
    "an accumulator. To join a LIST of strings use "
    "(apply #'concatenate 'string (mapcar ...)) or (format nil \"~{~a~}\" list); "
    "never pass the list itself to CONCATENATE. "
    "Give every argument one clear shape and keep it the same across tools: "
    "records are plain lists or alists, tables are lists of records. "
    "STRING= and CHAR= take no :TEST argument; pass :test #'string= or "
    ":test #'equal to ASSOC, FIND, MEMBER and REMOVE when keys are strings. "
    "A line break in a string comes from FORMAT: (format nil \"~a~%~a\" a b) or "
    "(format nil \"~{~a~^~%~}\" lines); in an expected test value write it as ~% "
    "inside the quoted string. In expected values symbols and keywords may be "
    "lower case, but STRINGS keep their exact case and their double quotes. "
    "Name predicates with a -p suffix. Compare passwords and other secrets "
    "exactly (STRING=), never case-insensitively. "
    "Begin each DEFUN with a one-line docstring naming the arguments and the "
    "value returned."
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
