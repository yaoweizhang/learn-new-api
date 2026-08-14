"""s13: retry transient upstream errors + fall back to next channel."""
import sys
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s13_retry_fallback.code import app  # noqa: E402
from s10_channel_management.channels import reset_channels, create_channel  # noqa: E402


@pytest.fixture
def openai_mock():
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://api.openai.com/v1/chat/completions").mock(side_effect=[
            Response(503, text="busy"),
            Response(503, text="busy"),
            Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
        ])
        yield mock


@pytest.fixture(autouse=True)
def _clean():
    reset_channels()
    yield
    reset_channels()


def test_retries_transient_then_succeeds(openai_mock):
    create_channel("primary", "openai", "https://api.openai.com", weight=100, priority=0)
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    assert openai_mock.calls.call_count == 3