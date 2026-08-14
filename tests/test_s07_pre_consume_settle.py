import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s07_pre_consume_settle.code import app  # noqa: E402
from s05_api_key_auth.storage import register_key, reset_keys  # noqa: E402
from s07_pre_consume_settle.quota import reset, get_balance  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    reset_keys()
    reset()
    register_key("u1", "sk-q")
    yield
    reset_keys()
    reset()


def test_pre_consume_deducts_before_call(upstream_openai):
    from s07_pre_consume_settle.quota import set_balance, get_balance
    set_balance("u1", 1_000_000)
    before = get_balance("u1")
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer sk-q"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    after = get_balance("u1")
    assert after < before  # something was deducted


def test_insufficient_quota_returns_402():
    from s07_pre_consume_settle.quota import set_balance
    set_balance("u2", 0)
    from s05_api_key_auth.storage import register_key as rk
    rk("u2", "sk-poor")
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer sk-poor"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 402


def test_upstream_failure_refunds_pre_consume():
    from s07_pre_consume_settle.quota import set_balance
    set_balance("u1", 1_000_000)
    before = get_balance("u1")
    import respx
    with respx.mock(base_url="https://api.openai.com", assert_all_called=False) as mock:
        mock.post("/v1/chat/completions").mock(return_value=__import__("httpx").Response(500, text="boom"))
        with TestClient(app) as c:
            r = c.post(
                "/v1/chat/completions",
                headers={"authorization": "Bearer sk-q"},
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
            )
    assert r.status_code == 500
    assert get_balance("u1") == before  # fully refunded
