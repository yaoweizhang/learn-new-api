"""Gemini (Google) adapter — translates between OpenAI wire format and Gemini."""
from __future__ import annotations

from s_full.adapters.base import Provider


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
