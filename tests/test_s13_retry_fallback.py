"""s13: fail over to the next channel when the current one errors.

Each channel is tried exactly once per request — no in-request retry.
A non-2xx (or transport error) on one channel escalates immediately to
the next candidate, matching new-api's behavior.
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
def multi_channel_mock():
    """Primary channel returns 503; secondary returns 200. Verifies that
    escalation happens WITHOUT retrying the primary."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://primary.example.com/v1/chat/completions").mock(
            return_value=Response(503, text="busy")
        )
        mock.post("https://secondary.example.com/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok from secondary"},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
        )
        yield mock


@pytest.fixture(autouse=True)
def _clean():
    reset_channels()
    reset_quota()
    reset_buckets()
    reset_keys()
    # s13 enforces auth + rate + quota on /v1/chat/completions.
    register_key("user-1", "sk-test-123")
    set_balance("user-1", 100_000)
    yield
    reset_channels()
    reset_quota()
    reset_buckets()
    reset_keys()


def test_escalates_to_next_channel_on_failure(multi_channel_mock):
    """Primary returns 503; secondary is at lower priority and must be tried
    next. Each channel should be attempted exactly once — no retry on primary."""
    create_channel("primary", "openai", "https://primary.example.com", weight=100, priority=0)
    create_channel("secondary", "openai", "https://secondary.example.com", weight=100, priority=1)
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer sk-test-123"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    # Each channel attempted exactly once — escalation, not retry.
    assert multi_channel_mock.calls.call_count == 2


def test_unauthenticated_request_rejected(multi_channel_mock):
    """Regression guard for the s13 unauthenticated-bypass fix."""
    create_channel("primary", "openai", "https://primary.example.com", weight=100, priority=0)
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 401
    # And upstream must NOT have been hit — explicit assertion to document the bypass is closed.
    assert multi_channel_mock.calls.call_count == 0
