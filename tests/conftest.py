"""Shared pytest fixtures for learn-new-api.

Each chapter's test file imports from here. Upstream mocks are respx routes
that match real wire-format shapes (OpenAI / Claude / Gemini) so tests catch
adapter regressions.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

# Make chapter modules importable as `sNN_topic.code`
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def upstream_openai():
    """Mock OpenAI chat completion + streaming endpoint."""
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