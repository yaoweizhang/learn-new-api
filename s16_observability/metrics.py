"""Prometheus counters + structlog setup."""
from __future__ import annotations

import logging

import structlog
from prometheus_client import Counter, Histogram

REQUESTS = Counter(
    "learn_new_api_requests_total",
    "Total /v1/chat/completions requests",
    ["model", "status"],
)
LATENCY = Histogram(
    "learn_new_api_request_latency_seconds",
    "Request latency",
    ["model"],
)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
    )