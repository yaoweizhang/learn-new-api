"""s13: retry transient upstream errors + fall back to next channel.

tenacity: 3 attempts, exponential backoff (0.2s, 0.4s, 0.8s).
If all attempts on the primary channel fail, mark it unhealthy; future calls
pick the next-priority channel.

Composition (mirrors `s_full/routes/chat.py`):
    auth -> rate -> pre-consume -> adapter -> upstream -> settle -> return

Use the typed-parameter pattern (`p: Principal = Depends(require_api_key)`),
NOT `dependencies=[Depends(...)]`, because the latter does not inject the
dependency result into the handler signature.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from common.json import marshal
from s04_multi_provider.adapters import pick_provider
from s05_api_key_auth.storage import Principal, is_blocked, lookup_key
from s06_token_counting.tokenizer import count_prompt
from s07_pre_consume_settle.quota import deduct, refund, settle
from s08_rate_limiting.bucket import take
from s10_channel_management import channels as ch_mod
from s12_caching.code import app as s12_app

app = FastAPI(title="learn-new-api s13")

RATE_PER_TOKEN = int(os.getenv("RATE_PER_TOKEN", "1"))

TRANSIENT = (502, 503, 504, 429)


def _key_for(provider_name: str) -> str:
    env = {
        "openai": "UPSTREAM_OPENAI_KEY",
        "claude": "UPSTREAM_CLAUDE_KEY",
        "gemini": "UPSTREAM_GEMINI_KEY",
    }.get(provider_name, "")
    return os.getenv(env, "") if env else ""


def require_api_key(request: Request) -> Principal:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    key = auth.removeprefix("Bearer ").strip()
    if is_blocked(key):
        raise HTTPException(status_code=401, detail="blocked")
    p = lookup_key(key)
    if p is None:
        raise HTTPException(status_code=401, detail="unknown")
    return p


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.2, min=0.2, max=2.0),
    retry=retry_if_exception_type(httpx.HTTPError),
    reraise=True,
)
async def _call_with_retry(client: httpx.AsyncClient, url: str, headers: dict, body: bytes) -> httpx.Response:
    r = await client.post(url, content=body, headers=headers)
    if r.status_code in TRANSIENT:
        # Convert a non-2xx into an exception so tenacity retries on it.
        # (httpx only raises on transport errors; status codes come back normally.)
        raise httpx.HTTPError(f"transient {r.status_code}")
    return r


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: int | None = None


@app.post("/v1/chat/completions")
async def chat_with_retry(
    req: ChatCompletionRequest,
    p: Principal = Depends(require_api_key),
):
    # Rate limit before any expensive work.
    if not take(p.user_id):
        raise HTTPException(status_code=429, detail="rate limited")

    try:
        provider_name = pick_provider(req.model).name
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Pre-consume: estimate tokens and deduct quota up front.
    prompt_tokens = count_prompt([m.model_dump() for m in req.messages], req.model)
    expected = req.max_tokens or 256
    estimate = (prompt_tokens + expected) * RATE_PER_TOKEN
    if not deduct(p.user_id, estimate):
        raise HTTPException(status_code=402, detail="insufficient quota")

    candidates = ch_mod.list_channels()
    if not candidates:
        refund(p.user_id, estimate)
        raise HTTPException(status_code=503, detail="no channels configured")

    payload = req.model_dump(exclude_none=True)
    payload["_api_key"] = _key_for(provider_name)
    body = marshal(payload)
    upstream_headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {_key_for(provider_name)}",
    }
    last_error: str | None = None
    last_status: int | None = None
    last_body: str | None = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        for ch in candidates:
            if not ch["enabled"] or not ch["healthy"]:
                continue
            url = f"{ch['base_url']}/v1/chat/completions"
            try:
                r = await _call_with_retry(client, url, upstream_headers, body)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                ch_mod.mark_unhealthy(ch["id"])
                continue
            if r.status_code < 400:
                translated = json.loads(r.text)
                # Settle: refund the difference between estimate and actual.
                usage = translated.setdefault("usage", {})
                pt = max(usage.get("prompt_tokens", 0), prompt_tokens)
                ct = usage.get(
                    "completion_tokens",
                    max(1, len(translated["choices"][0]["message"]["content"]) // 4),
                )
                actual = (pt + ct) * RATE_PER_TOKEN
                settle(p.user_id, estimate, actual)
                translated["usage"] = {
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "total_tokens": pt + ct,
                }
                translated["quota_charged"] = actual
                return JSONResponse(translated)
            last_status = r.status_code
            last_body = r.text
            last_error = f"{r.status_code}: {r.text}"
            ch_mod.mark_unhealthy(ch["id"])

    # All channels failed: refund the pre-consume in full.
    refund(p.user_id, estimate)
    if last_status is not None and last_status >= 400:
        raise HTTPException(status_code=last_status, detail=last_body)
    raise HTTPException(status_code=502, detail=last_error or "all channels failed")


# Mount s12 LAST so our own /v1/chat/completions route is matched first.
# Starlette iterates routes in registration order; a local route shadows
# the mounted one (same gotcha as Tasks 4.2/4.3).
app.mount("/", s12_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8013")))
