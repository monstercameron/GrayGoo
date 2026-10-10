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
"""
import copy
import re

_WHITESPACE = re.compile(r"\s+")
_MISSING = object()


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


def _merge_edit(previous, reply):
    name = reply.get("name")
    if isinstance(name, str) and _norm_name(name) != _norm_name(previous.get("name")):
        raise EditFailed(
            'the edit names "%s", but the function being repaired is "%s"'
            % (name, previous.get("name") or ""))

    pairs = _parse_edits(reply.get("edits"))
    new_definition = _apply(previous["definition"], pairs)
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
    return merged


def _merge_build(previous, reply):
    name = reply.get("name")
    if not _blank(name) and _norm_name(name) != _norm_name(previous.get("name")):
        return reply

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
    return merged


def merge_reply(previous, reply):
    """The full plan meant by REPLY, given the PREVIOUS candidate plan."""
    if not isinstance(previous, dict) or not isinstance(previous.get("definition"), str) \
            or not previous["definition"]:
        return reply
    if not isinstance(reply, dict):
        return reply
    action = reply.get("action")
    if action == "edit":
        return _merge_edit(previous, reply)
    if action == "build":
        return _merge_build(previous, reply)
    return reply
