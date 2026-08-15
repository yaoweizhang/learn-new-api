"""In-memory token blacklist (SHA-256 keyed, lock-protected).

Tokens are credentials — never stored as plaintext. The blacklist is keyed
on SHA-256(token) so a process memory dump or accidental log line doesn't
yield a usable token. In-memory is sufficient for the tutorial; future
chapters can swap in a Redis-backed impl by satisfying the same Protocol.
"""
from __future__ import annotations

import hashlib
import threading
from typing import Protocol


class TokenBlacklist(Protocol):
    def revoke(self, token: str) -> None: ...
    def is_revoked(self, token: str) -> bool: ...
    def reset(self) -> None: ...


class InMemoryTokenBlacklist:
    def __init__(self) -> None:
        self._revoked: set[str] = set()
        self._lock = threading.Lock()

    def _hash(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def revoke(self, token: str) -> None:
        with self._lock:
            self._revoked.add(self._hash(token))

    def is_revoked(self, token: str) -> bool:
        with self._lock:
            return self._hash(token) in self._revoked

    def reset(self) -> None:
        with self._lock:
            self._revoked.clear()


_default: TokenBlacklist = InMemoryTokenBlacklist()


def set_default(bl: TokenBlacklist) -> None:
    """Test-only seam."""
    global _default
    _default = bl


def get_default() -> TokenBlacklist:
    return _default