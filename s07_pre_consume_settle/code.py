"""s07: pre-consume + settle.

Quota math:
    RATE = 1 quota per token (configurable; flat rate per chapter)
    estimate = prompt_tokens * RATE + expected_completion_tokens * RATE

    1. Pre-deduct estimate (fail with 402 if insufficient)
    2. Call upstream
    3. On success, settle: refund (estimate - actual) if actual < estimate
    4. On upstream failure, refund the full pre-deduct
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

from common.json import marshal
from s04_multi_provider.adapters import pick_provider
from s05_api_key_auth.storage import Principal, is_blocked, lookup_key
from s06_token_counting.tokenizer import count_prompt
from s07_pre_consume_settle.quota import deduct, get_balance, settle

PORT = int(os.getenv("PORT", "8007"))
RATE_PER_TOKEN = int(os.getenv("RATE_PER_TOKEN", "1"))


def _key_for(provider_name: str) -> str:
    env = {
        "openai": "UPSTREAM_OPENAI_KEY",
        "claude": "UPSTREAM_CLAUDE_KEY",
        "gemini": "UPSTREAM_GEMINI_KEY",
    }.get(provider_name, "")
    return os.getenv(env, "") if env else ""


app = FastAPI(title="learn-new-api s07")


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


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: int | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/quota/{user_id}")
def quota(user_id: str) -> dict:
    return {"user_id": user_id, "balance": get_balance(user_id)}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, principal: Principal = Depends(require_api_key)):
    prompt_tokens = count_prompt([m.model_dump() for m in req.messages], req.model)
    expected_completion = req.max_tokens or 256
    estimate = (prompt_tokens + expected_completion) * RATE_PER_TOKEN
    if not deduct(principal.user_id, estimate):
        raise HTTPException(status_code=402, detail="insufficient quota")

    try:
        provider = pick_provider(req.model)
    except ValueError:
        from s07_pre_consume_settle.quota import refund
        refund(principal.user_id, estimate)
        raise HTTPException(status_code=400, detail="unknown model")

    payload = req.model_dump(exclude_none=True)
    payload["_api_key"] = _key_for(provider.name)
    url, headers, upstream_body = provider.to_upstream(payload)
    body_bytes = marshal(upstream_body)
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(url, content=body_bytes, headers=headers)
        except httpx.HTTPError:
            from s07_pre_consume_settle.quota import refund
            refund(principal.user_id, estimate)
            raise HTTPException(status_code=502, detail="upstream error")

    if r.status_code >= 400:
        from s07_pre_consume_settle.quota import refund
        refund(principal.user_id, estimate)
        raise HTTPException(status_code=r.status_code, detail=r.text)

    translated = provider.from_upstream(json.loads(r.text))
    usage = translated.setdefault("usage", {})
    pt = max(usage.get("prompt_tokens", 0), prompt_tokens)
    ct = usage.get("completion_tokens", max(1, len(translated["choices"][0]["message"]["content"]) // 4))
    actual = (pt + ct) * RATE_PER_TOKEN
    settle(principal.user_id, estimate, actual)
    translated["usage"] = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}
    translated["quota_charged"] = actual
    return JSONResponse(translated)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
