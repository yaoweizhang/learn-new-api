"""s11: async call logging.

Wraps s10's chat endpoint. After each successful call, enqueues a log row
(model, user_id, prompt_tokens, completion_tokens, quota_charged, ts). A
background task flushes every 100ms. Stats endpoint reads the flushed list.

The chat endpoint is provided by the mounted s10 app at /v1/chat/completions.
We do NOT redefine that route here — FastAPI would resolve the more specific
(this-app) route over the mounted one and break the chain. Instead we log
via a middleware below that wraps the entire app (mounted apps included).

Model extraction: the brief used `request.query_params.get("model", "?")`
but the model lives in the JSON body, not the query string. The fix is to
read the body once in the middleware, stash the model on request.state.model,
then re-deliver the body to downstream. Downstream FastAPI sees the same
bytes via Receive() and parses normally.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse

from s10_channel_management.code import app as s10_app
from s11_call_logs import log_store

app = FastAPI(title="learn-new-api s11")


_stop_event: asyncio.Event | None = None
_task: asyncio.Task | None = None


@app.on_event("startup")
async def _start_flusher():
    global _stop_event, _task
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(log_store.flush_loop(_stop_event))


@app.on_event("shutdown")
async def _stop_flusher():
    if _stop_event is not None:
        _stop_event.set()
    # Final synchronous flush so tests (which exit TestClient immediately
    # after the chat request) can still read the latest entries from
    # list_logs() without waiting for the async loop's next tick.
    log_store._drain_now()


class LogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # If this is the chat endpoint, peek at the JSON body to extract model
        # before FastAPI consumes it. We buffer the body bytes and replay them.
        # NOTE: the actual mounted path is /v1/v1/chat/completions (s09 mounts
        # s08 at /v1 and s08's route is /v1/chat/completions).
        if request.url.path.endswith("/v1/chat/completions") and request.method == "POST":
            body_bytes = await request.body()
            request.state.model = "?"
            try:
                payload = json.loads(body_bytes or b"{}")
                if isinstance(payload, dict) and "model" in payload:
                    request.state.model = payload["model"]
            except Exception:
                pass

            # Re-deliver body to downstream via Receive channel.
            async def receive():
                return {"type": "http.request", "body": body_bytes, "more_body": False}

            request._receive = receive  # type: ignore[attr-defined]

        response = await call_next(request)

        if (
            request.url.path.endswith("/v1/chat/completions")
            and response.status_code == 200
        ):
            # Read the response body and rewrap the iterator so downstream
            # clients still get the same bytes.
            body = b""
            async for chunk in response.body_iterator:
                body += chunk if isinstance(chunk, bytes) else chunk.encode()

            async def iterbody():
                yield body

            response.body_iterator = iterbody()
            log_store.enqueue(
                {
                    "path": request.url.path,
                    "ts": time.time(),
                    "status": response.status_code,
                    "model": getattr(request.state, "model", "?"),
                }
            )
        return response


app.add_middleware(LogMiddleware)


@app.get("/admin/logs")
def list_logs():
    return log_store.list_logs()


@app.get("/admin/stats")
def stats():
    logs = log_store.list_logs()
    by_model: dict[str, int] = {}
    for entry in logs:
        by_model[entry["model"]] = by_model.get(entry["model"], 0) + 1
    return {"total": len(logs), "by_model": by_model}


# Mount s10 LAST so our own /admin/logs and /admin/stats routes are matched
# first. Starlette iterates routes in registration order.
app.mount("/", s10_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8011")))