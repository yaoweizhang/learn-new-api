import sys
from pathlib import Path

import pytest
import respx
import time
from fastapi.testclient import TestClient
from httpx import Response

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


def test_full_relay_uses_provider_api_key(upstream_openai):
    """Regression: the chat route must inject the per-provider API key into
    the upstream call. Before the fix, Claude/Gemini always sent an empty
    key (the upstream would 401 in production)."""
    from s_full.services.billing import top_up
    from s_full.models.user import create_user, reset_db
    from s_full.models.channel import create_channel, reset_channels
    from s_full.middleware.auth import issue_token
    import os
    reset_db(); reset_channels()
    uid = create_user("u@x.com", "x")
    top_up("u@x.com", 1_000_000)
    create_channel("c1", "openai", "https://api.openai.com", weight=100, priority=0)
    token = issue_token(uid, "u@x.com", is_admin=False)
    os.environ["UPSTREAM_OPENAI_KEY"] = "sk-test-injected"
    try:
        with TestClient(app) as c:
            r = c.post(
                "/v1/chat/completions",
                headers={"authorization": f"Bearer {token}"},
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert r.status_code == 200
        # The mock captured the inbound request; assert it carried our key.
        sent = upstream_openai.calls.last.request
        assert sent.headers.get("authorization") == "Bearer sk-test-injected"
    finally:
        del os.environ["UPSTREAM_OPENAI_KEY"]


def test_full_relay_claude_path(upstream_claude):
    """End-to-end Claude relay. Before the fix, the route always sent an
    empty x-api-key header, so any real Claude call 401'd. This test fails
    if the Claude branch is missing or the API key env is mis-routed."""
    from s_full.services.billing import top_up
    from s_full.models.user import create_user, reset_db
    from s_full.models.channel import create_channel, reset_channels
    from s_full.middleware.auth import issue_token
    import os
    reset_db(); reset_channels()
    uid = create_user("u@x.com", "x")
    top_up("u@x.com", 1_000_000)
    create_channel("c2", "claude", "https://api.anthropic.com", weight=100, priority=0)
    token = issue_token(uid, "u@x.com", is_admin=False)
    os.environ["UPSTREAM_CLAUDE_KEY"] = "sk-ant-test"
    try:
        with TestClient(app) as c:
            r = c.post(
                "/v1/chat/completions",
                headers={"authorization": f"Bearer {token}"},
                json={"model": "claude-3-5-sonnet-20241022", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["message"]["content"] == "hi from claude"
        sent = upstream_claude.calls.last.request
        assert sent.headers.get("x-api-key") == "sk-ant-test"
    finally:
        del os.environ["UPSTREAM_CLAUDE_KEY"]


def test_injected_log_store_observes_calls(upstream_openai):
    """Replace s_full.models.log._default with a recording fake; the chat
    route should route through our injected store, not the module-level one."""
    from s_full.models import log as s_full_log

    class Recording:
        def __init__(self):
            self.entries: list[dict] = []
        def enqueue(self, entry):
            self.entries.append(entry)
        def list(self):
            return list(self.entries)
        def reset(self):
            self.entries.clear()
        def drain_now(self):
            pass

    from s_full.services.billing import top_up
    from s_full.models.user import create_user, reset_db
    from s_full.models.channel import create_channel, reset_channels
    from s_full.middleware.auth import issue_token
    reset_db(); reset_channels()
    uid = create_user("u@x.com", "x")
    top_up("u@x.com", 1_000_000)
    create_channel("c1", "openai", "https://api.openai.com", weight=100, priority=0)
    token = issue_token(uid, "u@x.com", is_admin=False)

    rec = Recording()
    saved = s_full_log.get_default()
    s_full_log.set_default(rec)
    try:
        with TestClient(app) as c:
            r = c.post(
                "/v1/chat/completions",
                headers={"authorization": f"Bearer {token}"},
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert r.status_code == 200, r.text
        assert any(e.get("model") == "gpt-4o-mini" for e in rec.entries)
    finally:
        s_full_log.set_default(saved)


def test_streaming_passes_through_chunks(upstream_openai_streaming):
    """stream=true relays upstream SSE bytes verbatim and emits
    text/event-stream with the expected framing. Quota is pre-consumed
    then settled; balance must have decreased by some positive amount."""
    from s_full.services.billing import top_up
    from s_full.services.quota import get_balance
    from s_full.models.user import create_user, reset_db
    from s_full.models.channel import create_channel, reset_channels
    from s_full.middleware.auth import issue_token
    reset_db(); reset_channels()
    uid = create_user("u@x.com", "x")
    top_up("u@x.com", 1_000_000)
    create_channel("c1", "openai", "https://api.openai.com", weight=100, priority=0)
    token = issue_token(uid, "u@x.com", is_admin=False)
    balance_before = get_balance(uid)
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {token}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 256,
                "stream": True,
            },
        )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert "data:" in body
    assert "data: [DONE]" in body
    assert '"content":"hi"' in body
    assert '"content":" there"' in body
    assert '"content":"!"' in body
    balance_after = get_balance(uid)
    # We pre-consumed then settled. No per-chunk usage means settle falls
    # back to a reasonable estimate, but the absolute charge depends on
    # tiktoken version. Just assert balance decreased.
    assert balance_after < balance_before


def test_streaming_refunds_when_upstream_reports_usage(upstream_openai_streaming_with_usage):
    """When the LAST data: chunk carries usage.completion_tokens, settle
    refunds the difference between pre-consume and actual. We assert
    the stream completes successfully and balance decreases."""
    from s_full.services.billing import top_up
    from s_full.services.quota import get_balance
    from s_full.models.user import create_user, reset_db
    from s_full.models.channel import create_channel, reset_channels
    from s_full.middleware.auth import issue_token
    reset_db(); reset_channels()
    uid = create_user("u@x.com", "x")
    top_up("u@x.com", 1_000_000)
    create_channel("c1", "openai", "https://api.openai.com", weight=100, priority=0)
    token = issue_token(uid, "u@x.com", is_admin=False)
    balance_before = get_balance(uid)
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {token}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 256,
                "stream": True,
            },
        )
    assert r.status_code == 200
    balance_after = get_balance(uid)
    # We pre-consumed then settled against the upstream-reported usage.
    # Balance must have decreased.
    assert balance_after < balance_before


def test_streaming_gemini_returns_400(respx_mock_gemini):
    """stream=true on a gemini-* model must return 400 because Gemini
    opted out of streaming in the adapter layer. The pre-consume must
    be refunded because the request never reached the upstream."""
    from s_full.services.billing import top_up
    from s_full.services.quota import get_balance
    from s_full.models.user import create_user, reset_db
    from s_full.models.channel import create_channel, reset_channels
    from s_full.middleware.auth import issue_token
    reset_db(); reset_channels()
    uid = create_user("u@x.com", "x")
    top_up("u@x.com", 1_000_000)
    create_channel("c1", "gemini", "https://generativelanguage.googleapis.com", weight=100, priority=0)
    token = issue_token(uid, "u@x.com", is_admin=False)
    balance_before = get_balance(uid)
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {token}"},
            json={
                "model": "gemini-1.5-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert r.status_code == 400
    assert "streaming not supported" in r.text
    # Pre-consume was refunded because the 400 was raised before upstream call.
    balance_after = get_balance(uid)
    assert balance_after == balance_before


def test_quota_settle_charges_overage_bidir():
    """s_full/services/quota.settle must mirror s07: charge the overage
    when actual > pre_deducted (real new-api collects this). Lock-in
    for the bidir refactor — without this, the one-dir version silently
    undercharged users."""
    from s_full.services import quota
    quota.reset()
    quota.set_balance(7, 1_000)
    assert quota.deduct(7, 100) is True
    quota.settle(7, pre_deducted=100, actual=150)
    # 1000 - 100 (pre) - 50 (overage) = 850.
    assert quota.get_balance(7) == 850


def test_billing_settle_uses_upstream_usage_not_pre_consume_floor():
    """billing.settle must use upstream's reported prompt/completion tokens
    directly, NOT floor pt at pre_deducted (the old behavior made actual
    always >= pre_deducted, hiding the overage path)."""
    from s_full.services import quota, billing
    quota.reset()
    quota.set_balance(8, 1_000)
    # Caller pre-consumes the standard estimate.
    quota.deduct(8, 261)  # e.g. 5 prompt + 256 expected
    # Upstream reports a tiny actual usage.
    actual = billing.settle(8, pre_deducted=261, usage={"prompt_tokens": 5, "completion_tokens": 2})
    # 1000 - 261 (pre) + 254 (refund) = 993.
    assert quota.get_balance(8) == 993
    # The function returned the actual cost (= 5 + 2 = 7).
    assert actual == 7


def test_non_stream_502_refunds_when_upstream_returns_malformed_json():
    """C1 regression: upstream returns 200 with body='not json'. The pre-consume
    must be refunded (no silent loss) and the client must get a 502, not a 500."""
    from s_full.services.billing import top_up
    from s_full.services.quota import get_balance
    from s_full.models.user import create_user, reset_db
    from s_full.models.channel import create_channel, reset_channels
    from s_full.middleware.auth import issue_token
    reset_db(); reset_channels()
    uid = create_user("u@x.com", "x")
    top_up("u@x.com", 1_000_000)
    create_channel("c1", "openai", "https://api.openai.com", weight=100, priority=0)
    token = issue_token(uid, "u@x.com", is_admin=False)
    balance_before = get_balance(uid)
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, content=b"not json")
        )
        with TestClient(app) as c:
            r = c.post(
                "/v1/chat/completions",
                headers={"authorization": f"Bearer {token}"},
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
            )
    assert r.status_code == 502, r.text
    # Pre-consume was refunded because the response couldn't be parsed.
    balance_after = get_balance(uid)
    assert balance_after == balance_before


def test_jwt_missing_sub_returns_401_not_500():
    """I1 regression: PyJWT doesn't enforce 'sub'. A hand-rolled token without
    it must surface as 401, not bubble up as a 500 from a KeyError."""
    import jwt
    from s_full.middleware.auth import SECRET
    from s_full.models.user import create_user, reset_db
    from s_full.models.channel import create_channel, reset_channels
    reset_db(); reset_channels()
    uid = create_user("u@x.com", "x")
    create_channel("c1", "openai", "https://api.openai.com", weight=100, priority=0)
    # Token omits 'sub' on purpose; includes a valid exp so PyJWT doesn't reject.
    token = jwt.encode(
        {"email": "u@x.com", "exp": int(time.time()) + 3600},
        SECRET,
        algorithm="HS256",
    )
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {token}"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 401, r.text


def test_billing_settle_returns_pre_deducted_when_partial_usage():
    """I2 regression: when upstream returns only prompt_tokens (no completion),
    billing.settle must return pre_deducted (treat the usage report as
    incomplete) — no refund, no extra charge."""
    from s_full.services import quota, billing
    quota.reset()
    quota.set_balance(9, 1_000)
    quota.deduct(9, 261)
    # 'completion_tokens' deliberately absent. Old behavior refunded (pt or 0) * RATE
    # = 5, charging the user only 5. New behavior keeps the full pre-consume.
    actual = billing.settle(9, pre_deducted=261, usage={"prompt_tokens": 5})
    assert actual == 261
    assert quota.get_balance(9) == 1_000 - 261  # no refund


def test_streaming_429_refunds_estimate():
    """I3 regression: streaming upstream returning a 4xx/5xx with empty body
    must NOT silently charge the user. Pre-consume is refunded (the user got
    no content)."""
    from s_full.services.billing import top_up
    from s_full.services.quota import get_balance
    from s_full.models.user import create_user, reset_db
    from s_full.models.channel import create_channel, reset_channels
    from s_full.middleware.auth import issue_token
    reset_db(); reset_channels()
    uid = create_user("u@x.com", "x")
    top_up("u@x.com", 1_000_000)
    create_channel("c1", "openai", "https://api.openai.com", weight=100, priority=0)
    token = issue_token(uid, "u@x.com", is_admin=False)
    balance_before = get_balance(uid)
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(429, content=b"", headers={"content-type": "text/event-stream"})
        )
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post(
                "/v1/chat/completions",
                headers={"authorization": f"Bearer {token}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 256,
                    "stream": True,
                },
            )
    # The stream raised before yielding any chunk, so the client got no content.
    # (Starlette's StreamingResponse sends headers before iterating, so the
    # status code may already be 200 by the time the HTTPException raises —
    # but no body bytes are delivered either way.)
    assert r.text == "", r.text
    # Critical: pre-consume refunded because the user got no response.
    balance_after = get_balance(uid)
    assert balance_after == balance_before
