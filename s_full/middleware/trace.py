"""TraceAndMetricsMiddleware — Prometheus counters + trace_id propagation.

Same shape as s16, minus the structlog dependency to keep s_full self-contained.
"""
from __future__ import annotations

import json
import time
import uuid

from fastapi import Request
from prometheus_client import Counter, Histogram
from starlette.middleware.base import BaseHTTPMiddleware

# Distinct metric prefix from s16 so the two apps can coexist in the same
# pytest process without colliding in the default Prometheus registry.
REQUESTS = Counter(
    "learn_new_api_s_full_requests_total",
    "Total /v1/chat/completions requests (s_full)",
    ["model", "status"],
)
LATENCY = Histogram(
    "learn_new_api_s_full_request_latency_seconds",
    "Request latency (s_full)",
    ["model"],
)


class TraceAndMetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex
        request.state.trace_id = trace_id

        # Extract the chat model from the JSON body so the metric label is
        # meaningful. Same body-peek + rewind pattern as s11 (Starlette
        # caches body in request._body, so downstream middlewares still see it).
        # Match against the original path; sub-app mounts can prefix it but /v1/chat/completions stays at root today.
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
        response.headers["x-trace-id"] = trace_id
        return response
