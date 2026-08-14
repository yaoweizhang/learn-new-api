"""/v1/chat/completions endpoint.

Composed of: auth → rate → token count → pre-consume → adapter → upstream
→ settle → log → return.

Note: auth uses the typed-parameter pattern (`p: Principal = Depends(...)`)
not `dependencies=[Depends(...)]`, because the latter does not inject the
dependency result into the handler signature.
"""
from __future__ import annotations

import json
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
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

    payload = req.model_dump(exclude_none=True)
    payload["_api_key"] = os.getenv("UPSTREAM_OPENAI_KEY", "") if provider.name == "openai" else ""
    url, headers, upstream_body = provider.to_upstream(payload)
    body_bytes = marshal(upstream_body)
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

    translated = provider.from_upstream(json.loads(r.text))
    actual = settle(p.user_id, estimate, translated.get("usage", {}))
    enqueue_log({
        "user_id": p.user_id, "model": req.model, "status": r.status_code,
        "usage": translated.get("usage", {}), "quota_charged": actual,
    })
    translated["quota_charged"] = actual
    return JSONResponse(translated)
