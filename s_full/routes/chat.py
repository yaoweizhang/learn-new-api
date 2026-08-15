"""/v1/chat/completions endpoint.

Composed of: auth → rate → token count → pre-consume → adapter → upstream
→ settle → log → return.

Note: auth uses the typed-parameter pattern (`p: Principal = Depends(...)`)
not `dependencies=[Depends(...)]`, because the latter does not inject the
dependency result into the handler signature.

When `stream=true`, the route returns a `StreamingResponse` that relays
upstream SSE bytes verbatim (no per-provider translation). Quota is
pre-consumed up front, then settled on stream completion using the LAST
`data:` chunk's `usage.completion_tokens` when the upstream reports it.
On transport error mid-stream, the full estimate is refunded.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from common.json import marshal
from s_full.adapters.claude import ClaudeProvider
from s_full.adapters.gemini import GeminiProvider
from s_full.adapters.openai import OpenAIProvider
from s_full.middleware.auth import Principal, require_api_key
from s_full.models.log import enqueue_log
from s_full.services.billing import pre_consume, settle
from s_full.services.rate_limit import take

router = APIRouter()

_PROVIDERS = {
    "openai": OpenAIProvider(),
    "claude": ClaudeProvider(),
    "gemini": GeminiProvider(),
}

# Per-provider env var for the upstream API key. Real new-api stores this
# on the Channel row (multi-key rotation); s_full keeps the simpler env
# lookup that matches the rest of the tutorial.
_API_KEY_ENV = {
    "openai": "UPSTREAM_OPENAI_KEY",
    "claude": "UPSTREAM_CLAUDE_KEY",
    "gemini": "UPSTREAM_GEMINI_KEY",
}


def _pick(model: str):
    if model.startswith(("gpt-", "o")):
        return _PROVIDERS["openai"]
    if model.startswith("claude-"):
        return _PROVIDERS["claude"]
    if model.startswith("gemini-"):
        return _PROVIDERS["gemini"]
    raise ValueError(model)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: int | None = None
    temperature: float | None = None
    stream: bool = False


def _parse_last_data_chunk(buffer: bytes) -> dict | None:
    """Best-effort parse of the LAST `data: {...}` SSE line in `buffer`.

    Many upstreams omit per-chunk usage; in that case return None so the
    caller falls back to settle-to-estimate.
    """
    text = buffer.decode("utf-8", errors="replace")
    last_data = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:") and line != "data: [DONE]":
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                last_data = json.loads(payload)
            except json.JSONDecodeError:
                continue
    return last_data


async def _relay_stream(
    url: str,
    upstream_headers: dict,
    body_bytes: bytes,
    provider,
    principal: Principal,
    estimate: int,
    model: str,
) -> AsyncIterator[bytes]:
    """Open an upstream SSE stream and yield bytes verbatim.

    On normal completion: parse the LAST `data:` chunk for usage if
    available, then call `settle(...)` (which refunds any difference
    between the pre-consume and actual). Finally enqueue a log entry.

    On abnormal termination — transport error mid-stream OR client
    disconnect (which raises `GeneratorExit`, a `BaseException` not
    caught by `except Exception`) — refund the full estimate so the
    user isn't charged for a response they never received. The state
    flag guards against double-refund if the inner try's `except`
    already ran before the outer `BaseException` handler executes.
    """
    from s_full.services.quota import refund

    timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
    full_buf = bytearray()
    state = "open"  # transitions to "settled" on success, "refunded" on abort
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", url, content=body_bytes, headers=upstream_headers
            ) as upstream:
                async for chunk in upstream.aiter_bytes():
                    full_buf.extend(chunk)
                    yield chunk
        # Stream completed normally — settle and log.
        last_data_payload = _parse_last_data_chunk(bytes(full_buf))
        usage = (last_data_payload.get("usage") if last_data_payload else None) or {}
        actual = settle(principal.user_id, estimate, usage)
        enqueue_log({
            "user_id": principal.user_id,
            "model": model,
            "status": 200,
            "stream": True,
            "usage": usage,
            "quota_charged": actual,
        })
        state = "settled"
    except BaseException:
        if state == "open":
            refund(principal.user_id, estimate)
            state = "refunded"
        raise


@router.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    p: Principal = Depends(require_api_key),
):
    if not take(p.user_id):
        raise HTTPException(429, "rate limited")
    try:
        estimate = pre_consume(p.user_id, req.model, [m.model_dump() for m in req.messages], req.max_tokens)
    except PermissionError:
        raise HTTPException(402, "insufficient quota")

    try:
        provider = _pick(req.model)
    except ValueError as exc:
        from s_full.services.quota import refund
        refund(p.user_id, estimate)
        raise HTTPException(400, str(exc))

    if req.stream and not provider.supports_streaming:
        from s_full.services.quota import refund
        refund(p.user_id, estimate)
        raise HTTPException(400, "streaming not supported for this provider")

    payload = req.model_dump(exclude_none=True)
    payload["_api_key"] = os.getenv(_API_KEY_ENV.get(provider.name, ""), "")
    url, headers, upstream_body = provider.to_upstream(payload)
    body_bytes = marshal(upstream_body)

    if req.stream:
        return StreamingResponse(
            _relay_stream(url, headers, body_bytes, provider, p, estimate, req.model),
            media_type="text/event-stream",
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(url, content=body_bytes, headers=headers)
        except httpx.HTTPError:
            from s_full.services.quota import refund
            refund(p.user_id, estimate)
            raise HTTPException(502, "upstream error")

    if r.status_code >= 400:
        from s_full.services.quota import refund
        refund(p.user_id, estimate)
        raise HTTPException(r.status_code, r.text)

    try:
        translated = provider.from_upstream(json.loads(r.text))
    except (ValueError, json.JSONDecodeError):
        from s_full.services.quota import refund
        refund(p.user_id, estimate)
        raise HTTPException(502, "upstream returned malformed body")
    actual = settle(p.user_id, estimate, translated.get("usage", {}))
    enqueue_log({
        "user_id": p.user_id, "model": req.model, "status": r.status_code,
        "usage": translated.get("usage", {}), "quota_charged": actual,
    })
    translated["quota_charged"] = actual
    return JSONResponse(translated)