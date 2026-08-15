"""s03: streaming responses via SSE.

When `stream=true`, relay bytes from upstream directly without buffering so
clients see first-token latency. Non-streaming requests still return JSON.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from common.json import marshal, unmarshal_str

PORT = int(os.getenv("PORT", "8003"))
FORWARD_TARGET = os.getenv(
    "FORWARD_TARGET", "https://api.openai.com/v1/chat/completions"
)
UPSTREAM_KEY = os.getenv("UPSTREAM_OPENAI_KEY", "")

app = FastAPI(title="learn-new-api s03")


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


async def _relay_stream(req: ChatCompletionRequest) -> AsyncIterator[bytes]:
    headers = {"Authorization": f"Bearer {UPSTREAM_KEY}"} if UPSTREAM_KEY else {}
    body = marshal(req.model_dump(exclude_none=True))
    timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", FORWARD_TARGET, content=body,
            headers={**headers, "content-type": "application/json", "accept": "text/event-stream"},
        ) as upstream:
            async for chunk in upstream.aiter_bytes():
                yield chunk


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    if req.stream:
        return StreamingResponse(
            _relay_stream(req),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )
    headers = {"Authorization": f"Bearer {UPSTREAM_KEY}"} if UPSTREAM_KEY else {}
    body = marshal(req.model_dump(exclude_none=True))
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(
                FORWARD_TARGET, content=body,
                headers={**headers, "content-type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return JSONResponse(json.loads(r.text))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)