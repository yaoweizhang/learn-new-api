import sys
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s08_rate_limiting.code import app  # noqa: E402
from s05_api_key_auth.storage import register_key, reset_keys  # noqa: E402
from s07_pre_consume_settle.quota import reset, set_balance  # noqa: E402
from s08_rate_limiting.bucket import reset_buckets, configure  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    reset_keys()
    reset()
    reset_buckets()
    register_key("u1", "sk-rl")
    set_balance("u1", 10_000_000)
    configure("u1", capacity=2, refill_per_sec=0.0)  # 2 tokens total, no refill
    yield
    reset_keys()
    reset()
    reset_buckets()


def test_first_two_pass_third_blocked(upstream_openai):
    with TestClient(app) as c:
        r1 = c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer sk-rl"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        r2 = c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer sk-rl"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        r3 = c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer sk-rl"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429