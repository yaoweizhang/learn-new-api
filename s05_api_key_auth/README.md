# s05: API Key Authentication

> Previous: [s04](../s04_multi_provider/) · Next: [s06](../s06_token_counting/)
> **Adds**: every request to `/v1/chat/completions` must carry a valid API key in `Authorization: Bearer <key>`. Unknown, missing, or blocked keys are rejected with `401`.

## The Problem

s01–s04 happily forward any request that looks like a chat completion. There is no concept of "who" is making the call: anyone who can reach the relay can burn the upstream quota, and there is no place to attach per-user rate limits, billing, or scopes. The relay is wide open.

## The Solution

Introduce a `Principal` (a `user_id` plus a tuple of `scopes`) and a `Depends(require_api_key)` dependency that runs before the chat-completion handler. The dependency:

1. Reads `Authorization: Bearer <key>` from the request.
2. Checks `storage.is_blocked(key)` (Redis blocklist hook — returns `False` in this chapter).
3. Looks up the key in `storage.lookup_key` and raises `401` if the key is unknown.
4. On success, attaches the `Principal` to `request.state` for downstream middleware.

The storage layer (`storage.py`) is in-memory in this chapter; the real implementation swaps it for Redis + a database. The split between `storage.py` and `code.py` mirrors new-api's separation between `model/` (persistence) and `middleware/` (HTTP plumbing).

```
Client ──POST + Bearer ──▶  require_api_key  ──▶  /v1/chat/completions  ──▶  Upstream
                                │ 401 if missing / unknown / blocked
                                ▼
                          Principal on request.state
```

![architecture](images/architecture.svg)

## How It Works

The dependency is a single function:

```python
def require_api_key(request: Request) -> Principal:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    key = auth.removeprefix("Bearer ").strip()
    if is_blocked(key):
        raise HTTPException(status_code=401, detail="key blocked")
    principal = lookup_key(key)
    if principal is None:
        raise HTTPException(status_code=401, detail="unknown key")
    request.state.principal = principal
    return principal
```

It is attached to the chat-completion route with `dependencies=[Depends(require_api_key)]` — the handler does not need to know about it. Storage is a tiny module:

```python
@dataclass
class Principal:
    user_id: str
    scopes: tuple[str, ...] = ()


_keys: dict[str, Principal] = {}

def register_key(user_id: str, key: str, scopes=("chat",)) -> None:
    _keys[key] = Principal(user_id=user_id, scopes=scopes)

def lookup_key(key: str) -> Principal | None:
    return _keys.get(key)

def is_blocked(key: str) -> bool:
    return False
```

`is_blocked` is the seam where Redis will plug in later; today it always returns `False`.

## Run It

The in-memory storage is empty at startup, so any first request returns `401`. Register a key, then send a request:

```sh
cd s05_api_key_auth
PORT=8005 python code.py &
```

In another shell:

```sh
# 401 — no Authorization header
curl -i -X POST http://localhost:8005/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

Register a key in the running process (one-shot REPL would be `python -c "from storage import register_key; register_key('demo','sk-demo')"`), restart, and resend — but in practice the easiest path is to add the key to `storage.py`'s startup logic or to drive the relay through tests. This is the same model new-api uses at boot when it reads its own user table.

For development, the simplest path is to drop a small helper into a startup script:

```sh
python -c "from s05_api_key_auth.storage import register_key; register_key('demo','sk-demo')" &
PORT=8005 python s05_api_key_auth/code.py &
curl -X POST http://localhost:8005/v1/chat/completions \
  -H 'authorization: Bearer sk-demo' \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

## Tests

```sh
pytest tests/test_s05_api_key_auth.py -v
```

Three tests, one autouse fixture (`_clean`) that resets the in-memory key table before and after every test:

- `test_missing_authorization_rejected` — no `Authorization` header → `401`.
- `test_valid_key_passes_through` — registered key `sk-test-123` → mock OpenAI returns `200`.
- `test_unknown_key_rejected` — unknown key `sk-nope` → `401`.

## → new-api source

| Here | new-api |
|---|---|
| `storage.py` (in-memory `_keys`) | `model/Key.go` — the persisted `sk-*` rows plus Redis cache |
| `require_api_key` dependency | `middleware/Auth.go` — `AuthHelper` reads `Authorization: Bearer …`, looks up the key, rejects banned / disabled tokens |
| `is_blocked(key)` hook | Redis blocklist check inside `middleware/Auth.go` (ban-by-token path) |
| `Principal` on `request.state` | `c.Set("ctx", ctx)` in `middleware/Auth.go` — every downstream handler reads user/scopes from context |
| `dependencies=[Depends(require_api_key)]` | `Router.Use(Auth)` — same effect at the router level |

new-api's real implementation is much richer: it loads the user row, resolves channel-specific keys, checks quotas (`model/UserQuota.go`), and writes the `Principal` into the request context so the relay layer can attribute usage. The seam shown here (`storage.is_blocked`) is the smallest cut surface that lets later chapters swap in those pieces without rewriting `code.py`.

## Trade-offs

What we deliberately did **not** do:

- **In-memory storage.** Fine for the tutorial; a process restart loses every key. Real storage is Redis + SQL (`model/Key.go` + `model/User.go`).
- **No hashing.** `register_key("demo","sk-demo")` stores the raw token. Production stores a hash and compares on lookup (`crypto.CompareHashAndPassword` in Go, `hmac.compare_digest` in Python).
- **No expiry / rotation.** Real keys have an `expired_time` and a rotation flow.
- **`is_blocked` is a stub.** It always returns `False`. In production it does a Redis `EXISTS` on a `banned:<key>` set and is the seam the ban endpoint writes to.
- **No per-route scope check.** `scopes` are attached to the `Principal` but never read yet. s06+ will enforce them.
- **No rate limit / quota accounting.** That is the next phase.
- **One global key space.** Real systems namespace keys per tenant or per channel; new-api scopes keys by `user_id` and resolves them through `model/Key.go`.