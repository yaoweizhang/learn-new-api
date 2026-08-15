"""Billing service: top_up + pre_consume + settle.

Combines token counting (s06) + quota deduction (s07) so the chat route
stays thin. top_up(email, amount) resolves email->user_id internally.
"""
from __future__ import annotations

import tiktoken

from s_full.models.user import find_by_email
from s_full.services import quota

_OPENAI_ENCODER = tiktoken.get_encoding("cl100k_base")
RATE_PER_TOKEN = 1


def _count_openai(messages: list[dict], model: str) -> int:
    n = 0
    for m in messages:
        n += 4
        content = m.get("content") or ""
        n += len(_OPENAI_ENCODER.encode(content))
    n += 2
    return n


def _count_estimate(messages: list[dict]) -> int:
    total = sum(len((m.get("content") or "")) for m in messages)
    return max(1, total // 4)


def _count_prompt(messages: list[dict], model: str) -> int:
    if model.startswith(("gpt-", "o")):
        return _count_openai(messages, model)
    return _count_estimate(messages)


def top_up(email: str, amount: int) -> None:
    """Credit `amount` quota to the user identified by email."""
    u = find_by_email(email)
    if u is None:
        raise ValueError(f"unknown user: {email}")
    uid = int(u["id"])
    cur = quota.get_balance(uid)
    quota.set_balance(uid, cur + amount)


def pre_consume(user_id: int, model: str, messages: list[dict], max_tokens: int | None) -> int:
    """Estimate cost and deduct. Returns the estimate. Raises HTTPException-equivalent
    via `deduct` returning False; callers translate."""
    prompt_tokens = _count_prompt(messages, model)
    expected = max_tokens or 256
    estimate = (prompt_tokens + expected) * RATE_PER_TOKEN
    if not quota.deduct(user_id, estimate):
        # Caller catches and returns 402; keeping the logic pure here.
        raise PermissionError("insufficient quota")
    return estimate


def settle(user_id: int, pre_deducted: int, usage: dict) -> int:
    """Apply the upstream's reported usage to the pre-consume.

    Uses upstream's `prompt_tokens` and `completion_tokens` directly (with a
    safe fallback to pre_deducted when the upstream omits usage, e.g. SSE
    streams that never emit a final usage chunk — that's the common case so
    the bidir path in `quota.settle` only fires when the upstream actually
    reports).

    Previously this floored `pt` at `pre_deducted`, which coupled badly
    with one-directional `quota.settle`: users were always charged the full
    estimate because `actual` was never smaller than `pre_deducted`. That
    masked the missing overage path.
    """
    pt = usage.get("prompt_tokens")
    ct = usage.get("completion_tokens")
    if pt is None or ct is None:
        # If either is missing, treat the usage report as incomplete and
        # keep the pre-consume as the bill — don't refund the gap.
        return pre_deducted
    actual = ((pt or 0) + (ct or 0)) * RATE_PER_TOKEN
    return quota.settle(user_id, pre_deducted, actual)
