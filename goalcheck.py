"""Compare what a goal asks for with what a plan or toolset provides.

Plain keyword matching, no model call. A feature is asked for when its goal
regex matches the prompt, and covered when its evidence regex matches any of
the texts (plan step names and specs, or tool names, descriptions and
definitions). The module also reports saved Lisp functions that nothing calls.
"""

import re

# (key, label, goal_regex, evidence_regex, hint). Patterns match case-insensitively.
FEATURES = (
    (
        "login",
        "login and session security",
        r"\b(?:log[\s-]*ins?|sign[\s-]*ins?|logon|auth(?:entic\w*|oriz\w*)?|pass?w\w*|sessions?|accounts?|secur\w*|sucur\w*|secruity)\b",
        r"\b(?:log[\s-]*(?:in|out)|sign[\s-]*in|auth(?:entic\w*|oriz\w*)?|sessions?|passwords?|current[\s-]*user|require[\s-]*login)\b",
        "a login page and form, a session cookie, logout, and refusing changes when not logged in",
    ),
    (
        "create",
        "adding items",
        r"\b(?:add(?:s|ed|ing)?|creat\w*|new|mana?g\w*|crud|admin\w*|editor\w*|blogs?|posts?)\b",
        r"\badd-|\bcreate-|\bnew-|\bhandle-add|\binsert",
        "a form on a page to add an item, and the POST handler that saves it",
    ),
    (
        "update",
        "editing items",
        r"\b(?:edit(?:s|ed|ing)?|updat\w*|chang\w*|modif\w*|mana?g\w*|crud)\b",
        r"\bedit|\bupdate|\bmodify|\bset-",
        "an edit form and its POST handler",
    ),
    (
        "delete",
        "deleting items",
        r"\b(?:d[ei]?l[ei]t\w*|remov\w*|mana?g\w*|crud)\b",
        r"\bdelete|\bremove",
        "a delete button or link and its handler",
    ),
    (
        "list",
        "listing pages",
        r"\b(?:lists?|listing|views?|shows?|browse\w*|pages?|catalog\w*|dashboards?|mana?g\w*|crud|blogs?|sites?|websites?)\b",
        r"\blist|\brender-|-pages?\b|\bindex|\btable|\bhome",
        "a page that lists the items",
    ),
    (
        "styling",
        "styling",
        r"\b(?:css|styl\w*|stlye|themes?|themed|design\w*|pretty|nice[\s-]+looking)\b",
        r"\bcss|\bstyl",
        "one stylesheet function that every page includes in a <style> tag",
    ),
    (
        "seed",
        "seeded data",
        r"\b(?:seed\w*|(?:sample|example|demo|initial)[\s-]+data|pre-?populat\w*|fixtures?)\b",
        r"\binitial-state|\bseed",
        "initial-state with at least three realistic seeded rows",
    ),
    (
        "search",
        "search and filter",
        r"\b(?:search\w*|serach\w*|filter\w*|find\w*)\b",
        r"\bsearch|\bfilter|\bquery-value",
        "a search box and a filtered list",
    ),
    (
        "validation",
        "input validation",
        r"\b(?:valid\w*|required[\s-]+fields?)\b",
        r"\b(?:in)?valid|\brequired|\bblank|\bempty",
        "refuse empty or malformed form fields with a message",
    ),
)

_EVIDENCE = {key: evidence for key, _label, _goal, evidence, _hint in FEATURES}

# Functions the harness calls itself, so nothing in the plan needs to call them.
_ENTRY_POINTS = frozenset({"handle-request", "handle-command", "initial-state"})

# Lisp string literals, line comments and block comments, in one pass so that
# a ';' inside a string or a '"' inside a comment is handled correctly.
_NON_CODE = re.compile(r'"(?:\\.|[^"\\])*"|;[^\n]*|#\|.*?\|#', re.DOTALL)


def goal_features(prompt):
    """Return the features the goal asks for, in FEATURES order.

    Each feature is a dict with "key", "label" and "hint". A feature is asked
    for when its goal regex matches the prompt, ignoring case.
    """
    return [
        {"key": key, "label": label, "hint": hint}
        for key, label, goal, _evidence, hint in FEATURES
        if re.search(goal, prompt, re.IGNORECASE)
    ]


def covered(features, texts):
    """Return {key: bool} saying whether each feature is evidenced in texts.

    A feature is covered when its evidence regex matches any of the texts,
    ignoring case. Texts are plan step names and specs, or tool names,
    descriptions and definitions.
    """
    texts = tuple(texts)
    return {
        feature["key"]: any(
            re.search(_EVIDENCE[feature["key"]], text, re.IGNORECASE) for text in texts
        )
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
