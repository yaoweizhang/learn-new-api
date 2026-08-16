"""Provider adapters — translate between OpenAI wire format and each upstream.

Each adapter has two methods:
    to_upstream(openai_request: dict) -> tuple[url, headers, body]
    from_upstream(upstream_json: dict) -> openai_response_dict
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Provider(ABC):
    name: str

    @abstractmethod
    def to_upstream(self, req: dict) -> tuple[str, dict, dict]: ...

    @abstractmethod
    def from_upstream(self, payload: dict) -> dict: ...


class OpenAIProvider(Provider):
    name = "openai"

    def to_upstream(self, req: dict) -> tuple[str, dict, dict]:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {req.pop('_api_key', '')}"}
        return url, headers, req

    def from_upstream(self, payload: dict) -> dict:
        return payload  # already OpenAI shape


class ClaudeProvider(Provider):
    name = "claude"

    def to_upstream(self, req: dict) -> tuple[str, dict, dict]:
        api_key = req.pop("_api_key", "")
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        model = req["model"]
        body = {
            "model": model,
            "max_tokens": req.get("max_tokens", 1024),
            "messages": req["messages"],
        }
        if "temperature" in req:
            body["temperature"] = req["temperature"]
        if "system" in req:
            body["system"] = req["system"]
        return url, headers, body

    def from_upstream(self, payload: dict) -> dict:
        text = "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )
        return {
            "id": payload.get("id", "claude-relay"),
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": payload.get("stop_reason", "stop"),
            }],
            "usage": {
                "prompt_tokens": payload.get("usage", {}).get("input_tokens", 0),
                "completion_tokens": payload.get("usage", {}).get("output_tokens", 0),
                "total_tokens": (
                    payload.get("usage", {}).get("input_tokens", 0)
                    + payload.get("usage", {}).get("output_tokens", 0)
                ),
            },
        }


class GeminiProvider(Provider):
    name = "gemini"

    def to_upstream(self, req: dict) -> tuple[str, dict, dict]:
        api_key = req.pop("_api_key", "")
        model = req["model"]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        headers = {"content-type": "application/json"}
        contents = []
        system = req.get("system")
        msgs = list(req["messages"])
        if system:
            contents.append({"role": "user", "parts": [{"text": system}]})
        for m in msgs:
            role = "model" if m["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
        body = {"contents": contents}
        return url, headers, body

    def from_upstream(self, payload: dict) -> dict:
        text = "".join(
            part.get("text", "")
            for cand in payload.get("candidates", [])
            for part in cand.get("content", {}).get("parts", [])
        )
        usage = payload.get("usageMetadata", {})
        return {
            "id": "gemini-relay",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": usage.get("promptTokenCount", 0),
                "completion_tokens": usage.get("candidatesTokenCount", 0),
                "total_tokens": usage.get("totalTokenCount", 0),
            },
        }


def pick_provider(model: str) -> Provider:
    if model.startswith("gpt-") or model.startswith("o"):
        return OpenAIProvider()
    if model.startswith("claude-"):
        return ClaudeProvider()
    if model.startswith("gemini-"):
        return GeminiProvider()
    raise ValueError(f"unknown model: {model}")