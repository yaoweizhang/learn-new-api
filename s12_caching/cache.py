"""Exact-match cache backed by an in-memory dict; the interface mirrors redis-py.

Real implementation would swap the dict for `redis.Redis(...)`. Tests use the
in-memory backend.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

_lock = threading.Lock()
_store: dict[str, tuple[float, bytes]] = {}  # key -> (expires_at, value)


def reset_cache() -> None:
    with _lock:
        _store.clear()


def _key(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get(payload: dict) -> bytes | None:
    key = _key(payload)
    now = time.monotonic()
    with _lock:
        entry = _store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < now:
            _store.pop(key, None)
            return None
        return value


def set(payload: dict, value: bytes, ttl_seconds: int = 300) -> None:
    key = _key(payload)
    expires_at = time.monotonic() + ttl_seconds
    with _lock:
        _store[key] = (expires_at, value)


def stats() -> dict:
    with _lock:
        now = time.monotonic()
        live = sum(1 for exp, _ in _store.values() if exp > now)
        return {"size": len(_store), "live": live}