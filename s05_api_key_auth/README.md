# s05: API Key 鉴权 — 一道 Bearer 守门,401 把匿名打掉

> Previous: [s04](../s04_multi_provider/) · Next: [s06](../s06_token_counting/)

> *"Bearer 一行拦住所有人"* —— header 一行守住所有下游。

> **Layer**：L2 鉴权与身份

## 本章要做什么

s01-s04 中继对谁都敞着——能摸到端口就能花上游 key 的钱,根本没有"谁在调"这件事:按用户限速、按用户计费、按用户 scope 控制,全都挂不上去,因为连调用方身份都没有。

要解决这个,在 chat 路由前面加一道 Bearer 闸门:每个 `/v1/chat/completions` 请求都先过 `Depends(require_api_key)`,不知道 / 不认识 / 被封禁的 key 一律 `401` 打掉,通过之后才进转发循环。本章就写这一道闸门:

1. **写 `require_api_key` 依赖 —— 为什么必须用 Depends 而不是中间件**:`Depends`(FastAPI 依赖注入:路由处理器之前自动跑的函数)是 FastAPI 的官方可测试注入点,**为什么不比 on_event 拦截**:on_event 只能编进 ASGI 中间件栈、测不动;**为什么每个 handler 自己声明**:路由写 `dependencies=[Depends(require_api_key)]` 不必改全局栈,新加路由默认是开放的(不会偷偷被闸上)——这是显式优于隐式的取舍。
2. **读 `Authorization: Bearer <key>` —— 为什么是 Bearer 头而不是 query 串**:Bearer 头一行密码,放请求头里、**为什么不放 URL**:`Authorization: Bearer sk-xxx` 是 OAuth 2.0 标准放密钥的地方,放 URL 会被 nginx access log、上游 SLA 日志、浏览器历史全留下来——密钥不应穿过日志系统;**为什么 split 方式是 `startswith("Bearer ")` + `removeprefix(...)`**:大小写不敏感但前缀格式严格,空格分隔切干净。
3. **查 `storage.lookup_key` + `storage.is_blocked` —— 为什么分两步**:先查黑名单(`is_blocked`)、再查白名单(`lookup_key`),**为什么不合并成一个 if**:`is_blocked` 是个生产接缝(未来接 Redis `banned:` 集合),即使白名单查不到,被显式封禁的 key 也应被特殊处理(返回 `key blocked` 而不是 `unknown key`,运维能区分意图);**为什么 storage 是独立模块**:和 s04 把 adapter 抽出来的理由一致——`code.py` 不该知道 key 存在哪里,只调 `lookup_key(key)` 拿 `Principal`。
4. **把 `Principal` 挂到 `request.state` —— 为什么挂到 state 而不是 return**:`Depends` 把 `principal` 当返回值也能拿到,但**为什么还要写 `request.state.principal = principal`**:后续中间件 / handler(限速 s08 / 配额 s07 / 日志 s11)都从 `request.state.principal` 拿身份,不一定走 Depends 链(`Principal` 也要够轻,本教程里只装 `user_id` + `scopes`)。

成品:`curl -i .../v1/chat/completions`(没有 `Authorization` 头)回 `401 missing bearer token`;注册一个 key `sk-demo` 再带 `authorization: Bearer sk-demo` 发请求,转发生效。后续 s07 在闸门之后接按用户的配额,s08 在闸门之后接按用户的限速,s11 把每次调用的 `user_id` 写进日志;chat 路径的鉴权链路到这里定型。

## 上一章复盘

s04 之后任何能访问的客户端都能花网关的钱。必须先有"谁在打"的标识。

## 在整体中的位置

守门人——任何后续处理(限速、配额、转发)都假设这步已经放行。**双轨鉴权其一**：s05 的 Bearer API key 守 chat 路径；dashboard / admin 路径另由 s09 的 JWT 鉴权把关(见 s09 README)。两条并存、不替代：chat 路径始终走 API key，dashboard / admin 始终走 JWT。

## 问题

s01–s04 都会愉快地转发一切长得像 chat completion 的请求。根本没有"谁在调"这件事:谁能摸到中继,谁就能花掉你的上游配额,也没有地方挂上按用户的限速、计费、scope。中继是完全敞开的。

## 方案

引入一个 `Principal`（当前请求代表的用户身份与权限：`user_id` + `scopes` 元组；`scopes` 是权限标签，挂在 Principal 上）和一个
`Depends`（FastAPI 依赖注入：路由前自动跑的函数）`require_api_key` 依赖,它会在 chat-completion 处理器之前运行。这个依赖做这几件事:

1. 从请求里读 `Authorization: Bearer <key>`。
2. 检查 `storage.is_blocked(key)`(黑名单查询钩子,返回是否被封禁——未来接 Redis;本章永远返 `False`)。
3. 在 `storage.lookup_key` 里查这个 key,查不到抛 `401`。
4. 成功的话,把 `Principal` 挂到 `request.state`,供下游中间件使用。

存储层(`storage.py`)本章是进程内的;真实实现会换 Redis + 数据库。
`storage.py` 和 `code.py` 的这种拆分,正好对齐 new-api 在 `model/`(持久化)和 `middleware/`(HTTP 装配)之间的切分。

下面这张 ASCII 流程图画鉴权边界,和下面那张架构图相对照——上面这张是单跳时序,下面那张是角色拓扑,中间那块都是 `require_api_key` 闸门:

```
Client ──POST + Bearer ──▶  require_api_key  ──▶  /v1/chat/completions  ──▶  Upstream
                                │ 401 如果缺失 / 未知 / 被封
                                ▼
                          Principal 挂在 request.state
```

下面这张架构图给读者一幅全局鸟瞰——图里有 `Client / require_api_key / 上游` 几个角色,请求自左向右、响应自右向左折返;中间那一块 `require_api_key` 闸门就是本章要写的代码:读 Bearer 头、查 key、挂 Principal:

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

它通过 `dependencies=[Depends(require_api_key)]` 挂到 chat-completion 路由上——处理器自己不需要关心鉴权。存储层是个非常小的模块:

```python
@dataclass
class Principal:
    user_id: str
    scopes: tuple[str, ...] = ()


_keys: dict[str, Principal] = {}

def register_key(user_id: str, key: str, scopes: tuple[str, ...] = ("chat",)) -> None:
    _keys[key] = Principal(user_id=user_id, scopes=scopes)

def lookup_key(key: str) -> Principal | None:
    return _keys.get(key)

def is_blocked(key: str) -> bool:
    return False
```

`is_blocked` 是个接缝,留给以后接 Redis;今天永远返 `False`。

## 运行

进程内存储启动时是空的,所以首条请求就会返回 `401`。注册一个 key 再发请求:

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

注册 key 最偷懒的办法是把 helper 塞到一个一次性 REPL 调用里:

```sh
python -c "from s05_api_key_auth.storage import register_key; register_key('demo','sk-demo')"
PORT=8005 python s05_api_key_auth/code.py &
curl -X POST http://localhost:8005/v1/chat/completions \
  -H 'authorization: Bearer sk-demo' \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

更稳妥的做法是把这段塞进 `storage.py` 的启动逻辑,或者直接走测试驱动。new-api 启动时也是这套:它读自己的 user 表。

## → new-api 源码

| 这里 | new-api |
|---|---|
| `storage.py`(进程内 `_keys`) | `model/Key.go` —— 持久化的 `sk-*` 行 + Redis 缓存 |
| `require_api_key` 依赖 | `middleware/Auth.go` —— `AuthHelper` 读 `Authorization: Bearer …`、查 key、拒掉被禁/失效的 token |
| `is_blocked(key)` 钩子 | `middleware/Auth.go` 里的 Redis 黑名单检查(按 token 封禁的路径) |
| `Principal` 挂在 `request.state` | `middleware/Auth.go` 里的 `c.Set("ctx", ctx)` —— 之后每个下游 handler 都从 context 里读 user/scopes |
| `dependencies=[Depends(require_api_key)]` | `Router.Use(Auth)` —— 在 router 层级达到同样效果 |

new-api 真实实现厚得多:它会加载用户行、解析每个 channel 的 key、检查配额(`model/UserQuota.go`),再把 `Principal` 写入请求 context,让 relay 层能把 usage 落到具体用户身上。这里展示的接缝(`storage.is_blocked`)就是能让后续章节把这些片段接上、而不必改写 `code.py` 的最小切面。

## 取舍

明确**没有**做的事:

- **进程内存储**。教程用没问题,进程一重启所有 key 都没了。真实存储是 Redis + SQL(`model/Key.go` + `model/User.go`)。
- **不做哈希**。`register_key("demo","sk-demo")` 把明文存下来。生产存哈希再比对(Go 端 `crypto.CompareHashAndPassword`,Python 端 `hmac.compare_digest`)。
- **没有过期 / 轮换**。真实 key 有 `expired_time` 和轮换流程。
- **`is_blocked` 是桩**。永远返回 `False`。生产里它对 `banned: <key>` 集合做 Redis `EXISTS`,就是封禁接口写入的位置。
- **没有按路由的 scope 检查**。`scopes` 挂在 `Principal` 上但还没读过。s06+ 会强制。
- **没有限速 / 配额记账**。那是下一阶段的事。
- **单一全局 key 空间**。真实系统按租户或按 channel 命名空间切分;new-api 按 `user_id` 区分 key,并通过 `model/Key.go` 解析。

## 下章预告

s05 知道"谁在打"但不知道"打得多贵"。s06 接 tiktoken 和按厂商估算,先汇报 token 数。
