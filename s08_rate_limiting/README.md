# s08: Per-User Token Bucket Rate Limiting

## Problem

Quota controls *cost* — a user with plenty of balance can still flood the
upstream and starve everyone else. A single noisy caller pounding
`/v1/chat/completions` 100× per second degrades latency for every other
tenant on the same proxy. Quota says "this user can spend"; we also need
"this user can spend at *this* rate".

## Solution

A **token bucket per user** sits in front of the handler. Each user has
`capacity` tokens that refill at `refill_per_sec`. Each request consumes
one token; if none are left the request is rejected with `429 Too Many
Requests` *before* any quota deduction or upstream call.

Defaults: 60-token burst, 1 token / second refill — so a fresh user can
send a burst of 60, then sustains roughly one request per second.

## How It Works

`s08_rate_limiting/bucket.py` exposes the bucket:

- `reset_buckets()` — clear all buckets (tests)
- `configure(user_id, capacity, refill_per_sec)` — set per-user limits
  and prime the bucket to full
- `take(user_id, cost=1.0) -> bool` — atomic: refill based on elapsed
  time, check against `cost`, decrement on success, return `False` if
  exhausted

The whole check-and-decrement happens under one `threading.Lock`, so
concurrent requests from the same user cannot over-spend tokens.

`s08_rate_limiting/code.py` wires the bucket into the handler:

```python
@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, p: Principal = Depends(require_api_key)):
    if not take(p.user_id):
        raise HTTPException(status_code=429, detail="rate limited")
    # ... pre-deduct estimate, call upstream, settle as in s07 ...
```

Order of checks matters: auth → rate limit → quota deduct → upstream.
A user without a valid key never reaches the bucket, and a user with
plenty of tokens but no quota gets `402` instead of `429`.

## Run It

```python
from s05_api_key_auth.storage import register_key
from s07_pre_consume_settle.quota import set_balance
from s08_rate_limiting.bucket import configure

register_key("sk-u", "u1")
set_balance("u1", 10_000_000)
configure("u1", capacity=60, refill_per_sec=1.0)  # default
```

```bash
python s08_rate_limiting/code.py
```

```bash
# burst of three — third gets 429 if the bucket only had 2 tokens
for i in 1 2 3; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8008/v1/chat/completions \
    -H 'authorization: Bearer sk-u' \
    -H 'content-type: application/json' \
    -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
done
```

`PORT` (default 8008) and `RATE_PER_TOKEN` (default 1) are overridable
via env.

## Tests

```bash
pytest tests/test_s08_rate_limiting.py -v
```

One test covers the contract:

| Test | What it asserts |
| --- | --- |
| `test_first_two_pass_third_blocked` | With `capacity=2, refill_per_sec=0`, the first two requests pass (200) and the third is rejected (429). |

## new-api Source

- `middleware/RateLimit.go` — middleware that applies per-user limits
  using Redis-backed counters; here we inline it as a function call so
  the contract is obvious.

## Trade-offs

- **In-memory bucket is per-process.** Each worker has its own
  counters, so under multi-worker deployments a user can effectively
  get `N_workers × capacity` burst. s12 moves the bucket into Redis
  with `INCR` + `EXPIRE` so all workers share state.
- **Default limits are global.** Production reads per-user limits from
  a database (tier, channel, plan). This chapter hard-codes 60/1 — the
  point is the algorithm, not the policy.
- **No `Retry-After` header.** Real impls add it so polite clients
  back off; we keep the response body minimal.
- **Per-user, not per-token.** Rate limits apply to the API key holder,
  not per upstream model. Multi-tenant model-level quotas would split
  the bucket by `(user_id, model)`.