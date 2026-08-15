"""s12: exact-match response cache.

Cache key: sha256 of {model, messages, temperature}. TTL configurable.
Skip cache if `stream=True` (streaming responses can't be cached whole).

Implementation note: cache lookup happens in a middleware that wraps the
mounted s11 app. We do NOT define our own `/v1/chat/completions` route —
FastAPI would shadow the mounted one and break everything (see s11 README).

The middleware checks the as-called path. Through the chapter chain
s12 -> s11 -> s10 -> s09 -> s08, s08's chat route (`/v1/chat/completions`)
is reached at the external path `/v1/v1/chat/completions` (s09 mounts
s08 at `/v1`). We match that path here.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from s11_call_logs.code import app as s11_app
from s12_caching import cache

app = FastAPI(title="learn-new-api s12")
app.mount("/", s11_app)


class CacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "POST" and request.url.path == "/v1/v1/chat/completions":
            body_bytes = await request.body()
            try:
                payload = json.loads(body_bytes)
            except Exception:
                payload = {}
            if not payload.get("stream"):
                hit = cache.get(payload)
                if hit is not None:
                    return Response(content=hit, media_type="application/json")
            response = await call_next(request)
            if response.status_code == 200 and not payload.get("stream"):
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                body = b"".join(chunks)
                cache.set(payload, body)
                return Response(
                    content=body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                )
            return response
        return await call_next(request)


app.add_middleware(CacheMiddleware)


@app.get("/admin/cache/stats")
def cache_stats() -> dict:
    return cache.stats()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8012")))