"""s13: retry transient upstream errors + fall back to next channel.

tenacity: 3 attempts, exponential backoff (0.2s, 0.4s, 0.8s).
If all attempts on the primary channel fail, mark it unhealthy; future calls
pick the next-priority channel.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s13_retry_fallback.code import app  # noqa: E402
from s07_pre_consume_settle.quota import reset as reset_quota, set_balance  # noqa: E402
from s08_rate_limiting.bucket import reset_buckets  # noqa: E402
from s10_channel_management.channels import reset_channels, create_channel  # noqa: E402
from s05_api_key_auth.storage import reset_keys, register_key  # noqa: E402


@pytest.fixture
def openai_mock():
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://api.openai.com/v1/chat/completions").mock(side_effect=[
            Response(503, text="busy"),
            Response(503, text="busy"),
            Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            ),
        ])
        yield mock


@pytest.fixture(autouse=True)
def _clean():
    reset_channels()
    reset_quota()
    reset_buckets()
    reset_keys()
    # s13 now enforces auth + rate + quota on /v1/chat/completions, so we
    # register a key and pre-fund the matching balance.
    register_key("user-1", "sk-test-123")
    set_balance("user-1", 100_000)
    yield
    reset_channels()
    reset_quota()
    reset_buckets()
    reset_keys()


def test_retries_transient_then_succeeds(openai_mock):
    create_channel("primary", "openai", "https://api.openai.com", weight=100, priority=0)
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer sk-test-123"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    assert openai_mock.calls.call_count == 3


def test_unauthenticated_request_rejected(openai_mock):
    """Regression guard for the s13 unauthenticated-bypass fix."""
    create_channel("primary", "openai", "https://api.openai.com", weight=100, priority=0)
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 401
    # And upstream must NOT have been hit — we mock it as assert_all_called=False,
    # but we explicitly assert here to document the bypass is closed.
    assert openai_mock.calls.call_count == 0
