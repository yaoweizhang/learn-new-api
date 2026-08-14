import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s06_token_counting.code import app  # noqa: E402
from s05_api_key_auth.storage import register_key, reset_keys  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    reset_keys()
    register_key("u1", "sk-tok")
    yield
    reset_keys()


def test_usage_field_populated(upstream_openai):
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer sk-tok"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["usage"]["prompt_tokens"] >= 1
    assert body["usage"]["total_tokens"] >= body["usage"]["prompt_tokens"]


def test_non_openai_falls_back_to_char_estimator(upstream_claude):
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer sk-tok"},
            json={"model": "claude-3-5-sonnet-20241022", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    assert r.json()["usage"]["prompt_tokens"] >= 1