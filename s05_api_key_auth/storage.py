"""API key → user lookup. In-memory for the tutorial; real impl uses Redis + DB."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Principal:
    user_id: str
    scopes: tuple[str, ...] = ()


_keys: dict[str, Principal] = {}


def register_key(user_id: str, key: str, scopes: tuple[str, ...] = ("chat",)) -> None:
    _keys[key] = Principal(user_id=user_id, scopes=scopes)


def lookup_key(key: str) -> Principal | None:
    return _keys.get(key)


def reset_keys() -> None:
    _keys.clear()


def is_blocked(key: str) -> bool:
    """Hook for Redis blocklist integration in a later chapter. Returns False here."""
    return False