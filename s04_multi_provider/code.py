"""s04: multi-provider adapter dispatch by model name.

Same kernel as s03; the only change is the request goes through a provider
adapter so a single OpenAI-shaped client request can reach any upstream.
Streaming through adapters is left for v2 (this chapter is non-streaming).
"""
from __future__ import annotations

import json
import os

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common.json import marshal, unmarshal_str
from s04_multi_provider.adapters import pick_provider

PORT = int(os.getenv("PORT", "8004"))


def _key_for(provider_name: str) -> str:
    env = {
        "openai": "UPSTREAM_OPENAI_KEY",
        "claude": "UPSTREAM_CLAUDE_KEY",
        "gemini": "UPSTREAM_GEMINI_KEY",
    }.get(provider_name, "")
    return os.getenv(env, "") if env else ""


app = FastAPI(title="learn-new-api s04")


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = None
    max_tokens: int | None = None
    system: str | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    try:
        provider = pick_provider(req.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    payload = req.model_dump(exclude_none=True)
    payload["_api_key"] = _key_for(provider.name)
    url, headers, upstream_body = provider.to_upstream(payload)
    body_bytes = marshal(upstream_body)
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(url, content=body_bytes, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    translated = provider.from_upstream(json.loads(r.text))
    return JSONResponse(translated)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)