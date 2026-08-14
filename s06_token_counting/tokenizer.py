"""Token counting with provider-specific strategies."""
from __future__ import annotations

import tiktoken

_OPENAI_ENCODER = tiktoken.get_encoding("cl100k_base")


def count_openai(messages: list[dict], model: str) -> int:
    """Use tiktoken cl100k_base. Returns prompt token count."""
    # Per OpenAI cookbook: ~4 tokens per message overhead + content tokens.
    n = 0
    for m in messages:
        n += 4
        content = m.get("content") or ""
        n += len(_OPENAI_ENCODER.encode(content))
    n += 2  # reply priming
    return n


def count_estimate(messages: list[dict]) -> int:
    """Char/4 fallback for non-OpenAI providers."""
    total = sum(len((m.get("content") or "")) for m in messages)
    return max(1, total // 4)


def count_prompt(messages: list[dict], model: str) -> int:
    if model.startswith(("gpt-", "o")):
        return count_openai(messages, model)
    return count_estimate(messages)