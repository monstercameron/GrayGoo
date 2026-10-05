"""Deterministic mutation-risk classifier: plan.md section 7 (R0-R6).

Given a parsed ``(candidate ...)`` dict (see :func:`s_expr.parse_candidate`),
:func:`classify` statically scans the ``:definition`` form plus the mutation
``:target`` and returns the highest applicable risk level with reasons and
the rehearsal gates that level requires.

Contract
--------
``classify(candidate_dict, context)`` returns::

    {"level": "R0".."R6", "reasons": [...], "gates": [...]}

* ``level`` is the HIGHEST level with at least one signal (ties impossible;
  multi-signal candidates resolve upward).
* ``reasons`` is a non-empty, deterministically ordered list of human-readable
  citations (``"<level>: ..."``). Pure (R0) candidates get a single default
  reason. At most 100 signals are reported; the rest are counted.
* ``gates`` is a fresh list of required rehearsal gates (cumulative, see below).

Levels and signals (plan.md sections 4.3, 7, 22)
------------------------------------------------
* R0 pure computation: default when nothing else fires (arithmetic,
  mapping/filtering, string building, ...).
* R1 local mutable state: ``setq``/``setf``/``push``/``incf``/``gethash``/
  destructive sequence ops, cache/memo/session signals, console I/O
  (``format``/``print``/``read-line``), clock/randomness. A stream read/write
  alone is R1: file/socket openers (``open``/``with-open-file``/...) carry
  R4, and file-backed code always has an opener nearby, so the max wins.
* R2 shared canonical state: globals (``*earmuffs*``, ``defparameter``/
  ``defvar``), user/account/record/registry/database/queue/memory signals,
  ``invoke-capability``, transactions.
* R3 structural/interface change: ``defstruct``/``defclass``/``defgeneric``/
  ``defmethod``/``defmacro``/``defpackage``/``defcapability``/..., schema /
  migration / serialization / interface / contract signals, runtime
  redefinition (``setf`` of ``symbol-function``/``fdefinition``).
* R4 external effects: network, filesystem, email, payment, process
  (``run-program``/``spawn``/``exec``/``shell``/``fork``/...), FFI, and any
  statically unclassifiable dynamic form (see below).
* R5 agent policy mutation: policy/context/retrieval/prompt-strategy
  targets or references.
* R6 trusted kernel/evaluator (FORBIDDEN): kernel, evaluator, promotion,
  permission, credential, ledger, and related trust-root targets or
  references. Never rehearsed; requires an external release (plan.md 4.3).
  Additionally (adversarial hardening): a target under an
  ``evo.dispatch``/``evo.kernel``-style package, or a definition that
  redefines a dispatch entrypoint symbol (``invoke-capability``) via
  ``defun``/``defmethod``/``setf`` of ``symbol-function``/
  ``fdefinition``/``macro-function``, is R6 (promotion-forbidden).

Gates (cumulative per the "Additional requirements" layering in plan.md 7)
--------------------------------------------------------------------------
* R0: isolated-compile, unit-tests, property-tests, performance-checks.
* R1: + state-diff-validation, replay-testing.
* R2: + forked-state, transaction-simulation, invariant-checking.
* R3: + migration, backwards-compatibility, contract-replay,
  previous-version-coexistence.
* R4: + effect-virtualization, approval-policy, shadow-or-dry-run-evaluation.
* R5: + offline-benchmark, control-group, paired-evaluation, hidden-test-suite,
  statistically-meaningful-result.
* R6: no rehearsal; gates are ["promotion-forbidden", "external-release-only"].

Conservatism (unknown forms)
----------------------------
Anything the static scan cannot classify resolves to AT LEAST R4 with an
explicit "unclassifiable" reason: dynamic/introspective heads (``eval``,
``apply``, ``funcall``, ``compile``, ``load``, ``intern``, ``macroexpand``,
...), malformed form heads, foreign node
types, and unknown declared-effect names. Garbage input (non-dict
candidate, missing/empty/non-list ``definition``, non-symbol ``target``)
is refused outright with ``TypeError``/``ValueError`` ("unclassifiable").

Hygiene rules
-------------
* String literal contents are NEVER scanned (data, not code).
* ``(quote ...)`` subtrees are inert data and are skipped.
* Plan.md 4.5 deterministic queries (``contract-of``, ``callers-of``, ...)
  are exempt as call heads so mere introspection is not flagged.
* Bare verbs are not signals: ``process``/``execute`` (data processing),
  ``prompt`` in a body (console prompt -> R1; in a TARGET it names the
  prompting surface -> R5, except the console compound ``prompt-user``
  -> R1), ``user-interface`` (display code, not user records or API
  contracts).
* Declared effects are read ONLY from the ``:effects``/``:effect`` entry of
  the candidate ``extra`` map and from ``context["effects"]``; ``:reason``
  and ``:claims`` are assertions, not code, and are ignored.

Scope limits
------------
This is a syntactic scan, not a proof: it cannot detect infinite loops,
memory bombs, or worker crashes. Those are the rehearsal boundary's job
(timeouts, memory limits, disposable workers per plan.md section 19).

Determinism: no I/O, no randomness, no clock; reasons are deduplicated and
sorted, so identical inputs always yield identical outputs.
"""

from __future__ import annotations

import s_expr

__all__ = ["LEVELS", "GATES", "classify"]

LEVELS = ("R0", "R1", "R2", "R3", "R4", "R5", "R6")
_RANK = {level: index for index, level in enumerate(LEVELS)}

# Gate steps in plan.md section 7 order; R1-R5 accumulate ("Additional
# requirements"), R6 short-circuits rehearsal entirely (plan.md 4.3).
_GATE_STEPS = (
    ("R0", ("isolated-compile", "unit-tests", "property-tests",
             "performance-checks")),
    ("R1", ("state-diff-validation", "replay-testing")),
    ("R2", ("forked-state", "transaction-simulation", "invariant-checking")),
    ("R3", ("migration", "backwards-compatibility", "contract-replay",
             "previous-version-coexistence")),
    ("R4", ("effect-virtualization", "approval-policy",
             "shadow-or-dry-run-evaluation")),
    ("R5", ("offline-benchmark", "control-group", "paired-evaluation",
             "hidden-test-suite", "statistically-meaningful-result")),
)
GATES: dict[str, list[str]] = {}
_accumulated: list[str] = []
for _level, _step in _GATE_STEPS:
    _accumulated = _accumulated + list(_step)
    GATES[_level] = list(_accumulated)
GATES["R6"] = ["promotion-forbidden", "external-release-only"]
del _level, _step, _accumulated

_MAX_REASONS = 100

_FAMILY = {
    "R1": "local state",
    "R2": "shared canonical state",
    "R3": "structural/interface change",
    "R4": "external effect",
    "R5": "agent policy",
    "R6": "trust-root (forbidden)",
}

# --- Head tables: exact match on package-stripped lowercase head symbol. ---
_R4_DYNAMIC_HEADS = frozenset({  # statically unclassifiable -> R4 floor
    "eval", "apply", "funcall", "macroexpand", "macroexpand-1", "eval-when",
    "compile", "compile-file", "load", "require", "load-system", "intern",
    "find-symbol", "find-all-symbols",
    # NOTE: symbol-function/fdefinition/macro-function reads are plain
    # introspection (dangerous uses still trip funcall/apply or the
    # setf-place rule below), so they are deliberately not dynamic heads.
})
_R4_HEADS = frozenset({
    "open", "reopen", "with-open-file", "with-open-stream", "delete-file",
    "delete-file-if-exists", "rename-file", "copy-file",
    "ensure-directories-exist", "probe-file", "truename", "pathname",
    "parse-namestring", "namestring", "file-write-date", "file-length",
    "run-program", "launch-program", "spawn", "exec", "shell", "send-email",
    "send-mail", "mail", "charge", "http-get", "http-post", "socket-connect",
    "socket-accept", "socket-listen", "defcfun", "defcvar", "foreign-funcall",
    "foreign-funcall-pointer", "load-foreign-library",
    "define-alien-routine", "alien-funcall", "load-shared-object", "dlopen",
})
_R3_HEADS = frozenset({
    "defstruct", "defclass", "defgeneric", "defmethod", "deftype", "defmacro",
    "defpackage", "in-package", "defcapability", "define-capability",
    "definterface", "define-condition", "change-class", "ensure-class",
    "defsetf", "define-setf-expander", "declaim", "proclaim", "export",
    "import", "provide", "fmakunbound",
})
_R2_HEADS = frozenset({
    "defparameter", "defvar", "defglobal", "invoke-capability", "enqueue",
    "dequeue", "queue-push", "queue-pop", "with-transaction", "transact",
    "makunbound", "trace", "untrace",
})
_R1_HEADS = frozenset({
    "setq", "setf", "psetq", "psetf", "set", "incf", "decf", "push",
    "pushnew", "pop", "remf", "shiftf", "rotatef", "gethash", "puthash",
    "remhash", "clrhash", "nconc", "nreverse", "nreconc", "delete",
    "delete-if", "delete-if-not", "delete-duplicates", "sort", "stable-sort",
    "fill", "replace", "vector-push", "vector-push-extend", "vector-pop",
    "adjust-array", "format", "print", "princ", "prin1", "pprint", "write",
    "write-line", "write-string", "write-char", "terpri", "fresh-line",
    "finish-output", "force-output", "clear-output", "close", "read",
    "read-line", "read-char", "read-byte", "write-byte", "get-universal-time",
    "get-decoded-time", "get-internal-real-time", "get-internal-run-time",
    "random", "make-random-state",
})
# Plan.md 4.5 deterministic queries: exempt as call heads.
_EXEMPT_HEADS = frozenset({
    "contract-of", "callers-of", "callees-of", "find-capability",
    "test-capability", "profile-capability", "history-of",
})
# setf places that redefine runtime interface/global cells.
_R3_SETF_PLACES = frozenset(
    {"symbol-function", "fdefinition", "macro-function"})
_R2_SETF_PLACES = frozenset({"symbol-value"})

# --- Substring families (matched on separator-normalized lowercase name). ---
_R6_SUBSTR = frozenset({
    "kernel", "evaluator", "promot", "permission", "credential", "ledger",
    "trust-root", "hidden-test", "hidden-benchmark", "hidden-suite",
    "benchmark-store", "governor", "worker-isolation", "isolation-mechanism",
    "dispatch-cell", "versioned-dispatch", "capability-dispatch",
})
_R5_SUBSTR = frozenset({
    "polic", "context", "retriev", "prompt-strategy", "prompting",
    "system-prompt", "prompt-template", "prompt-builder", "prompt-fingerprint",
    "prompt-policy", "prompt-compiler",
})
_R4_SUBSTR = frozenset({
    "network", "filesystem", "filepath", "pathname", "namestring",
    "run-program", "launch-program", "execute-program", "make-process",
    "process-wait", "process-alive", "process-kill", "process-output",
    "process-input", "process-close", "process-status", "process-list",
    "process-id", "waitpid", "send-mail", "sendmail", "mail", "drakma",
    "curl", "wget", "cffi", "foreign", "dlopen", "billing", "stripe",
    "invoice", "payment", "charge", "email", "smtp", "socket", "spawn",
    "shell", "alien", "http",
})
_R3_SUBSTR = frozenset({
    "serializ", "marshal", "schema", "migrat", "defstruct", "defclass",
    "defgeneric", "defmethod", "defmacro", "defpackage", "defcapability",
    "define-capability", "interface", "contract",
})
_R2_SUBSTR = frozenset({
    "shared", "canonical", "registry", "transact", "enqueue", "dequeue",
    "invoke-capability", "user-record", "account", "database", "queue",
    "user", "record", "global", "patch-memory", "procedural-memory",
    "semantic-memory", "memory-store", "capability-memory", "session-store",
    "distributed-cache", "cache-cluster",
})
_R1_SUBSTR = frozenset({
    "cache", "memo", "session", "clock", "random", "memory", "prompt",
})
# Short tokens use exact token match to avoid verb collisions (display/payload
# must not hit "pay"; profile must not hit "file"; skill must not hit "kill").
_R4_TOKENS = frozenset({
    "file", "files", "path", "paths", "directory", "directories", "url",
    "urls", "dex", "ffi", "exec", "pay", "kill", "fork",
})
_R3_TOKENS = frozenset({"api"})
_R2_TOKENS = frozenset({"db"})

_SUBSTR_BY_LEVEL = (
    ("R6", _R6_SUBSTR),
    ("R5", _R5_SUBSTR),
    ("R4", _R4_SUBSTR),
    ("R3", _R3_SUBSTR),
    ("R2", _R2_SUBSTR),
    ("R1", _R1_SUBSTR),
)
_TOKENS_BY_LEVEL = (
    ("R6", frozenset()),
    ("R5", frozenset()),
    ("R4", _R4_TOKENS),
    ("R3", _R3_TOKENS),
    ("R2", _R2_TOKENS),
    ("R1", frozenset()),
)

# Bare effect keywords use an exact map (unambiguous declaration channel).
_KEYWORD_LEVELS = {
    "kernel": "R6", "evaluator": "R6", "promotion": "R6", "promote": "R6",
    "permission": "R6", "permissions": "R6", "credential": "R6",
    "credentials": "R6", "ledger": "R6",
    "policy": "R5", "policies": "R5", "context": "R5", "retrieval": "R5",
    "retrieve": "R5", "retriever": "R5",
    "network": "R4", "network-read": "R4", "network-write": "R4",
    "filesystem": "R4", "filesystem-read": "R4", "filesystem-write": "R4",
    "file": "R4", "files": "R4", "email": "R4", "payment": "R4",
    "process": "R4", "ffi": "R4", "http": "R4", "https": "R4",
    "socket": "R4", "sockets": "R4", "url": "R4", "urls": "R4",
    "schema": "R3", "schemas": "R3", "migration": "R3", "migrations": "R3",
    "interface": "R3", "interfaces": "R3", "contract": "R3",
    "contracts": "R3", "api": "R3",
    "state": "R2", "states": "R2", "state-read": "R2", "state-write": "R2",
    "queue": "R2", "queues": "R2", "queue-read": "R2", "queue-write": "R2",
    "user": "R2", "users": "R2", "shared": "R2", "canonical": "R2",
    "registry": "R2", "database": "R2", "db": "R2",
    "cache": "R1", "memo": "R1", "session": "R1", "sessions": "R1",
    "clock": "R1", "random": "R1", "randomness": "R1", "memory": "R1",
    "prompt": "R1", "prompts": "R1",
}

# Declared-effect names (plan.md section 22 categories) to levels.
_EFFECT_LEVELS = {
    "pure": "R0",
    "state": "R2", "state-read": "R2", "state-write": "R2",
    "queue": "R2", "queue-read": "R2", "queue-write": "R2",
    "filesystem": "R4", "filesystem-read": "R4", "filesystem-write": "R4",
    "network": "R4", "network-read": "R4", "network-write": "R4",
    "email": "R4", "payment": "R4", "process": "R4", "ffi": "R4",
    "clock": "R1", "random": "R1", "randomness": "R1",
}

# Some compounds mislead the R2/R3 substring scan: "user-interface" is display
# code (neither a user record nor an API contract) and "prompt-user" is
# console I/O (not user-record access), so mask them before those checks.
_MASKED_COMPOUNDS = (("user-interface", "ui"),
                     ("graphical-interface", "gui"),
                     ("prompt-user", "prompt"))

# --- Protected dispatch/kernel symbols (promotion-forbidden R6). ---
# Adversarial finding (documents/adversarial-report.md, kernel-mutation):
# a candidate targeting evo.dispatch:invoke-capability classified R2
# because only the R2 "invoke-capability" signal fired. Any mutation whose
# target lives under an evo.dispatch/evo.kernel-style package, or whose
# definition redefines a dispatch entrypoint symbol, is a trust-root
# mutation. Additive: appends R6 hits without changing existing signals.
_R6_REDEFINITION_HEADS = frozenset({"defun", "defmethod"})
_R6_SETF_FUNCTION_PLACES = frozenset(
    {"symbol-function", "fdefinition", "macro-function"})

# Recursion cap for the structural scans (s_expr caps parse-built input at
# depth 200; direct callers bypass the parser, so the scans enforce their
# own documented ValueError instead of escaping as RecursionError).
_MAX_WALK_DEPTH = 500
# Dangerous operators flagged even in value (non-head) position.
_R4_VALUE_ATOMS = _R4_HEADS | _R4_DYNAMIC_HEADS


def _normalize(name):
    """Lowercase a symbol/keyword body with separators unified to '-'."""
    text = name.lower()
    for char in "_/:.+*":
        text = text.replace(char, "-")
    return text


def _is_protected_dispatch_symbol(atom):
    """True when ATOM names a protected dispatch/kernel/entrypoint symbol.

    Protected means package-qualified under an evo.dispatch/evo.kernel-style
    package (the package part carries an ``evo`` token plus a ``dispatch``
    or ``kernel`` token, separator-insensitive), or naming the dispatch
    entrypoint (``invoke-capability``) qualified or not. Keywords are never
    protected (data, not code). Case-insensitive, like the Lisp reader.
    """
    if not isinstance(atom, str) or isinstance(atom, s_expr.SString):
        return False
    if not atom or atom.startswith(":"):
        return False
    lowered = atom.lower()
    if ":" in lowered:
        segments = [seg for seg in lowered.split(":") if seg]
        if segments:
            tokens = set(tok for tok in _normalize(segments[0]).split("-")
                         if tok)
            if "evo" in tokens and ("dispatch" in tokens
                                    or "kernel" in tokens):
                return True
    name_part = lowered.split(":")[-1]
    if not name_part:
        nonempty = [seg for seg in lowered.split(":") if seg]
        name_part = nonempty[-1] if nonempty else ""
    return "invoke-capability" in _normalize(name_part)


def _unwrap_redefined_name(node):
    """Return the symbol a defun/defmethod form defines, else None.

    Handles plain names, ``(setf name)`` method names, and one quote layer.
    Computed names (other lists, variables) yield None: only literal
    redefinitions of protected symbols escalate to R6.
    """
    if isinstance(node, str) and not isinstance(node, s_expr.SString) \
            and not node.startswith(":"):
        return node
    if isinstance(node, (list, tuple)) and node \
            and isinstance(node[0], str) \
            and not isinstance(node[0], s_expr.SString):
        head = node[0].split(":")[-1].lower()
        if head == "setf" and len(node) > 1 \
                and isinstance(node[1], str) \
                and not isinstance(node[1], s_expr.SString) \
                and not node[1].startswith(":"):
            return node[1]
        if head == "quote" and len(node) == 2 \
                and isinstance(node[1], str) \
                and not isinstance(node[1], s_expr.SString) \
                and not node[1].startswith(":"):
            return node[1]
    return None


def _unwrap_quoted_symbol(node):
    """Return a literal symbol under an optional single quote, else None."""
    if isinstance(node, str) and not isinstance(node, s_expr.SString) \
            and not node.startswith(":"):
        return node
    if isinstance(node, (list, tuple)) and len(node) == 2 \
            and isinstance(node[0], str) \
            and node[0].lower() == "quote" \
            and isinstance(node[1], str) \
            and not isinstance(node[1], s_expr.SString) \
            and not node[1].startswith(":"):
        return node[1]
    return None


def _scan_dispatch_redefinition(node, hits, depth=0):
    """Append R6 hits for redefinitions of protected dispatch symbols.

    Covers ``(defun NAME ...)`` / ``(defmethod NAME ...)`` and
    ``(setf``/``psetf`` of a function cell ``(symbol-function`` /
    ``fdefinition`` / ``macro-function``) ``NAME)`` ...)`` where NAME is a
    literal protected symbol. Quoted subtrees are skipped and string
    contents never inspected, matching :func:`_walk` hygiene.
    """
    if depth > _MAX_WALK_DEPTH:
        raise ValueError("unclassifiable: definition exceeds maximum scan "
                         "depth %d" % _MAX_WALK_DEPTH)
    if isinstance(node, s_expr.SString):
        return
    if isinstance(node, str):
        return
    if isinstance(node, (int, float, bool)) or node is None:
        return
    if not isinstance(node, (list, tuple)) or not node:
        return
    head = node[0]
    if isinstance(head, (list, tuple)):
        for element in node:
            _scan_dispatch_redefinition(element, hits, depth + 1)
        return
    if not isinstance(head, str) or isinstance(head, s_expr.SString):
        for element in node[1:]:
            _scan_dispatch_redefinition(element, hits, depth + 1)
        return
    if head.startswith(":"):
        for element in node:
            _scan_dispatch_redefinition(element, hits, depth + 1)
        return
    if head.lower() == "quote":
        return
    head_base = head.split(":")[-1].lower()
    if head_base in _R6_REDEFINITION_HEADS and len(node) > 1:
        name = _unwrap_redefined_name(node[1])
        if name is not None and _is_protected_dispatch_symbol(name):
            hits.append(("R6", "R6: definition redefines protected "
                               "dispatch/kernel symbol %r via %s "
                               "(promotion-forbidden)" % (name, head_base)))
    elif head_base in ("setf", "psetf") and len(node) > 1:
        place = node[1]
        if isinstance(place, (list, tuple)) and place \
                and isinstance(place[0], str) \
                and not isinstance(place[0], s_expr.SString):
            place_head = place[0].split(":")[-1].lower()
            if place_head in _R6_SETF_FUNCTION_PLACES and len(place) > 1:
                name = _unwrap_quoted_symbol(place[1])
                if name is not None and _is_protected_dispatch_symbol(name):
                    hits.append(("R6", "R6: definition redefines protected "
                                       "dispatch/kernel symbol %r via setf "
                                       "of %s (promotion-forbidden)"
                                 % (name, place_head)))
    for element in node[1:]:
        _scan_dispatch_redefinition(element, hits, depth + 1)


def _scan_atom(atom, is_head, hits, origin):
    """Record (level, reason) hits for one symbol/keyword occurrence."""
    is_keyword = atom.startswith(":")
    body = atom[1:] if is_keyword else atom
    kind = ("target" if origin == "target"
            else "keyword" if is_keyword
            else "head" if is_head else "symbol")
    if not body:
        return
    if not is_keyword and len(atom) > 1 and atom.startswith("*") \
            and atom.endswith("*"):
        hits.append(("R2", "R2: %s symbol %r is a global special "
                           "(shared state)" % (origin, atom)))
    norm = _normalize(body)
    base = body.split(":")[-1].lower()
    if is_head:
        if base in _EXEMPT_HEADS:
            return
        if base in _R4_DYNAMIC_HEADS:
            hits.append(("R4", "R4: %s head (%s ...) is dynamically "
                               "dispatched/introspective and statically "
                               "unclassifiable (conservative)"
                         % (origin, base)))
        if base in _R4_HEADS:
            hits.append(("R4", "R4: %s head (%s ...) signals external "
                               "effect" % (origin, base)))
        if base in _R3_HEADS:
            hits.append(("R3", "R3: %s head (%s ...) signals "
                               "structural/interface change" % (origin, base)))
        if base in _R2_HEADS:
            hits.append(("R2", "R2: %s head (%s ...) signals shared "
                               "canonical state" % (origin, base)))
        if base in _R1_HEADS:
            hits.append(("R1", "R1: %s head (%s ...) signals local state "
                               "operation" % (origin, base)))
    if not is_head and not is_keyword and base in _R4_VALUE_ATOMS:
        hits.append(("R4", "R4: %s symbol %r in value position names an "
                           "external-effect or dynamically-dispatched "
                           "operator (conservative)" % (origin, atom)))
    if is_keyword and body.lower() in _KEYWORD_LEVELS:
        level = _KEYWORD_LEVELS[body.lower()]
        hits.append((level, "%s: %s keyword %r declares %s"
                     % (level, origin, atom, _FAMILY[level])))
    masked = norm
    for masked_from, masked_to in _MASKED_COMPOUNDS:
        masked = masked.replace(masked_from, masked_to)
    for level, needles in _SUBSTR_BY_LEVEL:
        # R2/R3 match against UI-masked text so "user-interface" (display
        # code) is neither a user record nor an API contract.
        haystack = masked if level in ("R2", "R3") else norm
        for needle in sorted(needles):
            if needle in haystack:
                hits.append((level, "%s: %s %s %r matches %s signal %r"
                             % (level, origin, kind, atom,
                                _FAMILY[level], needle)))
    tokens = {token for token in norm.split("-") if token}
    for level, allowed in _TOKENS_BY_LEVEL:
        for token in sorted(tokens & allowed):
            hits.append((level, "%s: %s %s %r matches %s token %r"
                         % (level, origin, kind, atom,
                            _FAMILY[level], token)))


def _walk(node, hits, origin="definition", depth=0):
    """Recursively scan one parsed definition node for risk signals."""
    if depth > _MAX_WALK_DEPTH:
        raise ValueError("unclassifiable: definition exceeds maximum scan "
                         "depth %d" % _MAX_WALK_DEPTH)
    if isinstance(node, s_expr.SString):
        return  # string literal contents are data, never scanned
    if isinstance(node, str):
        _scan_atom(node, False, hits, origin)
        return
    if isinstance(node, (int, float, bool)) or node is None:
        return
    if isinstance(node, (list, tuple)):
        if not node:
            return
        head = node[0]
        if isinstance(head, (list, tuple)):
            for element in node:
                _walk(element, hits, origin, depth + 1)
            return
        if not isinstance(head, str) or isinstance(head, s_expr.SString):
            hits.append(("R4", "R4: %s form headed by %r is statically "
                               "unclassifiable (conservative)"
                         % (origin, head)))
            for element in node[1:]:
                _walk(element, hits, origin, depth + 1)
            return
        if head.startswith(":"):
            # Keyword-headed (:key ...) metadata/data: scan contents as data.
            for element in node:
                _walk(element, hits, origin, depth + 1)
            return
        if head.lower() == "quote":
            return  # quoted data is inert
        _scan_atom(head, True, hits, origin)
        head_base = head.split(":")[-1].lower()
        if head_base in ("setf", "psetf") and len(node) > 1:
            place = node[1]
            if isinstance(place, (list, tuple)) and place \
                    and isinstance(place[0], str):
                place_head = place[0].split(":")[-1].lower()
                if place_head in _R3_SETF_PLACES:
                    hits.append(("R3", "R3: %s redefines %s at runtime "
                                       "(structural/interface change)"
                                 % (origin, place_head)))
                elif place_head in _R2_SETF_PLACES:
                    hits.append(("R2", "R2: %s writes global %s cell "
                                       "(shared canonical state)"
                                 % (origin, place_head)))
        for element in node[1:]:
            _walk(element, hits, origin, depth + 1)
        return
    hits.append(("R4", "R4: %s node of type %s is statically "
                       "unclassifiable (conservative)"
                 % (origin, type(node).__name__)))


def _scan_effect_decl(value, hits, origin):
    """Classify declared effects from an :effects channel (extra/context)."""
    names = list(value) if isinstance(value, (list, tuple)) else [value]
    for entry in names:
        if isinstance(entry, (list, tuple)):
            entry = entry[0] if entry else None
        if isinstance(entry, s_expr.SString) or not isinstance(entry, str):
            hits.append(("R4", "R4: unclassifiable declared effect entry "
                               "%r in %s (conservative)" % (entry, origin)))
            continue
        name = entry.lower().lstrip(":").strip().replace("_", "-")
        if name in _EFFECT_LEVELS:
            level = _EFFECT_LEVELS[name]
            if level != "R0":
                hits.append((level, "%s: declared effect %r in %s"
                             % (level, entry, origin)))
        else:
            hits.append(("R4", "R4: unknown declared effect %r in %s is "
                               "statically unclassifiable (conservative)"
                         % (entry, origin)))


def classify(candidate_dict, context=None):
    """Classify a parsed candidate's mutation risk (plan.md section 7).

    :param candidate_dict: dict from :func:`s_expr.parse_candidate` with at
        least ``definition`` (non-empty list) and usually ``target``.
    :param context: optional advisory dict; only an ``effects``/``effect``
        entry (a name or list of names, e.g. ``["network-write"]``) is
        read. Anything else is ignored. ``None`` (or a non-dict) means no
        declared context effects.
    :returns: ``{"level", "reasons", "gates"}`` as documented above.
    :raises TypeError: candidate is not a dict (refused as unclassifiable).
    :raises ValueError: missing/empty/non-list ``definition`` or non-symbol
        ``target`` (refused as unclassifiable).
    """
    if not isinstance(candidate_dict, dict):
        raise TypeError("unclassifiable: candidate must be a dict, got %s"
                        % type(candidate_dict).__name__)
    if "definition" not in candidate_dict:
        raise ValueError("unclassifiable: candidate dict has no "
                         "'definition' key")
    definition = candidate_dict["definition"]
    if not isinstance(definition, (list, tuple)) or not definition:
        raise ValueError("unclassifiable: 'definition' must be a non-empty "
                         "list, got %r" % (definition,))
    hits = []
    target = candidate_dict.get("target")
    if target is not None:
        if not isinstance(target, str) or isinstance(target, s_expr.SString):
            raise ValueError("unclassifiable: 'target' must be a symbol, "
                             "got %r" % (target,))
        _scan_atom(target, False, hits, "target")
        target_norm = _normalize(target)
        if "prompt" in target_norm and "prompt-user" not in target_norm:
            # A target naming the prompter mutates the prompting surface
            # (but prompt-user itself is just console I/O).
            hits.append(("R5", "R5: mutation target %r names the prompting "
                               "surface (agent policy)" % (target,)))
        target_base = target.lstrip(":").split(":")[-1].lower()
        if target_base in _R4_DYNAMIC_HEADS:
            # Redefining a core dynamic form (eval/apply/load/...) changes
            # evaluation semantics globally: statically unclassifiable.
            hits.append(("R4", "R4: mutation target %r names a core dynamic "
                               "form (conservative)" % (target,)))
    _walk(definition, hits)
    # Adversarial hardening (additive): protected dispatch/kernel targets
    # and redefinitions are trust-root mutations (R6, promotion-forbidden).
    if target is not None and _is_protected_dispatch_symbol(target):
        hits.append(("R6", "R6: mutation target %r is under a protected "
                           "dispatch/kernel package or names the dispatch "
                           "entrypoint (promotion-forbidden)" % (target,)))
    _scan_dispatch_redefinition(definition, hits)
    extra = candidate_dict.get("extra")
    if isinstance(extra, dict):
        for key in (":effects", ":effect"):
            if key in extra:
                _scan_effect_decl(extra[key], hits, "extra[%s]" % key)
    if isinstance(context, dict):
        for key in ("effects", "effect", ":effects", ":effect"):
            if key in context:
                _scan_effect_decl(context[key], hits, "context[%s]" % key)
    if not hits:
        return {
            "level": "R0",
            "reasons": ["R0: pure computation (no state, structural, "
                        "effect, policy, or kernel signals)"],
            "gates": list(GATES["R0"]),
        }
    level = max((hit_level for hit_level, _ in hits),
                key=lambda name: _RANK[name])
    ordered = sorted(set(hits), key=lambda hit: (-_RANK[hit[0]], hit[1]))
    reasons = [reason for _, reason in ordered[:_MAX_REASONS]]
    if len(ordered) > _MAX_REASONS:
        reasons.append("(%d more signals omitted)"
                       % (len(ordered) - _MAX_REASONS))
    return {"level": level, "reasons": reasons, "gates": list(GATES[level])}
