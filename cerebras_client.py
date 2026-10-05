"""Cerebras inference client built on the OpenAI SDK.

Covers plan.md Phase 0 task 8 (Cerebras adapter): connection handling,
structured chat-completion calls, and usage accounting. Model defaults to
``qwen-3.8-27b`` per the Cerebras model catalog.

Auth: reads ``CEREBRAS_API_KEY`` (preferred) or ``CEREBRAS`` from the
environment / project ``.env``. The key value is never printed.
"""

import os
import sys
import time

from dotenv import load_dotenv
from openai import OpenAI

import s_expr

load_dotenv()

BASE_URL = "https://api.cerebras.ai/v1"
DEFAULT_MODEL = "qwen-3.8-27b"

# Cerebras qwen-3.8-27b pricing in USD per million tokens.
COST_INPUT_USD_PER_MTOK = 0.99
COST_OUTPUT_USD_PER_MTOK = 1.49

_client = None


def get_api_key() -> str:
    key = os.environ.get("CEREBRAS_API_KEY") or os.environ.get("CEREBRAS")
    if not key:
        raise RuntimeError(
            "No Cerebras API key found: set CEREBRAS_API_KEY (or CEREBRAS) "
            "in the environment or .env"
        )
    return key


def get_client() -> OpenAI:
    """Shared OpenAI SDK client pointed at the Cerebras endpoint."""
    global _client
    if _client is None:
        _client = OpenAI(api_key=get_api_key(), base_url=BASE_URL)
    return _client


def compute_cost_usd(input_tokens, output_tokens) -> float | None:
    """Cost in USD for one call at qwen-3.8-27b pricing.

    Returns ``None`` when either token count is unknown (``None`` or
    non-numeric); otherwise the rounded cost (6 decimals).
    """
    if isinstance(input_tokens, bool) or isinstance(output_tokens, bool):
        return None
    if not isinstance(input_tokens, (int, float)) or not isinstance(
        output_tokens, (int, float)
    ):
        return None
    return round(
        (
            input_tokens * COST_INPUT_USD_PER_MTOK
            + output_tokens * COST_OUTPUT_USD_PER_MTOK
        )
        / 1_000_000,
        6,
    )


def aggregate_totals(results: list) -> dict:
    """Aggregate usage/cost totals over ``generate()`` result dicts.

    Missing/``None`` token or latency entries count as zero. Returns
    ``calls``, ``input_tokens``, ``output_tokens``, ``total_tokens``,
    ``latency_ms`` (rounded to 1 decimal), and ``cost_usd`` (rounded to
    6 decimals).
    """
    input_tokens = 0
    output_tokens = 0
    latency_ms = 0.0
    for result in results:
        if not isinstance(result, dict):
            continue
        inp = result.get("input_tokens")
        out = result.get("output_tokens")
        lat = result.get("latency_ms")
        if isinstance(inp, (int, float)) and not isinstance(inp, bool):
            input_tokens += inp
        if isinstance(out, (int, float)) and not isinstance(out, bool):
            output_tokens += out
        if isinstance(lat, (int, float)) and not isinstance(lat, bool):
            latency_ms += lat
    calls = sum(1 for r in results if isinstance(r, dict))
    return {
        "calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "latency_ms": round(latency_ms, 1),
        "cost_usd": round(
            (
                input_tokens * COST_INPUT_USD_PER_MTOK
                + output_tokens * COST_OUTPUT_USD_PER_MTOK
            )
            / 1_000_000,
            6,
        ),
    }


def generate(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    system: str | None = None,
    max_tokens: int = 1000,
    temperature: float = 0.2,
    reasoning_effort: str | None = None,
    timeout: float = 60.0,
) -> dict:
    """One chat-completion call. Returns text plus usage/latency accounting.

    ``reasoning_effort``: the model defaults to high reasoning; pass "none"
    to disable it for cheap deterministic calls. ``None`` leaves the API
    default in place.

    The result also carries ``request_id`` (API completion id, may be
    ``None``) and ``cost_usd`` (qwen-3.8-27b pricing, ``None`` when usage
    is unknown).
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "timeout": timeout,
    }
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort

    start = time.monotonic()
    completion = get_client().chat.completions.create(**kwargs)
    latency_ms = (time.monotonic() - start) * 1000

    choice = completion.choices[0]
    usage = completion.usage
    input_tokens = usage.prompt_tokens if usage else None
    output_tokens = usage.completion_tokens if usage else None
    return {
        "text": choice.message.content,
        "finish_reason": choice.finish_reason,
        "model": completion.model,
        "latency_ms": round(latency_ms, 1),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "request_id": getattr(completion, "id", None),
        "cost_usd": compute_cost_usd(input_tokens, output_tokens),
    }


class MalformedCandidateError(s_expr.SExprError):
    """Model output failed ``(candidate ...)`` validation.

    Raised by :func:`validate_model_output` BEFORE the output could reach
    any worker or evaluator. The ``failure`` attribute carries structured
    failure info (error type, message, position, offending key, and a
    short raw excerpt) for logging and repair loops.
    """

    def __init__(self, message, *, failure):
        super().__init__(message)
        self.failure = failure


CANDIDATE_SYSTEM_PROMPT = """\
You emit exactly one Common Lisp S-expression and nothing else: no prose, \
no markdown fences, no commentary outside the form. The form is a candidate \
definition with this grammar:

(candidate
  (:target <symbol>)        ; required: name of the capability to replace
  (:parent <integer>)       ; required: parent capability version number
  (:reason <symbol>)        ; optional: one symbol summarizing the change
  (:claims (<name> ...) ...) ; optional: claims about the candidate
  (:definition (<lambda> ...))) ; required: one non-empty replacement form

Rules: exactly one top-level (candidate ...) form; balanced parentheses; \
double-quoted strings only; every entry is a (:key ...) list; :target is a \
single bare symbol; :parent is a single integer; :definition is a single \
non-empty list. Anything else is rejected before execution."""


def validate_model_output(text) -> dict:
    """Validate raw model text as a single ``(candidate ...)`` form.

    Returns the parsed candidate dict (``target``/``parent``/``definition``
    plus optional ``reason``/``claims``/``extra``). Raises
    :class:`MalformedCandidateError` — carrying structured failure info
    in its ``failure`` attribute — on ANY malformed input, so callers can
    reject it before execution.
    """
    try:
        return s_expr.parse_candidate(text)
    except s_expr.SExprError as exc:
        excerpt = text[:500] if isinstance(text, str) else repr(text)[:500]
        failure = {
            "ok": False,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "position": getattr(exc, "position", None),
            "line": getattr(exc, "line", None),
            "column": getattr(exc, "column", None),
            "key": getattr(exc, "key", None),
            "raw_excerpt": excerpt,
        }
        raise MalformedCandidateError(str(exc), failure=failure) from exc


def generate_candidate(
    task_context: str,
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 800,
    temperature: float = 0.2,
    reasoning_effort: str | None = "none",
    timeout: float = 60.0,
) -> dict:
    """One structured candidate call: prompt for S-expression, validate it.

    Returns a dict with the usual usage accounting (``model``,
    ``finish_reason``, ``latency_ms``, ``input_tokens``,
    ``output_tokens``, ``request_id``, ``cost_usd``) plus ``raw``
    (verbatim model text) and either ``{"ok": True, "candidate": {...},
    "error": None}`` or, when the output is malformed, ``{"ok": False,
    "candidate": None, "error": {...structured failure info...}}``.
    Malformed output is reported, never raised and never executed;
    transport/API errors still raise.
    """
    result = generate(
        task_context,
        model=model,
        system=CANDIDATE_SYSTEM_PROMPT,
        max_tokens=max_tokens,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )
    raw = result.get("text") or ""
    cost_usd = result.get("cost_usd")
    if cost_usd is None:
        cost_usd = compute_cost_usd(
            result.get("input_tokens"), result.get("output_tokens")
        )
    outcome = {
        "model": result.get("model"),
        "finish_reason": result.get("finish_reason"),
        "latency_ms": result.get("latency_ms"),
        "input_tokens": result.get("input_tokens"),
        "output_tokens": result.get("output_tokens"),
        "request_id": result.get("request_id"),
        "cost_usd": cost_usd,
        "raw": raw,
    }
    try:
        candidate = validate_model_output(raw)
    except MalformedCandidateError as exc:
        outcome.update({"ok": False, "candidate": None, "error": exc.failure})
    else:
        outcome.update({"ok": True, "candidate": candidate, "error": None})
    return outcome


if __name__ == "__main__":
    # Smoke test: `uv run python cerebras_client.py [prompt]`
    result = generate(
        sys.argv[1] if len(sys.argv) > 1 else "Reply with exactly: OK",
        max_tokens=50,
        reasoning_effort="none",
    )
    print(f"model:        {result['model']}")
    print(f"finish:       {result['finish_reason']}")
    print(f"latency_ms:   {result['latency_ms']}")
    print(f"tokens in/out:{result['input_tokens']}/{result['output_tokens']}")
    print(f"request_id:   {result.get('request_id')}")
    print(f"cost_usd:     {result.get('cost_usd')}")
    print(f"text:         {result['text']}")
