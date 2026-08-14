"""s01: minimal HTTP forwarding kernel.

A FastAPI app with one route that forwards a JSON body verbatim to a single
upstream URL. No protocol awareness, no auth, no streaming. The kernel every
later chapter extends.
"""
from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

PORT = int(os.getenv("PORT", "8001"))
FORWARD_TARGET = os.getenv("FORWARD_TARGET", "https://api.openai.com/v1/chat/completions")
UPSTREAM_KEY = os.getenv("UPSTREAM_OPENAI_KEY", "")

app = FastAPI(title="learn-new-api s01")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


class RelayRequest(BaseModel):
    model: str
    messages: list[dict]


async def forward_request(target_url: str, payload: dict) -> dict:
    """POST `payload` to `target_url` and return the decoded JSON response.

    Upstream transport failures become 502; upstream error statuses are
    passed through unchanged so the caller sees the real reason.
    """
    headers = {"Authorization": f"Bearer {UPSTREAM_KEY}"} if UPSTREAM_KEY else {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(target_url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


@app.post("/relay")
async def relay(req: RelayRequest) -> dict:
    return await forward_request(FORWARD_TARGET, req.model_dump())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
