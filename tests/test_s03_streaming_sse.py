"""s03 streaming SSE passthrough.

Tests verify that:
- stream=true yields upstream SSE chunks verbatim to the client
- non-streaming requests still return JSON via the shared `upstream_openai` mock
"""
import sys
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s03_streaming_sse.code import app  # noqa: E402


SSE_PAYLOAD = (
    'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hello "},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"world"},"finish_reason":"stop"}]}\n\n'
    'data: [DONE]\n\n'
)


def test_streaming_returns_sse_chunks(upstream_openai):
    upstream_openai.post("/v1/chat/completions").mock(
        return_value=Response(
            200,
            content=SSE_PAYLOAD,
            headers={"content-type": "text/event-stream"},
        )
    )
    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        ) as r:
            chunks = list(r.iter_text())
    body = "".join(chunks)
    assert "hello " in body
    assert "world" in body
    assert "[DONE]" in body


def test_non_streaming_still_works(upstream_openai):
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200