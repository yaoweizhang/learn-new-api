"""s02: speak OpenAI's `/v1/chat/completions` protocol.

Same relay kernel; the request schema now mirrors OpenAI's chat completions
contract so clients written against OpenAI work against us unchanged.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from common.json import marshal, unmarshal_str

PORT = int(os.getenv("PORT", "8002"))
FORWARD_TARGET = os.getenv(
    "FORWARD_TARGET", "https://api.openai.com/v1/chat/completions"
)
UPSTREAM_KEY = os.getenv("UPSTREAM_OPENAI_KEY", "")

app = FastAPI(title="learn-new-api s02")


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False


class ChatCompletionResponse(BaseModel):
    id: str
    object: str
    choices: list[dict]
    usage: dict


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(req: ChatCompletionRequest) -> dict:
    headers = {"Authorization": f"Bearer {UPSTREAM_KEY}"} if UPSTREAM_KEY else {}
    body = marshal(req.model_dump(exclude_none=True))
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(
                FORWARD_TARGET, content=body, headers={**headers, "content-type": "application/json"}
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return unmarshal_str(r.text, ChatCompletionResponse).model_dump()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
