"""s16: observability — Prometheus metrics + structured logs + trace_id."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from s15_docker_deployment.code import app as s15_app
from s16_observability.metrics import LATENCY, REQUESTS, configure_logging
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

configure_logging()
app = FastAPI(title="learn-new-api s16")
log = structlog.get_logger()


class TraceAndMetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex
        request.state.trace_id = trace_id

        # Extract the chat model from the JSON body so the metric label is
        # meaningful. Same body-peek + rewind pattern as s11 (Starlette
        # caches body in request._body, so downstream middlewares still see it).
        if request.method == "POST" and request.url.path == "/v1/chat/completions":
            try:
                body_bytes = await request.body()
                payload = json.loads(body_bytes or b"{}")
                request.state.model = payload.get("model") if isinstance(payload, dict) else None
            except Exception:
                request.state.model = None

        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start

        if request.url.path == "/v1/chat/completions":
            model = getattr(request.state, "model", None) or "unknown"
            REQUESTS.labels(model=model, status=response.status_code).inc()
            LATENCY.labels(model=model).observe(elapsed)
        log.info(
            "request",
            trace_id=trace_id,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            elapsed=elapsed,
        )
        response.headers["x-trace-id"] = trace_id
        return response


app.add_middleware(TraceAndMetricsMiddleware)


# /metrics must be registered BEFORE app.mount("/", ...) so it shadows the
# catch-all. Same Starlette routing rule used by s15 for /healthz.
@app.get("/metrics")
def metrics_endpoint():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# Mount s15 LAST so our own /metrics route is matched first.
app.mount("/", s15_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8016")))