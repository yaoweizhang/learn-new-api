"""Provider ABC. Copied from s04 with no behavior change."""
from abc import ABC, abstractmethod


class Provider(ABC):
    name: str

    @abstractmethod
    def to_upstream(self, req: dict) -> tuple[str, dict, dict]: ...

    @abstractmethod
    def from_upstream(self, payload: dict) -> dict: ...
