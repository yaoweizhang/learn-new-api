"""s13: retry transient upstream errors + fall back to next channel.

tenacity: 3 attempts, exponential backoff (0.2s, 0.4s, 0.8s).
If all attempts on the primary channel fail, mark it unhealthy; future calls
pick the next-priority channel.
"""
from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from common.json import marshal
from s04_multi_provider.adapters import pick_provider
from s10_channel_management import channels as ch_mod
from s12_caching.code import app as s12_app

app = FastAPI(title="learn-new-api s13")


TRANSIENT = (502, 503, 504, 429)


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


@app.post("/v1/chat/completions")
async def chat_with_retry(req: ChatCompletionRequest):
    try:
        pick_provider(req.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Find any channel whose provider matches; fall back through priorities.
    candidates = ch_mod.list_channels()
    if not candidates:
        raise HTTPException(status_code=503, detail="no channels configured")
    last_error: str | None = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        for ch in candidates:
            if not ch["enabled"] or not ch["healthy"]:
                continue
            url = f"{ch['base_url']}/v1/chat/completions"
            payload = req.model_dump(exclude_none=True)
            body = marshal(payload)
            try:
                r = await _call_with_retry(
                    client,
                    url,
                    {"content-type": "application/json", "authorization": f"Bearer {os.getenv('UPSTREAM_OPENAI_KEY','')}"},
                    body,
                )
                if r.status_code < 400:
                    return r.json()
                last_error = f"{r.status_code}: {r.text}"
            except httpx.HTTPError as exc:
                last_error = str(exc)
            ch_mod.mark_unhealthy(ch["id"])
    raise HTTPException(status_code=502, detail=last_error or "all channels failed")


# Mount s12 LAST so our own /v1/chat/completions route is matched first.
# Starlette iterates routes in registration order; a local route shadows
# the mounted one (same gotcha as Tasks 4.2/4.3).
app.mount("/", s12_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8013")))