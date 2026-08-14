"""OpenAI adapter — translates between OpenAI wire format and the upstream."""
from __future__ import annotations

from s_full.adapters.base import Provider


class OpenAIProvider(Provider):
    name = "openai"

    def to_upstream(self, req: dict) -> tuple[str, dict, dict]:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {req.pop('_api_key', '')}"}
        return url, headers, req

    def from_upstream(self, payload: dict) -> dict:
        return payload  # already OpenAI shape
