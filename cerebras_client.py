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

load_dotenv()

BASE_URL = "https://api.cerebras.ai/v1"
DEFAULT_MODEL = "qwen-3.8-27b"

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
    return {
        "text": choice.message.content,
        "finish_reason": choice.finish_reason,
        "model": completion.model,
        "latency_ms": round(latency_ms, 1),
        "input_tokens": usage.prompt_tokens if usage else None,
        "output_tokens": usage.completion_tokens if usage else None,
    }


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
    print(f"text:         {result['text']}")
