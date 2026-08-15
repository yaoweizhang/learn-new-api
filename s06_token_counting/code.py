"""s06: token counting + populate usage.

Mirrors the request to count prompt tokens (tiktoken for OpenAI, char/4 for
others) before forwarding. If the upstream response already carries usage,
use it; otherwise synthesize from our estimate + a char/4 estimate of the
reply.
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

PORT = int(os.getenv("PORT", "8006"))


def _key_for(provider_name: str) -> str:
    env = {
        "openai": "UPSTREAM_OPENAI_KEY",
        "claude": "UPSTREAM_CLAUDE_KEY",
        "gemini": "UPSTREAM_GEMINI_KEY",
    }.get(provider_name, "")
    return os.getenv(env, "") if env else ""


app = FastAPI(title="learn-new-api s06")


def require_api_key(request: Request) -> Principal:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    key = auth.removeprefix("Bearer ").strip()
    if is_blocked(key):
        raise HTTPException(status_code=401, detail="key blocked")
    principal = lookup_key(key)
    if principal is None:
        raise HTTPException(status_code=401, detail="unknown key")
    return principal


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/chat/completions", dependencies=[Depends(require_api_key)])
async def chat_completions(req: ChatCompletionRequest):
    prompt_tokens = count_prompt([m.model_dump() for m in req.messages], req.model)
    try:
        provider = pick_provider(req.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    payload = req.model_dump(exclude_none=True)
    payload["_api_key"] = _key_for(provider.name)
    url, headers, upstream_body = provider.to_upstream(payload)
    body_bytes = marshal(upstream_body)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, content=body_bytes, headers=headers)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    translated = provider.from_upstream(json.loads(r.text))
    if "usage" not in translated or not translated["usage"].get("total_tokens"):
        completion = translated["choices"][0]["message"]["content"]
        completion_tokens = max(1, len(completion) // 4)
        translated["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    else:
        translated["usage"]["prompt_tokens"] = max(
            translated["usage"].get("prompt_tokens", 0), prompt_tokens
        )
    return JSONResponse(translated)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)