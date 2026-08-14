"""Per-user token bucket. In-memory; Redis-backed variant in s12. Same shape as s08."""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_buckets: dict[int, tuple[float, float]] = {}  # user_id -> (tokens, last_ts)
_caps: dict[int, tuple[float, float]] = {}    # user_id -> (capacity, refill_per_sec)


def reset_buckets() -> None:
    with _lock:
        _buckets.clear()
        _caps.clear()


def configure(user_id: int, capacity: float, refill_per_sec: float) -> None:
    with _lock:
        _caps[user_id] = (capacity, refill_per_sec)
        _buckets[user_id] = (capacity, time.monotonic())


def _refill(user_id: int) -> float:
    tokens, last = _buckets[user_id]
    cap, refill = _caps[user_id]
    now = time.monotonic()
    tokens = min(cap, tokens + (now - last) * refill)
    _buckets[user_id] = (tokens, now)
    return tokens


def take(user_id: int, cost: float = 1.0) -> bool:
    with _lock:
        if user_id not in _caps:
            _caps[user_id] = (60.0, 1.0)  # default: 60 burst, 1 req/sec
            _buckets[user_id] = (60.0, time.monotonic())
        tokens = _refill(user_id)
        if tokens < cost:
            return False
        _buckets[user_id] = (tokens - cost, time.monotonic())
        return True
