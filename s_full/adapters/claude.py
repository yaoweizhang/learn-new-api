"""Claude (Anthropic) adapter — translates between OpenAI wire format and Anthropic."""
from __future__ import annotations

from s_full.adapters.base import Provider


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
        if model.startswith("claude-"):
            model = model
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
