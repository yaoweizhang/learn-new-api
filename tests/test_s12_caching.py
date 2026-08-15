"""s12: exact-match response cache test.

After the second identical request, the upstream OpenAI mock should be
hit exactly once (the first call) and the second one served from cache.

The chat endpoint is reachable at `/v1/chat/completions` via the
s12 -> s11 -> s10 -> s09 -> s08 mount chain (every chapter mounts at root).
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s12_caching.code import app  # noqa: E402
from s12_caching.cache import reset_cache  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    from s10_channel_management.channels import reset_channels, create_channel
    from s09_user_system.users import reset_db
    from s05_api_key_auth.storage import reset_keys, register_key
    from s07_pre_consume_settle.quota import reset, set_balance
    from s08_rate_limiting.bucket import reset_buckets
    from s11_call_logs.log_store import reset_logs

    reset_cache()
    reset_channels()
    reset_db()
    reset_keys()
    reset()
    reset_buckets()
    reset_logs()

    # s08 chat endpoint needs a channel + API key + quota.
    create_channel("c1", "openai", "https://api.openai.com", weight=100, priority=0)

    yield

    reset_cache()
    reset_channels()
    reset_db()
    reset_keys()
    reset()
    reset_buckets()
    reset_logs()


def test_identical_request_hits_cache(upstream_openai):
    from s09_user_system.users import create_user
    from s09_user_system.jwt_util import issue
    from s05_api_key_auth.storage import register_key
    from s07_pre_consume_settle.quota import set_balance
    import bcrypt

    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode("utf-8")
    uid = create_user("u@x.com", pw, is_admin=False)
    token = issue(uid, "u@x.com", is_admin=False)

    # s08 gates on an API key + preloaded quota.
    api_key = "sk-test-key"
    register_key(user_id=str(uid), key=api_key)
    set_balance(str(uid), 10_000_000)

    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
    with TestClient(app) as c:
        r1 = c.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {api_key}"},
            json=body,
        )
        r2 = c.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {api_key}"},
            json=body,
        )
    assert r1.status_code == r2.status_code == 200, (r1.status_code, r1.text, r2.text)
    # second call is cached -> upstream hit count stays at 1
    assert upstream_openai.calls.call_count == 1