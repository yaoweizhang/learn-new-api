import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s10_channel_management.code import app  # noqa: E402
from s10_channel_management.channels import reset_channels  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    reset_channels()
    yield
    reset_channels()


def _admin_token():
    from s10_channel_management.channels import create_channel, list_channels
    from s09_user_system.jwt_util import issue
    admin_token = issue(user_id=0, email="admin@example.com", is_admin=True)
    return admin_token


def test_admin_can_create_channel():
    with TestClient(app) as c:
        r = c.post(
            "/admin/channels",
            headers={"authorization": f"Bearer {_admin_token()}"},
            json={"name": "openai-primary", "provider": "openai", "base_url": "https://api.openai.com", "weight": 100, "priority": 0},
        )
    assert r.status_code == 201


def test_non_admin_cannot_create_channel():
    from s09_user_system.jwt_util import issue
    user_token = issue(user_id=1, email="u@example.com", is_admin=False)
    with TestClient(app) as c:
        r = c.post(
            "/admin/channels",
            headers={"authorization": f"Bearer {user_token}"},
            json={"name": "x", "provider": "openai", "base_url": "x", "weight": 1, "priority": 0},
        )
    assert r.status_code == 403


# --- pick_channel_for selection algorithm ---

def test_pick_channel_for_returns_none_when_empty():
    from s10_channel_management.channels import pick_channel_for
    assert pick_channel_for("gpt-4o-mini") is None


def test_pick_channel_for_returns_none_for_unknown_model():
    from s10_channel_management.channels import pick_channel_for, create_channel
    create_channel("c1", "openai", "https://api.openai.com", weight=100, priority=0)
    assert pick_channel_for("unknown-model") is None


def test_pick_channel_for_filters_by_provider():
    from s10_channel_management.channels import pick_channel_for, create_channel
    openai = create_channel("oa", "openai", "https://api.openai.com", weight=100, priority=0)
    claude = create_channel("cl", "claude", "https://api.anthropic.com", weight=100, priority=0)
    # A gpt model must NOT land on the claude channel.
    picked = pick_channel_for("gpt-4o-mini")
    assert picked is not None and picked.id == openai.id
    # A claude model must NOT land on the openai channel.
    picked = pick_channel_for("claude-3-5-sonnet")
    assert picked is not None and picked.id == claude.id


def test_pick_channel_for_skips_unhealthy_and_disabled():
    from s10_channel_management.channels import pick_channel_for, create_channel, mark_unhealthy
    a = create_channel("a", "openai", "https://a", weight=100, priority=0)
    b = create_channel("b", "openai", "https://b", weight=200, priority=0)
    mark_unhealthy(b.id)
    # b has higher weight but is unhealthy; only a is eligible.
    picked = pick_channel_for("gpt-4o-mini")
    assert picked is not None and picked.id == a.id


def test_pick_channel_for_picks_lowest_priority_first():
    from s10_channel_management.channels import pick_channel_for, create_channel, mark_unhealthy
    primary = create_channel("primary", "openai", "https://primary", weight=10, priority=0)
    backup = create_channel("backup", "openai", "https://backup", weight=1000, priority=1)
    # primary has lower priority (preferred) but lower weight; selection must
    # honor priority first, then weighted-random within tier.
    picked = pick_channel_for("gpt-4o-mini")
    assert picked is not None and picked.id == primary.id


def test_pick_channel_for_distributes_load_by_weight(monkeypatch):
    """Within the same priority tier, weighted random must distribute across
    all channels (not always pick the highest-weight one)."""
    from s10_channel_management import channels as ch_mod
    from s10_channel_management.channels import pick_channel_for, create_channel
    a = create_channel("a", "openai", "https://a", weight=100, priority=0)
    b = create_channel("b", "openai", "https://b", weight=100, priority=0)
    # Deterministic random stream so the test is reproducible.
    rng = random.Random(42)
    monkeypatch.setattr(ch_mod.random, "choices", lambda population, weights, k: [rng.choices(population, weights=weights, k=k)[0]])
    counts = {a.id: 0, b.id: 0}
    for _ in range(200):
        picked = pick_channel_for("gpt-4o-mini")
        counts[picked.id] += 1
    # Both channels picked at least once (would be 0/200 if first-fit picked a).
    assert counts[a.id] > 0
    assert counts[b.id] > 0


def test_pick_channel_for_handles_zero_weights():
    """If every channel in the tier has weight 0, fall back to round-robin
    instead of random.choices raising on a zero-weight population."""
    from s10_channel_management.channels import pick_channel_for, create_channel
    a = create_channel("a", "openai", "https://a", weight=0, priority=0)
    create_channel("b", "openai", "https://b", weight=0, priority=0)
    picked = pick_channel_for("gpt-4o-mini")
    assert picked is not None  # doesn't raise; returns one of them


# Random import used in the distribute-by-weight test.
import random