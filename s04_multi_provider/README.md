# s04: Multi-Provider Adapter Dispatch

> Previous: [s03](../s03_streaming_sse/) · Next: [s05](../s05_api_key_auth/)
> **Adds**: the same OpenAI-shaped client can now reach Claude or Gemini, because the relay picks a `Provider` implementation from the model's name and translates both directions (request in, response out).

## The Problem

`s02` and `s03` forward an OpenAI-shaped JSON body verbatim. That works fine as long as the upstream *is* OpenAI — but the body shape that OpenAI expects (`model`, `messages`, `temperature`, optional `stream`) is not what Anthropic or Google expect. Claude wants `x-api-key`, an `anthropic-version` header, and a `max_tokens` field on every request. Gemini wants a `contents` array of `{role, parts:[{text}]}` and an API key in the URL query string.

If we forwarded OpenAI JSON to Claude, the upstream would reply with `400 invalid request`; if we forwarded to Gemini, we'd get back the same. One client, one wire format, three incompatible upstreams — that is the problem s04 solves.

## The Solution

Introduce a `Provider` abstract base class with one concrete implementation per upstream. Each provider knows two things:

1. **How to translate an OpenAI request into its own wire format** (`to_upstream`).
2. **How to translate its own response back into an OpenAI-shaped response** (`from_upstream`).

The route handler asks `pick_provider(model)` for the right adapter by looking at the model's name prefix (`gpt-`/`o` → OpenAI, `claude-` → Claude, `gemini-` → Gemini), then forwards the request through that adapter. The client sees the same `/v1/chat/completions` surface and the same JSON shape regardless of which upstream ends up answering.

```
Client ──POST /v1/chat/completions──▶  Relay (pick by model prefix)  ──POST upstream──▶  Provider
        ◀────── OpenAI JSON ─────────                                    ◀──── JSON ────
```

![architecture](images/architecture.svg)

## How It Works

The adapter table is the heart of the chapter:

| Model prefix | Provider | Upstream URL |
|---|---|---|
| `gpt-` or `o` | `OpenAIProvider` | `https://api.openai.com/v1/chat/completions` |
| `claude-` | `ClaudeProvider` | `https://api.anthropic.com/v1/messages` |
| `gemini-` | `GeminiProvider` | `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=…` |
| anything else | — | `400 unknown model` |

Each provider translates *only what differs* — common fields (`model`, `messages`) flow through unchanged; provider-specific fields (`system`, `max_tokens` for Claude; `contents` shape for Gemini) are constructed explicitly. Responses are folded back into the OpenAI `chat.completion` shape so the client never has to know which upstream answered:

```python
class Provider(ABC):
    name: str

    @abstractmethod
    def to_upstream(self, req: dict) -> tuple[str, dict, dict]: ...

    @abstractmethod
    def from_upstream(self, payload: dict) -> dict: ...


def pick_provider(model: str) -> Provider:
    if model.startswith("gpt-") or model.startswith("o"):
        return OpenAIProvider()
    if model.startswith("claude-"):
        return ClaudeProvider()
    if model.startswith("gemini-"):
        return GeminiProvider()
    raise ValueError(f"unknown model: {model}")
```

The route handler stays almost identical to s02's — the only new lines are `pick_provider(req.model)` and the `provider.to_upstream` / `provider.from_upstream` calls around the existing `httpx.AsyncClient.post`:

```python
@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    try:
        provider = pick_provider(req.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    payload = req.model_dump(exclude_none=True)
    payload["_api_key"] = _key_for(provider.name)
    url, headers, upstream_body = provider.to_upstream(payload)
    body_bytes = marshal(upstream_body)
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(url, content=body_bytes, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    translated = provider.from_upstream(json.loads(r.text))
    return JSONResponse(translated)
```

API keys come from per-provider environment variables (`UPSTREAM_OPENAI_KEY`, `UPSTREAM_CLAUDE_KEY`, `UPSTREAM_GEMINI_KEY`) resolved through `_key_for(provider.name)` and injected into the payload's `_api_key` slot before the adapter sees it. The underscore prefix keeps the field out of the serialized wire body.

## Run It

```sh
cd s04_multi_provider
PORT=8004 python code.py
```

Health:

```sh
curl http://localhost:8004/health
# {"status":"ok"}
```

Three providers, one request shape (set the matching `UPSTREAM_*_KEY` for a real reply; without one the upstream will 401 and the relay will pass that through, which is the behaviour we want):

```sh
# OpenAI
curl -X POST http://localhost:8004/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'

# Claude
curl -X POST http://localhost:8004/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"claude-3-5-sonnet-20241022","messages":[{"role":"user","content":"hi"}]}'

# Gemini
curl -X POST http://localhost:8004/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gemini-1.5-flash","messages":[{"role":"user","content":"hi"}]}'
```

## Tests

```sh
pytest tests/test_s04_multi_provider.py -v
```

All three providers are mocked with `respx` (each test gets the same `three_upstreams` fixture, which mounts mocks for all three upstreams so a single test run exercises the whole dispatch table):

- `test_routes_openai` — `model: gpt-4o-mini` is routed to OpenAI and the relay returns `openai-ok`.
- `test_routes_claude` — `model: claude-3-5-sonnet-20241022` is routed to Anthropic and the response is translated back into OpenAI shape with `claude-ok` in `choices[0].message.content`.
- `test_routes_gemini` — `model: gemini-1.5-flash` is routed to Google's endpoint and returns `gemini-ok`.
- `test_unknown_model_rejected` — `model: mystery-7` fails `pick_provider` and returns `400`.

## → new-api source

| Here | new-api |
|---|---|
| `Provider` ABC | `relay/channel/adaptor.go` — the `Adaptor` interface that every channel implements |
| `OpenAIProvider` | `relay/channel/openai/adaptor.go` — OpenAI-specific request/response conversion |
| `ClaudeProvider` | `relay/channel/claude/adaptor.go` — Anthropic Messages conversion |
| `GeminiProvider` | `relay/channel/gemini/adaptor.go` — Google `generateContent` conversion |
| `pick_provider(model)` | `relay/relay.go` — dispatches an inbound request to the right channel by inspecting the model name |

new-api takes this further with a `GetAdaptor(meta)` factory that maps a `(channel, model)` tuple to an adaptor instance, plus a per-channel `Key` mode (the `_*_KEY` envvars we hardcode here become runtime-configurable). The Go side also has streaming adapters for every provider — see the trade-off below.

## Trade-offs

What we deliberately did **not** do:

- **No streaming translation.** When `stream: true` is sent, we still wait for the full response and return JSON. The three providers' SSE wire formats differ mid-stream (OpenAI sends `data: {...}\n\n`, Claude sends `event: …` lines, Gemini sends `data: [array,…]`), so a real streaming translator is its own design problem. → s05+.
- **No real `system` translation for OpenAI.** OpenAI clients can pass `system` in `messages` (`{"role": "system", "content": "…"}`); Claude wants a top-level `system` field. The adapter does the top-level `system` lift; the `system`-in-`messages` variant is not yet handled.
- **Routing by prefix is brittle.** A model called `open-mistral-7b` (real Mistral model name) would match `o` and hit the OpenAI provider — which would then 401 or 400. new-api solves this by routing on `channel`, not on `model`, so the operator declares "this model goes to Anthropic" at config time.
- **A fresh connection pool per request.** Correct but slow; a long-lived client is the production answer. → s10.
- **No retries or backoff.** → s13.