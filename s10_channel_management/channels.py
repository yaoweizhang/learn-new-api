"""Channel registry + selection."""
from __future__ import annotations

import random
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


def _provider_for_model(model_name: str) -> str | None:
    """Map a model name to its provider. Mirrors s04_multi_provider's routing."""
    if model_name.startswith(("gpt-", "o")):
        return "openai"
    if model_name.startswith("claude-"):
        return "claude"
    if model_name.startswith("gemini-"):
        return "gemini"
    return None


def pick_channel_for(model_name: str) -> Channel | None:
    """Pick an enabled, healthy channel that serves the requested model.

    Selection algorithm (mirrors new-api's GetRandomSatisfiedChannel):
      1. Filter enabled + healthy
      2. Filter by provider that matches the model (gpt-* / o* -> openai, etc.)
      3. Take the lowest-priority tier (priority is an integer, lower = preferred)
      4. Within that tier, weighted random by `weight` — NOT first-fit

    A deterministic first-fit (old behavior) would route 100% of traffic to
    the highest-weight channel in the top tier and leave every other channel
    idle. Weighted random distributes load across the tier.
    """
    provider = _provider_for_model(model_name)
    if provider is None:
        return None
    with _lock:
        candidates = [
            c for c in _channels.values()
            if c.enabled and c.healthy and c.provider == provider
        ]
    if not candidates:
        return None
    min_priority = min(c.priority for c in candidates)
    tier = [c for c in candidates if c.priority == min_priority]
    weights = [max(c.weight, 0) for c in tier]
    # If every channel in the tier has weight 0, fall back to round-robin.
    if sum(weights) == 0:
        return tier[random.randrange(len(tier))]
    return random.choices(tier, weights=weights, k=1)[0]


def mark_unhealthy(cid: int) -> None:
    with _lock:
        if cid in _channels:
            _channels[cid].healthy = False