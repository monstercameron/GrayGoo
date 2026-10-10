"""Short repair replies turned back into full candidate plans.

When a candidate Common Lisp function fails its tests, the model is asked to
repair it. Sending the whole plan back costs many output tokens for what is
often a one-line change, so the model may answer in one of two shorter ways:

1. An EDIT: the name of the function plus a list of replacements, each saying
   which exact text to find in the current definition ("old") and what to put
   in its place ("new"). Edits apply one after another, so a later edit sees
   the text as the earlier edits left it.
2. A PARTIAL build: a normal "build" reply that leaves out fields that did
   not change (for example no "tests").

merge_reply() takes the previous full plan and such a reply and returns the
full plan the reply means. apply_edits() does the text work and raises
EditFailed, with a plain-words reason, when an edit cannot be applied safely.

An edit or partial build must name the version of the definition it was written against (a definition_sha, given as expect_sha or as the reply's own "base"), and a reply aimed at a different version is refused rather than patched. After the edits, check_definition() requires the result to be exactly one well-formed defun with the same name and parameter list, and an edit that breaks that is rejected with the reason.
"""
import copy
import hashlib
import re

_WHITESPACE = re.compile(r"\s+")
_MISSING = object()
_WHITE = frozenset(" \t\r\n\f\v")
_DELIMS = frozenset('()";')
_PREFIX = frozenset("'`,@#")


class EditFailed(ValueError):
    """An edit reply could not be applied; the message says why in plain words."""


def _excerpt(text):
    """The first 80 characters of TEXT with runs of whitespace collapsed."""
    return _WHITESPACE.sub(" ", text[:80])


def _occurs_error(count, old):
    return EditFailed(
        'the text to replace occurs %d times in the definition; '
        'give more of the surrounding text: "%s"' % (count, _excerpt(old)))


def _not_found_error(old):
    return EditFailed(
        'the text to replace was not found in the definition: "%s"' % _excerpt(old))


def _key(edit, *names):
    for name in names:
        if name in edit:
            return edit[name]
    return _MISSING


def _edit_pair(edit):
    """(old, new) for one edit reply item, or None when it is malformed."""
    if isinstance(edit, dict):
        old = _key(edit, "old", "find")
        new = _key(edit, "new", "replace")
    elif isinstance(edit, (list, tuple)) and len(edit) == 2:
        old, new = edit
    else:
        return None
    if not isinstance(old, str) or not old or not isinstance(new, str):
        return None
    return old, new


def _parse_edits(edits):
    """The (old, new) pairs of EDITS; raises EditFailed when any is unusable."""
    if not isinstance(edits, list) or not edits:
        raise EditFailed("no edits were given")
    pairs = []
    for number, edit in enumerate(edits, 1):
        pair = _edit_pair(edit)
        if pair is None:
            raise EditFailed(
                'edit %d is not of the form {"old": text, "new": text}' % number)
        pairs.append(pair)
    return pairs


def _replace_once(text, old, new):
    """TEXT with the single occurrence of OLD replaced by NEW (see apply_edits)."""
    count = text.count(old)
    if count == 1:
        start = text.index(old)
        return text[:start] + new + text[start + len(old):]
    if count > 1:
        raise _occurs_error(count, old)

    pieces = old.split()
    if not pieces:
        raise _not_found_error(old)
    pattern = re.compile(r"\s+".join(re.escape(piece) for piece in pieces))
    matches = list(pattern.finditer(text))
    if not matches:
        raise _not_found_error(old)
    if len(matches) > 1:
        raise _occurs_error(len(matches), old)
    match = matches[0]
    return text[:match.start()] + new + text[match.end():]


def _apply(definition, pairs):
    text = definition
    for old, new in pairs:
        text = _replace_once(text, old, new)
    if text == definition:
        raise EditFailed("the edits change nothing")
    return text


def apply_edits(definition, edits):
    """DEFINITION with EDITS applied in order; raises EditFailed."""
    pairs = _parse_edits(edits)
    if not isinstance(definition, str) or not definition:
        raise EditFailed("there is no definition to edit")
    return _apply(definition, pairs)


def _norm_name(name):
    return name.strip().lower() if isinstance(name, str) else ""


def _blank(value):
    return not (isinstance(value, str) and value.strip())


def _squash(text):
    return _WHITESPACE.sub(" ", text).strip()


def definition_sha(definition):
    """The first 16 hex characters of the SHA-256 of DEFINITION.

    Line endings are read as LF and trailing whitespace is ignored, so the same
    text written with CRLF or with padding gives the same version. Anything
    that is not a string gives "".
    """
    if not isinstance(definition, str):
        return ""
    lines = definition.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    text = "\n".join(line.rstrip() for line in lines).rstrip()
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


class _Node(object):
    """One lexical item: kind is list, atom, string, char, prefix or root."""
    __slots__ = ("kind", "start", "end", "children")

    def __init__(self, kind, start, end=None, children=None):
        self.kind = kind
        self.start = start
        self.end = end
        self.children = children


def _read(text):
    """Nest TEXT into nodes; returns (root, unclosed, extra).

    ROOT's children are the top-level nodes. UNCLOSED counts lists still open
    at the end and EXTRA counts closing parens with nothing to close. Parens
    inside strings, inside character literals and after a semicolon are not
    counted.
    """
    root = _Node("root", 0, len(text), [])
    stack = [root]
    extra = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in _WHITE:
            i += 1
        elif c == ";":
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif c == "(":
            node = _Node("list", i, None, [])
            stack[-1].children.append(node)
            stack.append(node)
            i += 1
        elif c == ")":
            if len(stack) > 1:
                node = stack.pop()
                node.end = i + 1
            else:
                extra += 1
            i += 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise EditFailed("the edit leaves a string that is never closed")
            stack[-1].children.append(_Node("string", i, j + 1))
            i = j + 1
        elif c == "#" and text[i + 1:i + 2] == "\\":
            j = min(i + 3, n)
            while j < n and text[j] not in _WHITE and text[j] not in _DELIMS:
                j += 1
            stack[-1].children.append(_Node("char", i, j))
            i = j
        else:
            j = i + 1
            while j < n and text[j] not in _WHITE and text[j] not in _DELIMS \
                    and not (text[j] == "#" and text[j + 1:j + 2] == "\\"):
                j += 1
            token = text[i:j]
            kind = "prefix" if all(ch in _PREFIX for ch in token) else "atom"
            stack[-1].children.append(_Node(kind, i, j))
            i = j
    return root, len(stack) - 1, extra


def _items(nodes):
    """(start, node) for each form among NODES.

    A quote-like prefix (for example a lone apostrophe) is not a form of its
    own: it joins the form that follows it, and its start is then the start
    of the prefix.
    """
    items = []
    start = None
    last = None
    for node in nodes:
        last = node
        if node.kind == "prefix":
            if start is None:
                start = node.start
            continue
        items.append((node.start if start is None else start, node))
        start = None
    if start is not None:
        items.append((start, last))
    return items


def _balance_error(detail):
    return EditFailed(
        "the edit leaves the definition with unbalanced parentheses (%s)" % detail)


def _structure(definition, name=None):
    """(found name, parameters) of DEFINITION; raises EditFailed unless it is one defun.

    NAME, when it is given, must match the defun's name ignoring case. The
    parameters are the lambda list without its outer parentheses, with
    whitespace collapsed.
    """
    if not isinstance(definition, str):
        raise EditFailed("there is no definition to check")
    root, unclosed, extra = _read(definition)
    if unclosed:
        raise _balance_error("%d unclosed" % unclosed)
    if extra:
        raise _balance_error("%d too many closing" % extra)

    forms = _items(root.children)
    if len(forms) != 1:
        raise EditFailed(
            "the edit leaves %d top-level forms; a tool is exactly one defun" % len(forms))
    start, form = forms[0]
    items = []
    if form.kind == "list" and start == form.start:
        items = _items(form.children)
    if len(items) < 2 or items[0][1].kind != "atom" or items[1][1].kind != "atom" \
            or definition[items[0][0]:items[0][1].end].lower() != "defun":
        raise EditFailed("the edit leaves something that is not a defun")

    found = definition[items[1][0]:items[1][1].end]
    if isinstance(name, str) and name.strip() and _norm_name(found) != _norm_name(name):
        raise EditFailed(
            'the edit changes the function\'s name from "%s" to "%s"' % (name, found))

    if len(items) < 3 or items[2][1].kind != "list" or items[2][0] != items[2][1].start:
        raise EditFailed("the edit removes the parameter list")
    lambda_list = items[2][1]
    return found, _squash(definition[lambda_list.start + 1:lambda_list.end - 1])


def check_definition(definition, name=None):
    """Raise EditFailed unless DEFINITION is exactly one well-formed defun.

    When NAME is given, the defun must be named NAME, ignoring case, and it
    must keep a parameter list.
    """
    _structure(definition, name)


def _params_of(definition):
    """The parameters of DEFINITION, or None when it is not a well-formed defun."""
    try:
        return _structure(definition)[1]
    except EditFailed:
        return None


def _pinned_version(reply, expect_sha):
    """The version a reply must have been written against, or "" when none is named.

    EXPECT_SHA (the caller's record) and the reply's own "base" must agree.
    """
    base = reply.get("base")
    base = base.strip() if isinstance(base, str) else ""
    expected = expect_sha.strip() if isinstance(expect_sha, str) else ""
    if base and expected and base != expected:
        raise EditFailed(
            "the edit names version %s, but the harness expected %s" % (base, expected))
    return base or expected


def _check_version(previous, reply, expect_sha):
    """Refuse a reply written against another version of the definition."""
    expected = _pinned_version(reply, expect_sha)
    if not expected:
        return
    actual = definition_sha(previous["definition"])
    if expected.lower() != actual:
        raise EditFailed(
            "the function changed since this edit was written (expected version %s, found %s); "
            "send the edit again against the current text" % (expected, actual))


def _merge_edit(previous, reply, expect_sha):
    _check_version(previous, reply, expect_sha)

    name = reply.get("name")
    if isinstance(name, str) and _norm_name(name) != _norm_name(previous.get("name")):
        raise EditFailed(
            'the edit names "%s", but the function being repaired is "%s"'
            % (name, previous.get("name") or ""))

    pairs = _parse_edits(reply.get("edits"))
    new_definition = _apply(previous["definition"], pairs)
    _, new_params = _structure(new_definition, previous.get("name"))
    old_params = _params_of(previous["definition"])
    if old_params is not None and old_params.lower() != new_params.lower():
        raise EditFailed(
            "the edit changes the parameters from (%s) to (%s); callers and tests depend "
            "on them, so send a full build to change them" % (old_params, new_params))
    changed = sum(len(old) + len(new) for old, new in pairs)

    merged = copy.deepcopy(previous)
    merged["action"] = "build"
    merged["definition"] = new_definition
    tests = reply.get("tests")
    if isinstance(tests, list) and tests:
        merged["tests"] = copy.deepcopy(tests)
    call = reply.get("call")
    if isinstance(call, str) and call.strip():
        merged["call"] = call
    description = reply.get("description")
    if isinstance(description, str) and description.strip():
        merged["description"] = description
    merged["edited"] = len(pairs)
    merged["saved_chars"] = max(0, len(new_definition) - changed)
    merged["base_sha"] = definition_sha(previous["definition"])
    return merged


def _merge_build(previous, reply, expect_sha):
    name = reply.get("name")
    if not _blank(name) and _norm_name(name) != _norm_name(previous.get("name")):
        return reply
    if _blank(reply.get("definition")):
        _check_version(previous, reply, expect_sha)

    merged = copy.deepcopy(reply)
    kept = []

    def take(field, reply_has_it, previous_has_it):
        if not reply_has_it and previous_has_it:
            merged[field] = copy.deepcopy(previous[field])
            kept.append(field)

    def text_field(field):
        return _blank(reply.get(field)), not _blank(previous.get(field))

    def list_field(field):
        value = reply.get(field)
        return (isinstance(value, list) and bool(value)), \
            isinstance(previous.get(field), list) and bool(previous[field])

    for field in ("name", "definition", "call", "description"):
        reply_blank, previous_ok = text_field(field)
        take(field, not reply_blank, previous_ok)
    reply_has_tests, previous_has_tests = list_field("tests")
    take("tests", reply_has_tests, previous_has_tests)

    merged["kept"] = sorted(kept)
    if "definition" in kept:
        merged["base_sha"] = definition_sha(previous["definition"])
    return merged


def merge_reply(previous, reply, expect_sha=None):
    """The full plan meant by REPLY, given the PREVIOUS candidate plan.

    EXPECT_SHA is the definition_sha the caller recorded when it asked for the
    reply. An edit, or a partial build that would keep the previous definition,
    written against another version is refused; a complete build is not checked.
    """
    if not isinstance(previous, dict) or not isinstance(previous.get("definition"), str) \
            or not previous["definition"]:
        return reply
    if not isinstance(reply, dict):
        return reply
    action = reply.get("action")
    if action == "edit":
        return _merge_edit(previous, reply, expect_sha)
    if action == "build":
        return _merge_build(previous, reply, expect_sha)
    return reply
