"""s11: async call logging test.

After a chat completion, an entry must show up in /admin/logs once the
flush loop has had a chance to drain the buffer. We tolerate a small
sleep here because the flusher is genuinely async (v2 would replace this
with a deterministic queue + event); this is acceptable timing-dependence
documented in the test.
"""
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s11_call_logs.code import app  # noqa: E402
from s10_channel_management.channels import reset_channels, create_channel  # noqa: E402
from s09_user_system.users import reset_db, create_user  # noqa: E402
from s09_user_system.jwt_util import issue  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    reset_channels()
    reset_db()
    from s05_api_key_auth.storage import reset_keys
    from s07_pre_consume_settle.quota import reset
    from s08_rate_limiting.bucket import reset_buckets
    reset_keys()
    reset()
    reset_buckets()
    yield
    reset_channels()
    reset_db()
    reset_keys()
    reset()
    reset_buckets()


def test_logs_written_after_call(upstream_openai):
    from s11_call_logs.log_store import reset_logs, list_logs
    from s05_api_key_auth.storage import register_key
    from s07_pre_consume_settle.quota import set_balance
    reset_logs()
    create_channel("c1", "openai", "https://api.openai.com", weight=100, priority=0)
    pwd = b"secret123"
    import bcrypt
    # NOTE: brief had a typo `bcrypt.hashpwd = bcrypt.hashpw(...)` which would
    # crash because `bcrypt.hashpwd` is not a real attribute. Fixed to plain
    # assignment below.
    pw_hash = bcrypt.hashpw(pwd, bcrypt.gensalt()).decode("utf-8")
    uid = create_user("u@example.com", pw_hash, is_admin=True)
    token = issue(uid, "u@example.com", is_admin=True)

    # NOTE: brief only signed a JWT, but the chat endpoint (s08) gates on an
    # API key registered in s05 storage + a preloaded quota balance in s07.
    # Register both here so the upstream call returns 200.
    api_key = "sk-test-key"
    register_key(user_id=str(uid), key=api_key)
    set_balance(str(uid), 10_000_000)

    # The chat endpoint is reachable at /v1/chat/completions via the
    # s11 -> s10 -> s09 -> s08 mount chain (every chapter mounts at root).
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {api_key}"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200, r.text
    # Allow async flusher to drain the buffer to _flushed.
    # Known acceptable timing-dependence; v2 will use a deterministic event.
    time.sleep(0.2)
    logs = list_logs()
    assert len(logs) == 1
    assert logs[0]["model"] == "gpt-4o-mini"