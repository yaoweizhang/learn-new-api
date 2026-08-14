# s07: Pre-consume and Settle

## Problem

A user with 100 tokens of quota could submit 100 parallel requests of 100
tokens each. If every request estimates 100 tokens before calling upstream,
all 100 requests get past the gate and the user blows through their balance
on the actual cost, which we only learn about after the upstream reply
arrives. The user could go **negative** mid-call — and the next request
that legitimately has quota gets rejected because the balance already
underflowed.

## Solution

Pre-deduct an *estimate* before forwarding to the upstream. When the real
reply comes back, settle the difference:

1. **Pre-deduct estimate** (prompt tokens + expected completion × rate).
   Fail with `402 Payment Required` if the balance is insufficient.
2. **Call upstream.**
3. **On success, settle:** refund `(estimate - actual)` if the actual
   usage is lower than the estimate.
4. **On upstream failure (network error, 4xx, 5xx):** refund the **full**
   pre-deduct so the user is not charged for a request that never
   succeeded.

The deduction is atomic under a `threading.Lock`, so concurrent requests
from the same user can never double-spend.

## How It Works

Quota math:

```
RATE = 1 quota per token (configurable; flat rate per chapter)
estimate = (prompt_tokens + expected_completion) * RATE_PER_TOKEN
```

`s07_pre_consume_settle/quota.py` exposes the store:

- `reset()` — clear all balances (tests)
- `set_balance(user_id, amount)` — set the balance for a user
- `get_balance(user_id) -> int` — read balance
- `deduct(user_id, amount) -> bool` — atomic conditional deduction;
  returns `False` if the balance is insufficient (no partial deduct)
- `refund(user_id, amount)` — add quota back (for failure recovery)
- `settle(user_id, pre_deducted, actual) -> int` — refund the diff and
  return the actual amount charged

`s07_pre_consume_settle/code.py` wires the math into the FastAPI handler:

```
estimate = (prompt_tokens + expected_completion) * RATE_PER_TOKEN
if not deduct(principal.user_id, estimate):
    raise HTTPException(402, "insufficient quota")

try:
    r = await client.post(upstream_url, ...)
except httpx.HTTPError:
    refund(principal.user_id, estimate)        # network failure
    raise HTTPException(502, "upstream error")

if r.status_code >= 400:
    refund(principal.user_id, estimate)        # upstream returned an error
    raise HTTPException(r.status_code, r.text)

# success path — refund the diff
pt = max(usage.prompt_tokens, prompt_tokens)
ct = usage.completion_tokens
actual = (pt + ct) * RATE_PER_TOKEN
settle(principal.user_id, estimate, actual)
```

## Run It

```python
from s05_api_key_auth.storage import register_key
from s07_pre_consume_settle.quota import set_balance

register_key("sk-u", "u1")
set_balance("u1", 10_000)
```

```bash
python s07_pre_consume_settle/code.py
```

```bash
curl http://localhost:8007/quota/u1                                  # {"balance": 10000}
curl -X POST http://localhost:8007/v1/chat/completions \
  -H 'authorization: Bearer sk-u' \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
curl http://localhost:8007/quota/u1                                  # {"balance": 9970-ish}
```

`RATE_PER_TOKEN` (default 1) and `PORT` (default 8007) are overridable via env.

## Tests

```bash
pytest tests/test_s07_pre_consume_settle.py -v
```

Three tests cover the contract:

| Test | What it asserts |
| --- | --- |
| `test_pre_consume_deducts_before_call` | A successful call deducts something from the balance. |
| `test_insufficient_quota_returns_402` | A user with 0 quota gets `402 Payment Required`. |
| `test_upstream_failure_refunds_pre_consume` | When upstream returns 500, the full pre-deduct is refunded. |

## new-api Source

- `service/PreConsumeQuota.go` — pre-deduct / settle logic.
- `model/Quota.go` — the Quota struct and per-user counters.

## Trade-offs

- **In-memory storage.** State dies with the process; s09 introduces SQLite
  with transactional deduction.
- **No quota refresh / top-up.** Quota lives in the dict forever until the
  service restarts; production uses Redis + a periodic refill cron.
- **Estimate is generous.** We pre-deduct for `expected_completion = 256`
  tokens when `max_tokens` is unset. Most replies are shorter, so users
  routinely get small refunds. If they consistently hit the ceiling,
  switch to per-channel rate cards in a later chapter.
- **No idempotency key.** A client that retries on timeout can be double-
  charged (deducted twice, settled once). s10 adds `Idempotency-Key`.
