"""Tokenizer/parser/validator for the ``(candidate ...)`` S-expression subset.

Implements the model-output grammar from plan.md section 16: the LLM MUST
emit a single ``(candidate ...)`` form and invalid syntax MUST be rejected
before anything is executed. This module is the offline, pure-stdlib half
of that gate; see :func:`cerebras_client.validate_model_output` for the
live entry point.

Representations
---------------
* lists -> Python ``list``
* double-quoted strings -> :class:`SString` (a ``str`` subclass, so it
  stays distinct from symbols while comparing equal to plain strings)
* symbols and keywords (``:name``) -> plain ``str``
* integers / floats -> ``int`` / ``float``

``t``/``nil`` are preserved as symbols; no evaluation is performed here.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_INPUT_CHARS = 100_000
MAX_DEPTH = 200

_REQUIRED_KEYS = (":target", ":parent", ":definition")


class SExprError(ValueError):
    """Base class for all S-expression parse/validation failures."""


class SExprSyntaxError(SExprError):
    """Tokenizing/parsing failed (unbalanced, truncated, garbage, oversized)."""

    def __init__(self, message, *, position=None, line=None, column=None):
        super().__init__(message)
        self.position = position
        self.line = line
        self.column = column


class SExprSchemaError(SExprError):
    """A well-formed form is not a valid ``(candidate ...)`` per the grammar."""

    def __init__(self, message, *, key=None):
        super().__init__(message)
        self.key = key


class SString(str):
    """A double-quoted string literal (distinct from a symbol)."""


@dataclass(frozen=True)
class _Token:
    kind: str  # "lparen" | "rparen" | "atom"
    value: object
    position: int


def _line_col(text, position):
    line = text.count("\n", 0, position) + 1
    nl = text.rfind("\n", 0, position)
    return line, position - nl


def _fail(message, text, position):
    line, column = _line_col(text, position)
    raise SExprSyntaxError(
        f"{message} (line {line}, column {column})",
        position=position,
        line=line,
        column=column,
    )


def _classify_atom(word, text, position):
    if word.startswith(":"):
        if len(word) == 1:
            _fail("empty keyword ':'", text, position)
        return word
    try:
        return int(word)
    except ValueError:
        pass
    try:
        # Reject nan/inf spellings as numbers; they stay symbols.
        lowered = word.lower()
        if lowered in ("nan", "+nan", "-nan", "inf", "+inf", "-inf",
                       "infinity", "+infinity", "-infinity"):
            return word
        return float(word)
    except ValueError:
        return word


def tokenize(text):
    """Split S-expression source into tokens.

    Handles ``;`` line comments, double-quoted strings with backslash
    escapes, and comma-as-whitespace. Raises :class:`SExprSyntaxError`
    on unterminated strings or unexpected characters.
    """
    if not isinstance(text, str):
        raise SExprSyntaxError(f"expected str, got {type(text).__name__}")
    if len(text) > MAX_INPUT_CHARS:
        raise SExprSyntaxError(
            f"input too large: {len(text)} chars exceeds cap of "
            f"{MAX_INPUT_CHARS}"
        )
    tokens = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n,":
            i += 1
        elif ch == ";":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "(":
            tokens.append(_Token("lparen", ch, i))
            i += 1
        elif ch == ")":
            tokens.append(_Token("rparen", ch, i))
            i += 1
        elif ch == '"':
            start = i
            i += 1
            buf = []
            closed = False
            while i < n:
                c = text[i]
                if c == "\\":
                    if i + 1 >= n:
                        break
                    esc = text[i + 1]
                    buf.append({
                        "n": "\n", "t": "\t", "r": "\r",
                        '"': '"', "\\": "\\",
                    }.get(esc, esc))
                    i += 2
                elif c == '"':
                    closed = True
                    i += 1
                    break
                else:
                    buf.append(c)
                    i += 1
            if not closed:
                _fail("unterminated string literal", text, start)
            tokens.append(_Token("atom", SString("".join(buf)), start))
        elif ch == "#" and text[i + 1:i + 2] == "\\" and i + 2 < n:
            # Character literal: #\' #\( #\) #\; #\" #\, are one token each,
            # as are named ones such as #\Space. The character after the
            # backslash is always taken, whatever it is.
            start = i
            i += 3
            while i < n and text[i] not in " \t\r\n,();\"'":
                i += 1
            tokens.append(
                _Token("atom", _classify_atom(text[start:i], text, start), start)
            )
        elif ch == "'":
            # Quote sugar: 'x -> (quote x)
            tokens.append(_Token("quote", "'", i))
            i += 1
        else:
            start = i
            while i < n and text[i] not in " \t\r\n,();\"'":
                i += 1
            word = text[start:i]
            tokens.append(
                _Token("atom", _classify_atom(word, text, start), start)
            )
    return tokens


def _parse_tokens(tokens, text):
    root = []  # virtual top level holding the top-level form(s)
    stack = [root]
    auto = [False]  # per-frame: auto-close after one value (quote sugar)
    openers = [None]  # opener positions for "unbalanced" diagnostics

    def close_completed_quotes():
        while len(stack) > 1 and auto[-1]:
            stack.pop()
            auto.pop()
            openers.pop()

    for tok in tokens:
        if tok.kind == "lparen":
            if len(stack) - 1 >= MAX_DEPTH:
                _fail(
                    f"nesting exceeds max depth of {MAX_DEPTH}",
                    text,
                    tok.position,
                )
            node = []
            stack[-1].append(node)
            stack.append(node)
            auto.append(False)
            openers.append(tok.position)
        elif tok.kind == "rparen":
            if len(stack) == 1:
                _fail("unbalanced delimiter: unexpected ')'", text, tok.position)
            if auto[-1]:
                _fail(
                    "quote (') without a following form",
                    text,
                    openers[-1],
                )
            stack.pop()
            auto.pop()
            openers.pop()
            close_completed_quotes()
        elif tok.kind == "quote":
            # Quote sugar: 'x -> (quote x). The frame auto-closes once
            # the next complete value lands inside it.
            if len(stack) - 1 >= MAX_DEPTH:
                _fail(
                    f"nesting exceeds max depth of {MAX_DEPTH}",
                    text,
                    tok.position,
                )
            marker = ["quote"]
            stack[-1].append(marker)
            stack.append(marker)
            auto.append(True)
            openers.append(tok.position)
        else:
            stack[-1].append(tok.value)
            close_completed_quotes()
    if len(stack) > 1 and auto[-1]:
        _fail("dangling quote (') with no following form", text, openers[-1])
    if len(stack) > 1:
        _fail(
            "unbalanced delimiter: unterminated '('",
            text,
            openers[-1] if openers[-1] is not None else 0,
        )
    return root


def parse(text):
    """Parse exactly one top-level S-expression form.

    Raises :class:`SExprSyntaxError` on empty input, unbalanced or
    truncated delimiters, unterminated strings, oversized input, or any
    trailing content after the first form.
    """
    forms = _parse_tokens(tokenize(text), text)
    if not forms:
        raise SExprSyntaxError("empty input: no S-expression found")
    if len(forms) > 1:
        _fail(
            "trailing content after first S-expression: "
            "expected a single form",
            text,
            _find_second_form_pos(text),
        )
    return forms[0]


def _find_second_form_pos(text):
    # Best-effort position of trailing content for diagnostics: the
    # caller already knows the input has >1 form; point at the second
    # non-trivia character run by re-scanning depth.
    depth = 0
    in_string = False
    escaped = False
    in_comment = False
    seen_first = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_comment:
            if ch == "\n":
                in_comment = False
            i += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == ";":
            in_comment = True
        elif ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                seen_first = True
        elif seen_first and depth == 0 and ch not in " \t\r\n,":
            return i
        i += 1
    return 0


def _is_symbol(value):
    return (
        isinstance(value, str)
        and not isinstance(value, SString)
        and not value.startswith(":")
    )


def parse_candidate(text):
    """Parse and schema-check one ``(candidate ...)`` form.

    Returns a dict with ``target`` (symbol), ``parent`` (int),
    ``definition`` (non-empty list), plus optional ``reason``/``claims``
    and any unknown keys under ``extra``.

    Raises :class:`SExprSyntaxError` for malformed S-expressions and
    :class:`SExprSchemaError` for well-formed input that violates the
    candidate schema (wrong head, missing keys, bad value shapes).
    """
    form = parse(text)
    if not isinstance(form, list) or not form:
        raise SExprSchemaError("candidate must be a non-empty list")
    if not _is_symbol(form[0]) or form[0] != "candidate":
        raise SExprSchemaError(
            f"head must be the symbol 'candidate', got {form[0]!r}"
        )

    fields = {}
    for entry in form[1:]:
        if not isinstance(entry, list) or not entry:
            raise SExprSchemaError(
                f"candidate entry must be a (:key ...) list, got {entry!r}"
            )
        key = entry[0]
        if (
            not isinstance(key, str)
            or isinstance(key, SString)
            or not key.startswith(":")
        ):
            raise SExprSchemaError(
                f"candidate entry must start with a keyword, got {key!r}"
            )
        if key in fields:
            raise SExprSchemaError(
                f"duplicate candidate key {key}", key=key
            )
        fields[key] = entry[1:]

    missing = [k for k in _REQUIRED_KEYS if k not in fields]
    if missing:
        raise SExprSchemaError(
            f"candidate missing required key(s): {', '.join(missing)}",
            key=missing[0],
        )

    target_vals = fields[":target"]
    if len(target_vals) != 1 or not _is_symbol(target_vals[0]):
        raise SExprSchemaError(
            f":target must be exactly one symbol, got {target_vals!r}",
            key=":target",
        )
    parent_vals = fields[":parent"]
    if (
        len(parent_vals) != 1
        or not isinstance(parent_vals[0], int)
        or isinstance(parent_vals[0], bool)
    ):
        raise SExprSchemaError(
            f":parent must be exactly one integer, got {parent_vals!r}",
            key=":parent",
        )
    defn_vals = fields[":definition"]
    if (
        len(defn_vals) != 1
        or not isinstance(defn_vals[0], list)
        or not defn_vals[0]
    ):
        raise SExprSchemaError(
            ":definition must be exactly one non-empty list, "
            f"got {defn_vals!r}",
            key=":definition",
        )

    result = {
        "target": target_vals[0],
        "parent": parent_vals[0],
        "definition": defn_vals[0],
        "reason": None,
        "claims": [],
        "extra": {},
    }
    if ":reason" in fields:
        reason_vals = fields[":reason"]
        if len(reason_vals) != 1 or not isinstance(reason_vals[0], str):
            raise SExprSchemaError(
                f":reason must be exactly one symbol or string, "
                f"got {reason_vals!r}",
                key=":reason",
            )
        result["reason"] = reason_vals[0]
    if ":claims" in fields:
        for claim in fields[":claims"]:
            if not isinstance(claim, list) or not claim:
                raise SExprSchemaError(
                    f"each :claims entry must be a non-empty list, "
                    f"got {claim!r}",
                    key=":claims",
                )
        result["claims"] = list(fields[":claims"])
    for key, vals in fields.items():
        if key not in _REQUIRED_KEYS and key not in (":reason", ":claims"):
            result["extra"][key] = list(vals)
    return result
