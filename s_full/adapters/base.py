"""Provider ABC. Copied from s04 with no behavior change.

`supports_streaming` is a capability flag — the chat route checks it before
opening an upstream SSE stream and returns 400 for providers that opt out.
Defaults True so most providers (OpenAI, Claude, etc.) just work.
"""
from abc import ABC, abstractmethod


class Provider(ABC):
    name: str
    supports_streaming: bool = True

    @abstractmethod
    def to_upstream(self, req: dict) -> tuple[str, dict, dict]: ...

    @abstractmethod
    def from_upstream(self, payload: dict) -> dict: ...
