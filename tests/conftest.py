"""Shared pytest fixtures for learn-new-api.

Each chapter's test file imports from here. Upstream mocks are respx routes
that match real wire-format shapes (OpenAI / Claude / Gemini) so tests catch
adapter regressions.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import respx
from httpx import Response

# Make chapter modules importable as `sNN_topic.code`
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def upstream_openai():
    """Mock OpenAI /v1/chat/completions (handles both non-streaming and streaming; SSE is selected by the request body, not the URL)."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello back"},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                },
            )
        )
        yield mock


@pytest.fixture
def upstream_claude():
    """Mock Anthropic Messages endpoint."""
    with respx.mock(base_url="https://api.anthropic.com") as mock:
        mock.post("/v1/messages").mock(
            return_value=Response(
                200,
                json={
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi from claude"}],
                    "model": "claude-3-5-sonnet-20241022",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 4, "output_tokens": 3},
                },
            )
        )
        yield mock


@pytest.fixture
def upstream_gemini():
    """Mock Gemini generateContent endpoint."""
    with respx.mock(base_url="https://generativelanguage.googleapis.com") as mock:
        mock.post(
            "/v1beta/models/gemini-1.5-flash:generateContent"
        ).mock(
            return_value=Response(
                200,
                json={
                    "candidates": [{
                        "content": {"parts": [{"text": "hi from gemini"}], "role": "model"},
                        "finishReason": "STOP",
                    }],
                    "usageMetadata": {
                        "promptTokenCount": 6,
                        "candidatesTokenCount": 4,
                        "totalTokenCount": 10,
                    },
                },
            )
        )
        yield mock


@pytest.fixture
def upstream_openai_streaming():
    """Mock OpenAI /v1/chat/completions returning SSE chunks.

    Three `data:` chunks with delta content, then `data: [DONE]`. No
    per-chunk usage — exercises the settle-to-estimate (no refund) path.
    """
    sse_body = (
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
        b'data: {"id":"chatcmpl-2","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" there"},"finish_reason":null}]}\n\n'
        b'data: {"id":"chatcmpl-3","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"!"},"finish_reason":"stop"}]}\n\n'
        b'data: [DONE]\n\n'
    )
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(
                200,
                content=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )
        yield mock


@pytest.fixture
def upstream_openai_streaming_with_usage():
    """Like upstream_openai_streaming but the LAST data: chunk carries
    `usage.completion_tokens`, so settle refunds the difference."""
    sse_body = (
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"a"},"finish_reason":null}]}\n\n'
        b'data: {"id":"chatcmpl-2","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"b"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":1,"total_tokens":6}}\n\n'
        b'data: [DONE]\n\n'
    )
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(
                200,
                content=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )
        yield mock


@pytest.fixture
def respx_mock_gemini():
    """Convenience wrapper used by the streaming Gemini-400 test. The
    route rejects the request before the upstream is hit, so the route
    may remain uncalled — we use `assert_all_called=False` to skip
    respx's assertion-on-teardown."""
    with respx.mock(
        base_url="https://generativelanguage.googleapis.com",
        assert_all_called=False,
    ) as mock:
        mock.post(
            "/v1beta/models/gemini-1.5-flash:generateContent"
        ).mock(
            return_value=Response(
                200,
                json={
                    "candidates": [{
                        "content": {"parts": [{"text": "hi from gemini"}], "role": "model"},
                        "finishReason": "STOP",
                    }],
                    "usageMetadata": {
                        "promptTokenCount": 6,
                        "candidatesTokenCount": 4,
                        "totalTokenCount": 10,
                    },
                },
            )
        )
        yield mock