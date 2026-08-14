# s02: OpenAI Protocol

> Previous: [s01](../s01_minimal_relay/) · Next: [s03](../s03_streaming_sse/)
> **Adds**: the inbound surface adopts OpenAI's `/v1/chat/completions` path and JSON schema. Any OpenAI client (the official SDK, LangChain, LlamaIndex, your terminal `curl`) now works against us unchanged.

## The Problem

`s01` answered a custom URL (`/relay`) with a custom JSON shape. Every client
had to learn our dialect. That is the wrong end of the deal: the LLM ecosystem
already speaks OpenAI's `chat.completions` contract, and re-teaching the world
our bespoke format is a non-starter.

A gateway that targets OpenAI's surface gets every existing client for free.

## The Solution

Two adjustments and zero new infrastructure:

1. **Rename the route** from `/relay` to `/v1/chat/completions` — the path
   OpenAI exposes, the path every client already knows.
2. **Tighten the request schema** to mirror OpenAI's payload: `model`,
   `messages: [{role, content}, ...]` (with `min_length=1`), plus optional
   `temperature`, `max_tokens`, `stream`. Anything else is left for the
   upstream to reject — the relay does not invent fields.

The forwarding loop is byte-identical. The only thing that changed is *what
we call ourselves* on the way in.

```
Client ──POST /v1/chat/completions──▶  OpenAI-shaped API  ──POST FORWARD_TARGET──▶  Upstream
        ◀────── JSON ────────────                         ◀──────── JSON ───────────
```

![architecture](images/architecture.svg)

## How It Works

The Pydantic model carries the schema; the handler marshals it through the
shared `common/json` helpers so the JSON round-trip obeys the same rule as
every other chapter:

```python
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
```

The route is just `s01`'s relay with a new URL and a typed body. `model_dump(exclude_none=True)` strips the optional fields before serialisation, so callers that omit `temperature` don't pay for an empty JSON key on the wire:

```python
@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(req: ChatCompletionRequest) -> dict:
    headers = {"Authorization": f"Bearer {UPSTREAM_KEY}"} if UPSTREAM_KEY else {}
    body = marshal(req.model_dump(exclude_none=True))
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(
                FORWARD_TARGET, content=body, headers={**headers, "content-type": "application/json"}
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return unmarshal_str(r.text, ChatCompletionResponse).model_dump()
```

`marshal` and `unmarshal_str` from `common/json.py` are the only JSON entry
points business code is allowed to use: `marshal` produces compact UTF-8 bytes
(no spaces, `ensure_ascii=False`), and `unmarshal_str` parses the wire body
through a Pydantic model so response validation is enforced at the boundary.
Keeping these in one module mirrors new-api's `common/json.go` rule.

Why `exclude_none=True`? OpenAI's API treats absent optional fields as
"server default". Sending `temperature: null` is a different request — it
forces `null` and bypasses the upstream default. Stripping the field preserves
the caller's intent.

## Run It

```sh
cd s02_openai_protocol
PORT=8002 python code.py
```

Check it is alive:

```sh
curl http://localhost:8002/health
# {"status":"ok"}
```

Relay a request (export `UPSTREAM_OPENAI_KEY` first for a real reply; without
a key the upstream returns 401 and you will see that status pass straight
through, which is the behaviour we want):

```sh
curl -X POST http://localhost:8002/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

## Tests

```sh
pytest tests/test_s02_openai_protocol.py -v
```

The upstream is mocked with `respx` (the `upstream_openai` fixture in
`tests/conftest.py`), so the suite runs offline and still asserts the real
wire shape. The two tests cover:

- `test_openai_route_exists` — a valid payload is relayed and returns a body
  with a `choices` key.
- `test_request_validation_rejects_missing_messages` — omitting `messages`
  is rejected with `422` at the Pydantic boundary, before any network call.

## → new-api source

| Here | new-api |
|---|---|
| `ChatCompletionRequest` model | `relay/channel/openai/adaptor.go` — request/response DTO conversion between OpenAI's wire format and the internal `relay` struct |
| `chat_completions` route | `relay/relay.go` — dispatches the inbound request to the OpenAI `Adaptor` |
| `model_dump(exclude_none=True)` | `relay/constant.go` — the per-channel normaliser that drops empty fields before forwarding |

new-api generalises this pattern into an `Adaptor` interface
(`relay/channel/openai/adaptor.go`) with one implementation per provider. We
arrive at the same design in s04.

## Trade-offs

What we deliberately did **not** do:

- **No translation for Claude / Gemini**. The body is still OpenAI-shaped, so
  a Claude-style `system` block or Gemini's `contents` array would be
  forwarded verbatim and rejected by the upstream. → s04.
- **No streaming**. `r.json()` waits for the complete body, so token-by-token
  output is impossible. → s03.
- **No auth, quota, logging, or metrics.** → s05, s07, s11, s16.
- **A fresh connection pool per request.** Correct but slow; a long-lived
  client is the production answer. → s10.
- **No retries or backoff.** A transient upstream hiccup surfaces as a 502
  to the caller. → s13.
