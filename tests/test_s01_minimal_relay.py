import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s01_minimal_relay.code import app, FORWARD_TARGET  # noqa: E402


def test_health():
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_forwards_to_upstream(upstream_openai):
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
    }
    with TestClient(app) as client:
        r = client.post("/relay", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "hello back"
    assert upstream_openai.calls.call_count == 1
