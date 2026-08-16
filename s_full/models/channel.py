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
        # provider: dropped — s_full doesn't use channel pool selection
        ch = Channel(id=cid, name=name, base_url=base_url, weight=weight, priority=priority)
        _channels[cid] = ch
        return ch


def list_channels() -> list[dict]:
    with _lock:
        return [asdict(c) for c in _channels.values()]


def get_channel(cid: int) -> Channel | None:
    with _lock:
        return _channels.get(cid)
