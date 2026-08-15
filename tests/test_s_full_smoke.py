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
