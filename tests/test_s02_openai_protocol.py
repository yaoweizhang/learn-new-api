import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s02_openai_protocol.code import app  # noqa: E402


def test_openai_route_exists(upstream_openai):
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert r.status_code == 200
    assert "choices" in r.json()


def test_request_validation_rejects_missing_messages():
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions", json={"model": "gpt-4o-mini"})
    assert r.status_code == 422
