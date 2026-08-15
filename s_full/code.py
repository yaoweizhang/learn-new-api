"""s_full entrypoint. Wires routes, middleware, observability.

Note: unlike s16 (which mounts the s15 -> s14 -> ... chain), s_full has
its OWN routers defined locally. It does NOT mount the chapter chain —
the chapters are kept isolated for tutorial reasons; s_full is the
production-shape integration that consolidates them.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from s_full.middleware.trace import TraceAndMetricsMiddleware
from s_full.models.log import flush_loop
from s_full.routes import admin, auth, chat

app = FastAPI(title="learn-new-api s_full")
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(chat.router)
app.add_middleware(TraceAndMetricsMiddleware)


_stop_event: asyncio.Event | None = None
_task: asyncio.Task | None = None


@app.on_event("startup")
async def _start_flusher():
    global _stop_event, _task
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(flush_loop(_stop_event))


@app.on_event("shutdown")
async def _stop_flusher():
    if _stop_event is not None:
        _stop_event.set()
    # Final synchronous flush so tests don't lose the last batch.
    from s_full.models.log import _drain_now
    _drain_now()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8099")))
