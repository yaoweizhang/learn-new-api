"""Channel registry + selection. Same shape as s10."""
from __future__ import annotations

import threading
from dataclasses import dataclass, asdict

_lock = threading.Lock()
_channels: dict[int, dict] = {}
_next_id = 1


@dataclass
class Channel:
    id: int
    name: str
    provider: str
    base_url: str
    weight: int
    priority: int
    enabled: bool = True
    healthy: bool = True


def reset_channels() -> None:
    global _next_id
    with _lock:
        _channels.clear()
        _next_id = 1


def create_channel(name: str, provider: str, base_url: str, weight: int, priority: int) -> Channel:
    global _next_id
    with _lock:
        cid = _next_id
        _next_id += 1
        ch = Channel(id=cid, name=name, provider=provider, base_url=base_url, weight=weight, priority=priority)
        _channels[cid] = ch
        return ch


def list_channels() -> list[dict]:
    with _lock:
        return [asdict(c) for c in _channels.values()]


def get_channel(cid: int) -> Channel | None:
    with _lock:
        return _channels.get(cid)


def pick_channel_for(model_prefix: str) -> Channel | None:
    """Pick the highest-priority, enabled, healthy channel whose provider matches model_prefix."""
    with _lock:
        candidates = [c for c in _channels.values() if c.enabled and c.healthy]
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c.priority, -c.weight))
    return candidates[0]


def mark_unhealthy(cid: int) -> None:
    with _lock:
        if cid in _channels:
            _channels[cid].healthy = False
