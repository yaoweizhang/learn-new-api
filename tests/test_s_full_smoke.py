import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s_full.code import app  # noqa: E402


def test_health():
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 200


def test_full_relay_roundtrip(upstream_openai):
    from s_full.services.billing import top_up
    from s_full.models.user import create_user, reset_db
    from s_full.models.channel import create_channel, reset_channels
    from s_full.middleware.auth import issue_token
    reset_db(); reset_channels()
    uid = create_user("u@x.com", "x")
    top_up("u@x.com", 1_000_000)
    create_channel("c1", "openai", "https://api.openai.com", weight=100, priority=0)
    token = issue_token(uid, "u@x.com", is_admin=False)
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {token}"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
