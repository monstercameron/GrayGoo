"""Compare what a goal asks for with what a plan or toolset provides.

Plain keyword matching, no model call. A feature is asked for when its goal
regex matches the prompt, and covered when its evidence matches the name and
description of a step or tool. Bodies of Lisp definitions are not evidence:
only the name of each definition and its docstring are read, so a call to
``list`` inside a function does not make it a listing. The module also reports
saved Lisp functions that nothing calls.

The features are guesses from the wording of the goal, not requirements: each
carries ``"basis": "keyword"``, and ``describe`` says so when a caller shows them.
"""

import re

# Words that name a verb followed, within three words, by an object noun: "edit
# items", "update a record", "change the price of", "manage two products".
_OBJECT = (r"(?:items?|records?|entr(?:y|ies)|products?|posts?|contacts?|customers?|orders?|"
           r"tasks?|todos?|notes?|users?|rows?|articles?|books?)")
_MANAGEMENT = r"\bmanagement\b"          # "product management" names the whole job


def _verb_near(verbs):
    """A verb from VERBS and an object noun at most three words after it."""
    return r"\b(?:%s)\w*\s+(?:%s\b|\w+\s+%s\b|\w+\s+\w+\s+%s\b)" % (verbs, _OBJECT, _OBJECT, _OBJECT)


# (key, label, goal_regex, evidence, hint). The goal regex matches the prompt,
# ignoring case. Evidence is a dict read by _hits: "words" are whole name parts
# or words, "prefixes" start a name part, "pairs" are two parts in a row, and
# "leads" is a part that counts when another part follows it.
FEATURES = (
    (
        "login",
        "login and session security",
        r"\b(?:log[\s-]*ins?|sign[\s-]*ins?|log[\s-]*outs?|sign[\s-]*outs?|logon|auth|"
        r"authentic\w*|authoriz\w*|pass?w\w*|securit\w*|sucur\w*|secruity)\b",
        {"words": ("login", "logins", "logon", "logout", "signin", "signout", "password",
                   "passwords", "passwd", "auth"),
         "prefixes": ("authentic", "authoriz"),
         "pairs": (("log", "in"), ("log", "out"), ("sign", "in"), ("sign", "out"),
                   ("current", "user"))},
        "a way to sign in and to sign out; after signing out, the signed-in pages are no longer shown",
    ),
    (
        "create",
        "adding items",
        r"(?:\b(?:add(?:s|ed|ing)?|creat\w*|new|crud|admin\w*|blogs?|posts?)\b|%s|%s)"
        % (_MANAGEMENT, _verb_near("manag")),
        {"words": ("add", "adds", "added", "adding", "create", "creates", "created", "creating",
                   "insert", "inserts", "inserted", "inserting"),
         "leads": ("new",)},
        "a way to add an item and see it afterwards",
    ),
    (
        "update",
        "editing items",
        r"(?:\b(?:edit(?:s|ed|ing)?|updat\w*|renam\w*|crud)\b|%s|%s)"
        % (_MANAGEMENT, _verb_near("chang|modif|manag")),
        {"words": ("edit", "edits", "edited", "editing", "update", "updates", "updated", "updating",
                   "modify", "modifies", "modified", "modifying", "rename", "renames", "renamed"),
         "leads": ("set",)},
        "a way to change an existing item and see the change",
    ),
    (
        "delete",
        "deleting items",
        r"(?:\b(?:d[ei]?l[ei]t\w*|remov\w*|crud)\b|%s|%s)" % (_MANAGEMENT, _verb_near("manag")),
        {"prefixes": ("delete", "remove")},
        "a way to remove an item and see that it is gone",
    ),
    (
        "list",
        "listing pages",
        r"(?:\b(?:lists?\s+(?:of|all)|listing|catalog\w*|dashboards?|index\s+page|pages|blogs?)\b"
        r"|%s|%s)" % (_MANAGEMENT, _verb_near("manag")),
        {"words": ("table", "tables", "catalog", "catalogue", "dashboard", "home", "<page-part>"),
         "prefixes": ("list", "index")},
        "a view that shows the stored items",
    ),
    (
        "styling",
        "styling",
        r"\b(?:css|styl\w*|stlye|themes?|themed|pretty|nice[\s-]+looking|look[\s-]+nice)\b",
        {"prefixes": ("css", "styl")},
        "output that looks deliberately styled, with the same look on every page",
    ),
    (
        "seed",
        "seeded data",
        r"\b(?:seed\w*|(?:sample|example|demo|initial)[\s-]+data|pre-?populat\w*|fixtures?)\b",
        {"words": ("fixture", "fixtures"),
         "prefixes": ("seed",),
         "pairs": (("initial", "state"), ("initial", "data"), ("sample", "data"),
                   ("example", "data"), ("demo", "data"))},
        "some example items already present when the app starts, so it can be tried at once",
    ),
    (
        "search",
        "search and filter",
        r"\b(?:search\w*|serach\w*|filter\w*)\b",
        {"prefixes": ("search", "filter")},
        "a way to type text and see only the matching items",
    ),
    (
        "validation",
        "input validation",
        r"\b(?:validat\w*|required[\s-]+fields?|reject\w*\s+invalid\w*)\b",
        {"words": ("valid", "invalid", "required", "blank"),
         "prefixes": ("validat",)},
        "an empty or malformed input is refused with a message, and the stored data is left unchanged",
    ),
)

_EVIDENCE = {key: evidence for key, _label, _goal, evidence, _hint in FEATURES}

# Functions the harness calls itself, so nothing in the plan needs to call them.
_ENTRY_POINTS = frozenset({"handle-request", "handle-command", "initial-state"})

# Helpers the harness supplies to every app (webkit kit). Mentioning one does not
# show that a feature is built: table-rows reads a table, it does not list one.
KIT_NAMES = frozenset({
    "request-field", "pair-value", "form-value", "query-value", "cookie-value",
    "table-rows", "with-table-rows", "html-escape", "html-page", "redirect-to",
    "with-state", "with-cookie", "join-strings", "hash-password", "password-matches-p",
})

# Lisp string literals, line comments and block comments, in one pass so that
# a ';' inside a string or a '"' inside a comment is handled correctly.
_NON_CODE = re.compile(r'"(?:\\.|[^"\\])*"|;[^\n]*|#\|.*?\|#', re.DOTALL)

_SYMBOL = re.compile(r"[a-z][a-z0-9-]*")
_DEF_NAME = re.compile(r"\(\s*def\w*\s+([^\s()\"';]+)", re.IGNORECASE)
_DOCSTRING = re.compile(
    r"\(\s*def(?:un|macro)\s+[^\s()\"';]+\s+\([^()]{0,200}\)\s*\"((?:\\.|[^\"\\]){0,2000})\"",
    re.IGNORECASE)
_PAGE_PART = "<page-part>"


def goal_features(prompt):
    """Return the features the goal asks for, in FEATURES order.

    Each feature is a dict with "key", "label", "hint" and "basis" ("keyword":
    the feature was inferred from the wording). A feature is asked for when its
    goal regex matches the prompt, ignoring case.
    """
    return [
        {"key": key, "label": label, "hint": hint, "basis": "keyword"}
        for key, label, goal, _evidence, hint in FEATURES
        if re.search(goal, prompt, re.IGNORECASE)
    ]


def describe(features):
    """Each feature as one line that says it was inferred from the wording of the goal."""
    return ["%s (inferred from the wording of the goal)" % feature["label"] for feature in features]


def _prose(text):
    """TEXT without its parenthesised forms: a Lisp definition or call, body included, is dropped.

    A quote or a parenthesis inside a string of a form does not end the form.
    Text outside any form is kept as it is.
    """
    kept, depth, quoted, escaped = [], 0, False, False
    for ch in text:
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
            if depth == 0:
                kept.append(ch)
            continue
        if ch == '"':
            quoted = True
        elif ch == "(":
            depth += 1
            continue
        elif ch == ")" and depth:
            depth -= 1
            continue
        if depth == 0:
            kept.append(ch)
    return "".join(kept)


def _tokens(text):
    """The name parts and description words of TEXT as lower-case tokens.

    Symbols are split at '-' ("cmd-add" gives cmd and add). Kit helper names are
    skipped. A name after the first part that is "page" or "pages" adds a marker
    token, because a page named after items is a listing page.
    """
    source = text or ""
    parts = [_prose(source)] + _DEF_NAME.findall(source) + _DOCSTRING.findall(source)
    tokens = []
    for part in parts:
        for symbol in _SYMBOL.findall(part.lower()):
            name = symbol.strip("-")
            if not name or name in KIT_NAMES:
                continue
            pieces = [piece for piece in name.split("-") if piece]
            tokens += pieces
            if any(piece in ("page", "pages") for piece in pieces[1:]):
                tokens.append(_PAGE_PART)
    return tokens


def _hits(tokens, evidence):
    """True when the tokens carry one of the evidence entries of a feature."""
    words = evidence.get("words", ())
    prefixes = evidence.get("prefixes", ())
    pairs = evidence.get("pairs", ())
    leads = evidence.get("leads", ())
    last = len(tokens) - 1
    for index, token in enumerate(tokens):
        if token in words or token.startswith(prefixes):
            return True
        if index < last and (token in leads or (token, tokens[index + 1]) in pairs):
            return True
    return False


def covered(features, texts):
    """Return {key: bool} saying whether each feature is evidenced in texts.

    A feature is covered when one of the texts carries its evidence: a name
    part, a description word or a docstring. Texts are plan step names with
    their specs, or tool names with their descriptions.
    """
    tokenized = [_tokens(text) for text in texts]
    return {
        feature["key"]: any(_hits(tokens, _EVIDENCE[feature["key"]]) for tokens in tokenized)
        for feature in features
    }


def missing(features, texts):
    """Return the features (the same dicts) that have no evidence in texts."""
    done = covered(features, texts)
    return [feature for feature in features if not done[feature["key"]]]


def _is_kit(tool):
    """Return True for harness-supplied helpers (kit flag or the web-kit session)."""
    return bool(tool.get("kit")) or tool.get("session") == "web-kit"


def _calls(name, code):
    """Return True when name appears as a whole Lisp symbol in code.

    The symbol must start the text or follow whitespace, '(' or "'" (which
    also covers "#'"), and must end the text or be followed by whitespace or
    ')'. Case is ignored, as the Lisp reader folds case.
    """
    pattern = r"(?<![^\s('])" + re.escape(name) + r"(?![^\s)])"
    return re.search(pattern, code, re.IGNORECASE) is not None


def unused_functions(tools):
    """Return the names of saved functions that nothing calls.

    Kit tools and the harness entry points are never reported. A function
    counts as called when another tool's definition calls it, outside string
    literals and comments. Names are returned in input order.
    """
    code = [_NON_CODE.sub(" ", tool["definition"]) for tool in tools]
    unused = []
    for index, tool in enumerate(tools):
        name = tool["name"]
        if _is_kit(tool) or name in _ENTRY_POINTS:
            continue
        if not any(
            _calls(name, other) for other_index, other in enumerate(code) if other_index != index
        ):
            unused.append(name)
    return unused
