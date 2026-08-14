import sys
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s04_multi_provider.code import app  # noqa: E402


@pytest.fixture
def three_upstreams():
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=Response(200, json={"choices": [{"message": {"content": "openai-ok"}}]})
        )
        mock.post("https://api.anthropic.com/v1/messages").mock(
            return_value=Response(200, json={"content": [{"type": "text", "text": "claude-ok"}]})
        )
        mock.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent").mock(
            return_value=Response(200, json={"candidates": [{"content": {"parts": [{"text": "gemini-ok"}]}}]})
        )
        yield mock


def test_routes_openai(three_upstreams):
    with TestClient(app) as c2:
        r = c2.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    assert "openai-ok" in r.text


def test_routes_claude(three_upstreams):
    with TestClient(app) as c2:
        r = c2.post(
            "/v1/chat/completions",
            json={"model": "claude-3-5-sonnet-20241022", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    assert "claude-ok" in r.text


def test_routes_gemini(three_upstreams):
    with TestClient(app) as c2:
        r = c2.post(
            "/v1/chat/completions",
            json={"model": "gemini-1.5-flash", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    assert "gemini-ok" in r.text


def test_unknown_model_rejected():
    with TestClient(app) as c2:
        r = c2.post(
            "/v1/chat/completions",
            json={"model": "mystery-7", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 400