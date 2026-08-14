# learn-new-api — Design Spec

**Date**: 2026-08-14
**Status**: Approved
**Repository**: `D:\study\learning_serial\learn-new-api\` (new, sibling of `learn-claude-code`)

## Goal

Build a tutorial project that teaches the reader how to **build their own AI API gateway**, by reading and running progressively richer Python examples. The model is `learn-claude-code`'s: each chapter is a self-contained, runnable demo that adds exactly one capability on top of the previous one. Every chapter also points to the corresponding real code in `new-api/` so readers can cross-reference their toy implementation with the production-grade Go codebase.

The reader finishes the tutorial with:

1. A working minimal API gateway they can run locally with `python s_full.py`.
2. Mental model of how a real gateway (`new-api`) is organized and why.
3. Enough vocabulary and entry points that they can confidently read `new-api`'s Go source.

## Non-Goals (YAGNI for v1)

To keep scope tight, the following real `new-api` features are **out of scope** for v1 and explicitly deferred to a future v2:

- Multi-database support (SQLite/MySQL/PostgreSQL) — tutorial uses SQLite only.
- OAuth / WebAuthn / Passkey — replaced by simple email + bcrypt + JWT.
- Backend i18n (go-i18n) — Chinese strings only.
- Billing expression engine (`pkg/billingexpr`) — flat rate per model.
- Task / async systems (video, TTS, image gen) — covers text chat relay only.
- Full React admin UI — replaced by a minimal FastAPI server-rendered CRUD page.
- Reseller / distribution system.
- Per-channel custom pricing overrides.

If a contributor wants to extend the tutorial into any of these, they can do so without disturbing the v1 chapter graph.

## Audience

A developer who already knows Python basics and has hit an LLM API a few times. They want to understand how `new-api` works under the hood by building a working toy, not by reading Go source cold. They are not assumed to know Gin, GORM, Redis internals, or SSE wire format — those are taught just in time.

## Tech Stack

- **Language**: Python 3.11+
- **Web framework**: FastAPI + Uvicorn
- **HTTP client**: `httpx` (async, for both upstream calls and TestClient)
- **Validation**: Pydantic v2
- **Token counting**: `tiktoken` (with character-based fallback for non-OpenAI tokenizers)
- **Cache / blackboard**: `redis-py`
- **Auth**: `PyJWT`, `bcrypt`
- **Observability**: `prometheus_client`, `structlog`
- **Tests**: `pytest`, `pytest-asyncio`, `respx` (mock upstream HTTP)

## Project Layout

```
learn-new-api/
├── README.md                       # Project overview + reader path
├── docs/                           # Architecture overviews, glossary
│   └── superpowers/specs/          # This file lives here
├── s01_minimal_relay/
│   ├── README.md                   # Problem / Solution / How / Run / Tests / Ref-to-newapi
│   ├── code.py                     # ~30-50 line minimum kernel
│   └── images/                     # Architecture diagrams (svg preferred)
├── s02_openai_protocol/
├── ...
├── s16_observability/
├── s_full/                         # Final integrated version, organized for real use
│   ├── code.py
│   └── README.md
├── tests/
│   ├── conftest.py                 # Shared fixtures (test_app, mock_upstreams)
│   └── test_sNN_*.py               # One file per chapter
├── agents/                         # Optional: agent-builder scaffolds (future)
├── skills/                         # Optional: tutorial-specific skills (future)
├── .env.example
├── requirements.txt
├── makefile                        # run-sNN, test-sNN, all
└── .gitignore
```

Each chapter directory is fully self-contained. Readers can `cd sNN_topic && python code.py` to run that chapter alone; ports increment (8001, 8002, …) to avoid collisions when comparing two chapters side by side.

## Chapter Outline

| #  | Title                              | New concept added                                   | new-api source pointer |
|----|------------------------------------|------------------------------------------------------|------------------------|
| 01 | Minimal relay kernel               | HTTP forwarding, header passthrough, timeouts        | `relay/relay.go` |
| 02 | OpenAI-compatible protocol         | `/v1/chat/completions` route, JSON schema            | `relay/channel/openai/` |
| 03 | Streaming responses (SSE)          | `StreamingResponse`, chunk framing, backpressure     | `relay/sse.go` |
| 04 | Multi-provider adapters            | Claude / Gemini format conversion, `Provider` ABC    | `relay/channel/{claude,gemini}/` |
| 05 | API key auth middleware            | Bearer token, Redis blocklist                        | `middleware/Auth.go` |
| 06 | Token counting                     | `tiktoken`, char-based fallback                      | `service/TokenCalculate.go` |
| 07 | Pre-consume & settle               | Quota table, transactional deduction, refund delta   | `service/PreConsumeQuota.go` |
| 08 | Rate limiting                      | Token bucket, per-user / per-key                     | `middleware/RateLimit.go` |
| 09 | User system                        | Signup / login / JWT / bcrypt                        | `controller/User.go`, `service/User.go` |
| 10 | Channel management                | Multi-channel, weights, health check                 | `controller/Channel.go`, `model/Channel.go` |
| 11 | Call logs & statistics             | Async write, per-user / per-model aggregation        | `model/Log.go`, `service/LogInfoGenerate.go` |
| 12 | Response cache + Redis             | Exact cache + semantic-cache placeholder             | `pkg/cachex`, `common/Redis.go` |
| 13 | Retry / circuit break / fallback   | Per-channel retry, priority-based failover           | `service/ChannelSelect.go` |
| 14 | Minimal admin dashboard            | Server-rendered CRUD via Jinja2 templates             | `web/` (minimal subset) |
| 15 | Deployment (Docker + .env)         | Dockerfile, docker-compose, healthcheck              | `Dockerfile`, `docker-compose.yml` |
| 16 | Observability                      | Prometheus `/metrics`, JSON logs, `trace_id`         | `logger/`, `pkg/perf_metrics/` |
| full | Full integration                  | All of the above, production-style layout            | Whole `new-api/` repo |

Each chapter has exactly one new concept. The previous chapter's code is the starting point; the chapter adds the concept and explains why.

## Per-Chapter README Template

```markdown
# sNN: <Title>

> Previous: [sNN-1](../sNN-1_topic/) · Next: [sNN+1](../sNN+1_topic/)
> **Adds**: <one-sentence summary of the new capability>

## The Problem
<Why this chapter exists — what breaks or is missing without it>

## The Solution
<Top-level approach, with ASCII or SVG diagram in images/>

## How It Works
<Key code snippets from code.py, with line-by-line commentary>

## Run It
<Copy-pasteable commands: `python code.py`, then a curl or browser screenshot>

## Tests
<`pytest tests/test_sNN_*.py -v`>

## → new-api source
<File paths in new-api/ that implement the same idea for real>

## Trade-offs
<What we deliberately did NOT do and why>
```

This template mirrors `learn-claude-code/sNN_*/README.md` so the two tutorial repos feel like siblings.

## Testing Strategy

- One `tests/test_sNN_*.py` per chapter.
- `conftest.py` provides:
  - `test_app` — a FastAPI `TestClient` constructed from the chapter's `app`.
  - `mock_openai`, `mock_claude`, `mock_gemini` — `respx` routes that mimic upstream wire format, returning canned completions or SSE streams.
- Each test asserts **observable contract**, not implementation details:
  - Request reaches the right provider route.
  - Response shape matches the contract.
  - Quota / log side-effects happen (or don't) when expected.
  - Auth / rate-limit / cache decisions are honored.
- New tests **must use `pytest` with explicit fixtures**; no random fuzzing, no timing assertions, no coverage-only tests.
- Tests are runnable independently: `pytest tests/test_sNN_quota.py`.

## Diagrams

- Each chapter has at least one diagram: ASCII inline for fast reading, SVG in `images/` for sharing.
- Diagrams cover: component map, request flow (sequence), state transitions (rate limit bucket, billing state machine).
- Style guide: monochrome, monospaced when ASCII; SVG keeps stroke width consistent, no decorative shadows.

## Dependencies Per Chapter

| Chapter | New deps added |
|---------|----------------|
| 01      | fastapi, uvicorn, httpx |
| 02      | pydantic |
| 03      | (uses stdlib `sse-starlette`) |
| 04      | (uses httpx) |
| 05      | pyjwt |
| 06      | tiktoken |
| 07      | (uses sqlite stdlib) |
| 08      | (uses in-memory bucket; redis comes in 12) |
| 09      | bcrypt |
| 10      | (uses sqlite) |
| 11      | (uses sqlite) |
| 12      | redis |
| 13      | (uses httpx + tenacity) |
| 14      | jinja2 |
| 15      | (docker only) |
| 16      | prometheus_client, structlog |

Each chapter README lists only its new dependencies at the top, so readers can `pip install` incrementally.

## Configuration & Secrets

`.env.example` documents every variable the tutorial uses:

```
UPSTREAM_OPENAI_KEY=
UPSTREAM_CLAUDE_KEY=
UPSTREAM_GEMINI_KEY=
JWT_SECRET=change-me
REDIS_URL=redis://localhost:6379/0
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=admin
LOG_LEVEL=INFO
```

Chapters that introduce a new variable explain it; chapters that don't must not.

## Reader Outcomes

After completing all 16 chapters plus `s_full`, the reader can:

1. Run a real multi-provider relay against their own API keys.
2. Read `new-api/relay/channel/openai/adaptor.go` and recognize the pattern from chapter 02–04.
3. Add a new provider by writing one adapter file.
4. Explain why quota pre-consumption is necessary (chapter 07) and what would break without it.
5. Stand up a deployable service with `docker compose up` (chapter 15).
6. Hook up Prometheus + Grafana to monitor it (chapter 16).

## Out-of-Scope Risks

- **Scope creep**: 16 chapters is a lot. If we slip, defer chapters 11–14 to v1.5 rather than letting all chapters become shallow.
- **Upstream drift**: OpenAI / Anthropic / Google APIs evolve. Pin to a known-good schema per chapter and call out the version. Avoid hardcoding fields that frequently move.
- **Go ↔ Python impedance**: The mapping from Python examples to Go source is approximate. Each "→ new-api source" link must be re-verified after major new-api releases.

## Open Questions

None at design time. The implementation plan will surface concrete questions (e.g., where to draw the line between "minimal" and "useful" in chapter 14's admin UI).