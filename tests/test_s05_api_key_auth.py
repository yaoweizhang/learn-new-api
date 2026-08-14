import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s05_api_key_auth.code import app  # noqa: E402
from s05_api_key_auth.storage import reset_keys, register_key  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    reset_keys()
    register_key("user-1", "sk-test-123")
    yield
    reset_keys()


def test_missing_authorization_rejected():
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 401


def test_valid_key_passes_through(upstream_openai):
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer sk-test-123"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200


def test_unknown_key_rejected():
    # No upstream mock: the request 401s at the auth check before it ever
    # reaches the upstream. Registering an `upstream_openai` respx route
    # would cause assert_all_called to fire on context exit.
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer sk-nope"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 401