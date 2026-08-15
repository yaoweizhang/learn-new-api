import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s16_observability.code import app  # noqa: E402


def test_metrics_endpoint_exposes_counters():
    with TestClient(app) as c:
        r = c.get("/metrics")
    assert r.status_code == 200
    assert "learn_new_api_requests_total" in r.text


def test_trace_id_propagates_to_response():
    with TestClient(app) as c:
        r = c.get("/healthz", headers={"x-trace-id": "abc-123"})
    assert r.headers.get("x-trace-id") == "abc-123"


def test_chat_request_increments_counter():
    with TestClient(app) as c:
        c.post("/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
        r = c.get("/metrics")
    assert "learn_new_api_requests_total" in r.text
    # The middleware now reads `model` from the JSON body, so the label
    # carries the actual model name (was previously pinned to "unknown").
    assert 'model="gpt-4o-mini"' in r.text