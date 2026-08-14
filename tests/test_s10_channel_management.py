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