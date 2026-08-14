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