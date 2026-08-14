# s01: Minimal Relay Kernel

> Previous: — · Next: [s02](../s02_openai_protocol/)
> **Adds**: an HTTP forwarder — one route that passes a JSON body to a single upstream and returns the reply.

New dependencies: `fastapi`, `uvicorn`, `httpx`, `pydantic`.

## The Problem

You have an app that needs to talk to an LLM provider. Without a gateway, every
caller holds the provider key, hard-codes the provider URL, and duplicates the
same request plumbing. Changing providers, rotating a key, or watching traffic
means editing every caller. That is a manual copy-paste relay done by humans —
it does not scale past the first afternoon.

The fix is a program that sits in the middle. Everything else in this tutorial —
protocols, streaming, auth, quota, logging — is built on top of that one idea,
so we start with the smallest version that actually runs.

## The Solution

A single process that accepts a request, sends it onward, and gives back the
answer. Conceptually a `while True` loop over inbound HTTP requests, where the
body of the loop is "forward it".

![architecture](images/architecture.svg)

```
Client  ──POST /relay──▶  Relay  ──POST FORWARD_TARGET──▶  Upstream
        ◀──── JSON ────          ◀──────── JSON ─────────
```

The relay adds exactly two things at this stage: it owns the upstream URL, and
it owns the upstream key. Callers need to know neither.

## How It Works

The whole kernel is three pieces.

**1. A liveness route.** Cheap, dependency-free, and used by every later chapter
(and by Docker's healthcheck in s15):

```python
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
```

**2. A request shape.** Pydantic validates the body before we spend a network
round trip on it. At this stage we only insist on `model` and `messages`;
s02 makes it a real OpenAI schema:

```python
class RelayRequest(BaseModel):
    model: str
    messages: list[dict]
```

**3. The forwarder.** `httpx.AsyncClient` is the async counterpart to
`requests`. Async matters here: the relay spends nearly all of its wall clock
waiting on the upstream, so a blocking client would pin one OS thread per
in-flight call and cap throughput almost immediately.

```python
async def forward_request(target_url: str, payload: dict) -> dict:
    headers = {"Authorization": f"Bearer {UPSTREAM_KEY}"} if UPSTREAM_KEY else {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(target_url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()
```

Line by line:

- `headers = ... if UPSTREAM_KEY else {}` — the relay injects the provider key.
  The caller never sees it. This is the single most important reason a gateway
  exists.
- `timeout=30.0` — never inherit an unbounded default. A hung upstream must not
  become a hung relay.
- `except httpx.HTTPError` → **502**. A transport failure is *our* upstream's
  fault, not the caller's, and 502 Bad Gateway says exactly that.
- `if r.status_code >= 400` → pass the upstream status through unchanged. If
  OpenAI says 429, the caller should see 429, not a laundered 500.
- The `async with` block closes the client (and its connection pool) on every
  request. Simple, and deliberately wasteful — s10 fixes it with a shared pool.

The route itself is then one line:

```python
@app.post("/relay")
async def relay(req: RelayRequest) -> dict:
    return await forward_request(FORWARD_TARGET, req.model_dump())
```

Configuration is environment-driven so no chapter ever needs a code edit to
point elsewhere:

```python
PORT           = int(os.getenv("PORT", "8001"))
FORWARD_TARGET = os.getenv("FORWARD_TARGET", "https://api.openai.com/v1/chat/completions")
UPSTREAM_KEY   = os.getenv("UPSTREAM_OPENAI_KEY", "")
```

## Run It

```sh
cd s01_minimal_relay
PORT=8001 python code.py
```

Check it is alive:

```sh
curl http://localhost:8001/health
# {"status":"ok"}
```

Relay a request (export `UPSTREAM_OPENAI_KEY` first for a real reply; without a
key the upstream answers 401 and you will see that status pass straight
through, which is the behaviour we want):

```sh
curl -X POST http://localhost:8001/relay \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

## Tests

```sh
pytest tests/test_s01_minimal_relay.py -v
```

The upstream is mocked with `respx` (the `upstream_openai` fixture in
`tests/conftest.py`), so the suite runs offline and still asserts the real wire
shape: the relay must return the upstream's `choices[0].message.content` and
must call the upstream exactly once.

## → new-api source

| Here | new-api |
|---|---|
| `relay()` route | `relay/relay.go` — the entry point that dispatches an inbound request to an upstream |
| `forward_request()` | `relay/channel/openai/adaptor.go` — the per-channel adapter that builds the outbound request and parses the reply |

new-api generalises the single function above into an `Adaptor` interface with
one implementation per provider. We arrive at the same design in s04.

## Trade-offs

What we deliberately did **not** do:

- **No auth.** Anyone who can reach the port can spend your key. → s05.
- **No streaming.** `r.json()` waits for the complete body, so token-by-token
  output is impossible. → s03.
- **Single upstream.** One `FORWARD_TARGET`, no channel table, no weights, no
  failover. → s10, s13.
- **No protocol translation.** The body is forwarded verbatim, so the caller
  must already speak the upstream's dialect. → s02, s04.
- **No quota, logging, or metrics.** → s07, s11, s16.
- **A fresh connection pool per request.** Correct but slow; a long-lived
  client is the production answer.
