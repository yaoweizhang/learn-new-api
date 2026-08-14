# s05: API Key 鉴权

> Previous: [s04](../s04_multi_provider/) · Next: [s06](../s06_token_counting/)

**本章新增**:每次访问 `/v1/chat/completions` 都必须带上合法的 API
key,放在 `Authorization: Bearer <key>` 里。未知、缺失、被封禁的 key
统统返回 `401`。

## 问题

s01–s04 都会愉快地转发一切长得像 chat completion 的请求。根本没有
"谁在调"这件事:谁能摸到中继,谁就能花掉你的上游配额,也没有地方挂
上按用户的限速、计费、scope。中继是完全敞开的。

## 方案

引入一个 `Principal`(一个 `user_id` 加一个 `scopes` 元组)和一个
`Depends(require_api_key)` 依赖,它会在 chat-completion 处理器之前运
行。这个依赖做这几件事:

1. 从请求里读 `Authorization: Bearer <key>`。
2. 检查 `storage.is_blocked(key)`(Redis 黑名单钩子——本章永远返
   `False`)。
3. 在 `storage.lookup_key` 里查这个 key,查不到抛 `401`。
4. 成功的话,把 `Principal` 挂到 `request.state`,供下游中间件使用。

存储层(`storage.py`)本章是进程内的;真实实现会换 Redis + 数据库。
`storage.py` 和 `code.py` 的这种拆分,正好对齐 new-api 在
`model/`(持久化)和 `middleware/`(HTTP 装配)之间的切分。

```
Client ──POST + Bearer ──▶  require_api_key  ──▶  /v1/chat/completions  ──▶  Upstream
                                │ 401 如果缺失 / 未知 / 被封
                                ▼
                          Principal 挂在 request.state
```

![architecture](images/architecture.svg)

## 工作原理

依赖本身就是一个函数:

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

它通过 `dependencies=[Depends(require_api_key)]` 挂到 chat-completion
路由上——处理器自己不需要关心鉴权。存储层是个非常小的模块:

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

`is_blocked` 是个接缝,留给以后接 Redis;今天永远返 `False`。

## 运行

进程内存储启动时是空的,所以首条请求就会返回 `401`。注册一个 key 再
发请求:

```sh
cd s05_api_key_auth
PORT=8005 python code.py &
```

在另一个 shell:

```sh
# 401 —— 没有 Authorization 头
curl -i -X POST http://localhost:8005/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

在运行中的进程里注册 key(一次性 REPL 形式 `python -c "from storage
import register_key; register_key('demo','sk-demo')"`),重启,再
发——但实际最简单的还是把这段塞到 `storage.py` 启动逻辑里,或者直接
走测试驱动。new-api 启动时也是这套:它读自己的 user 表。

对开发来说,最偷懒的办法是把 helper 塞到一个启动脚本里:

```sh
python -c "from s05_api_key_auth.storage import register_key; register_key('demo','sk-demo')" &
PORT=8005 python s05_api_key_auth/code.py &
curl -X POST http://localhost:8005/v1/chat/completions \
  -H 'authorization: Bearer sk-demo' \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

## 测试

```sh
pytest tests/test_s05_api_key_auth.py -v
```

三条测试 + 一条 autouse 固定器(`_clean`),它在每条测试前后都重置
进程内 key 表:

- `test_missing_authorization_rejected` —— 不带 `Authorization` 头
  → `401`。
- `test_valid_key_passes_through` —— 已注册 key `sk-test-123`
  → mock 的 OpenAI 返回 `200`。
- `test_unknown_key_rejected` —— 未知 key `sk-nope` → `401`。

## → new-api 源码

| 这里 | new-api |
|---|---|
| `storage.py`(进程内 `_keys`) | `model/Key.go` —— 持久化的 `sk-*` 行 + Redis 缓存 |
| `require_api_key` 依赖 | `middleware/Auth.go` —— `AuthHelper` 读 `Authorization: Bearer …`、查 key、拒掉被禁/失效的 token |
| `is_blocked(key)` 钩子 | `middleware/Auth.go` 里的 Redis 黑名单检查(按 token 封禁的路径) |
| `Principal` 挂在 `request.state` | `middleware/Auth.go` 里的 `c.Set("ctx", ctx)` —— 之后每个下游 handler 都从 context 里读 user/scopes |
| `dependencies=[Depends(require_api_key)]` | `Router.Use(Auth)` —— 在 router 层级达到同样效果 |

new-api 真实实现厚得多:它会加载用户行、解析每个 channel 的 key、
检查配额(`model/UserQuota.go`),再把 `Principal` 写入请求 context,
让 relay 层能把 usage 落到具体用户身上。这里展示的接缝
(`storage.is_blocked`)就是能让后续章节把这些片段接上、而不必改写
`code.py` 的最小切面。

## 取舍

明确**没有**做的事:

- **进程内存储**。教程用没问题,进程一重启所有 key 都没了。真实存
  储是 Redis + SQL(`model/Key.go` + `model/User.go`)。
- **不做哈希**。`register_key("demo","sk-demo")` 把明文存下来。生
  产存哈希再比对(Go 端 `crypto.CompareHashAndPassword`,Python 端
  `hmac.compare_digest`)。
- **没有过期 / 轮换**。真实 key 有 `expired_time` 和轮换流程。
- **`is_blocked` 是桩**。永远返回 `False`。生产里它对 `banned:
  <key>` 集合做 Redis `EXISTS`,就是封禁接口写入的位置。
- **没有按路由的 scope 检查**。`scopes` 挂在 `Principal` 上但还没
  读过。s06+ 会强制。
- **没有限速 / 配额记账**。那是下一阶段的事。
- **单一全局 key 空间**。真实系统按租户或按 channel 命名空间切
  分;new-api 按 `user_id` 区分 key,并通过 `model/Key.go` 解析。
