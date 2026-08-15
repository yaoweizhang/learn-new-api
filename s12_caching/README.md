# s12：精确匹配响应缓存（in-memory dict + sha256 key）

> Previous: [s11](../s11_call_logs/) · Next: [s13](../s13_retry_fallback/)

> *"完全相同 prompt 才命中"* —— 规范化后的消息数组作 key。

> **Layer**：L4 路由与韧性

## 问题

前 11 章里，每次客户端问"gpt-4o-mini 给我讲个笑话"，请求都会原封
不动打到上游——哪怕 1000 个用户问的是同一个问题，上游也会被叫 1000
次。两个痛点立刻出现：

1. **上游账单爆炸**。OpenAI 这类按 token 计费的服务，最贵的不是单
   次推理而是重复请求：同一个 prompt + temperature 跑 1000 次就是
   1000 倍的钱，而答案完全一样。
2. **用户体感延迟**。同一段 prompt 冷启动时延可能 800ms，缓存命中
   之后只要几毫秒。能不能 30ms 之内把"重复问题"挡在网关层，是用户
   体感差别的关键。

所以我们加一道闸门：相同的请求只放一次过去，剩下 999 次从内存里
读出上次的结果直接返回。这一章只做**精确匹配**——同 model、同
messages、同 temperature 才算相同。语义相似（"讲个笑话" vs "给我
说个笑话"）留到 v2 或专门的语义缓存层。

## 方案

引入一个中间件 + 一个内存字典后端：

- **`s12_caching/cache.py`** —— 进程内字典 `dict[key, (expires_at,
  value)]`，外面包一层 `threading.Lock`。对外暴露 `get/set/stats/
  reset_cache`，签名故意照搬 `redis-py` 的样子（v2 把字典换成
  `redis.Redis(...)` 时只动实现，不动接口）。
- **`s12_caching/code.py`** —— FastAPI 装配。挂载 s11 整块 app，新增
  一个 `CacheMiddleware`（包在 s11 的 `LogMiddleware` 外层）和一条
  调试路由 `/admin/cache/stats`。

缓存键：`sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))`。
`sort_keys` + `separators` 让序列化结果**与字段顺序无关**——`{"a":1,
"b":2}` 和 `{"b":2,"a":1}` 算同一个 key。

路由形状：

```
POST /v1/v1/chat/completions      Bearer API key, body={model, messages, stream?, temperature?}  -> 200 + (首次写缓存, 命中直接返回)
GET  /admin/cache/stats                                                              -> 200 {size, live}
```

`stream=true` 的请求**跳过**缓存——SSE 是一段持续的字节流，没法
用一个 `bytes` 整体缓存。

## 工作原理

### `cache.py`：内存字典后端

```python
_store: dict[str, tuple[float, bytes]] = {}  # key -> (expires_at, value)

def _key(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def get(payload):
    key = _key(payload)
    entry = _store.get(key)
    if entry is None or entry.expires_at < time.monotonic():
        return None
    return entry.value

def set(payload, value, ttl_seconds=300):
    _store[_key(payload)] = (time.monotonic() + ttl_seconds, value)
```

- **键的来源**：把请求 JSON 序列化 → 算 sha256 → hex digest。同一份
  payload 任何时候算出来都是同一个 key；改一个字符（哪怕加个空格）
  就完全不一样。
- **TTL**：`set` 时记下过期时间（用 `time.monotonic()` 而不是
  `time.time()`，免得系统时钟跳变时把缓存集体判过期）。`get` 时检
  查过期；过期则当作没命中、清掉。
- **线程安全**：整个 `_store` 读写都加锁。教学版够用；真上 Redis
  后这部分开销归零。
- **为什么不用 `functools.lru_cache`**？因为我们要的是"按 payload
  内容（任意 dict）做键"，`lru_cache` 只支持 hashable 位置参数；要
  自己控 TTL。

### `code.py`：中间件装配

```python
app.mount("/", s11_app)

class CacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method == "POST" and request.url.path == "/v1/v1/chat/completions":
            body_bytes = await request.body()
            try:
                payload = json.loads(body_bytes)
            except Exception:
                payload = {}

            if not payload.get("stream"):
                hit = cache.get(payload)
                if hit is not None:
                    return Response(content=hit, media_type="application/json")

            response = await call_next(request)
            if response.status_code == 200 and not payload.get("stream"):
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                body = b"".join(chunks)
                cache.set(payload, body)
                return Response(content=body, status_code=response.status_code,
                                headers=dict(response.headers),
                                media_type=response.media_type)
            return response
        return await call_next(request)
```

三个关键点：

1. **请求体缓存由 Starlette 处理，下游可复用**。`await request.body()`
   把 body 取出来做键；Starlette 的 `BaseHTTPMiddleware` 在首次读取
   后会把字节缓存到 `request._body`，下游中间件（包括 s11 的
   `LogMiddleware`）再读时拿到的依然是同一份完整 body。所以本章
   不需要手动 replay——s11 的 `request.state.model` 字段会正常记
   录实际 model。
2. **响应体读一次，重组成 `Response`**。`response.body_iterator`
   读完必须重组：否则下游拿到的会是空响应。`Response(content=body,
   status_code=..., headers=..., media_type=...)` 把同样的 bytes 包
   回去，保留状态码和 headers。
3. **路径检查必须对齐挂载链**。`s09` 把 s08 挂在 `/v1`，s08 的
   chat 路由是 `/v1/chat/completions`；所以从 s12 对外看，路径是
   `/v1/v1/chat/completions`。`request.url.path` 看到的是客户端实
   际打过来的路径——这里必须写 `/v1/v1/chat/completions`，否则中间
   件根本进不去 if 分支。这是已知的双前缀债务，见取舍。

### `/admin/cache/stats`：可观测

```python
@app.get("/admin/cache/stats")
def cache_stats() -> dict:
    return cache.stats()
```

返回 `{size, live}`：总条目数和没过期的条目数（方便看 TTL 淘汰的
速度）。生产里会加 hit_rate、eviction_rate、单 key 大小。

## 运行

```bash
# 起服务
python s12_caching/code.py          # PORT 默认 8012

# 准备一个用户 + API key + 渠道 + 配额（chat 端点依赖的状态）
python -c "
from s10_channel_management.channels import create_channel
create_channel('c1', 'openai', 'https://api.openai.com', weight=100, priority=0)
from s09_user_system.users import create_user
import bcrypt
uid = create_user('u@x.com', bcrypt.hashpw(b'secret', bcrypt.gensalt()).decode(), is_admin=False)
from s05_api_key_auth.storage import register_key
register_key(user_id=str(uid), key='sk-test')
from s07_pre_consume_settle.quota import set_balance
set_balance(str(uid), 1_000_000)
"

# 第一次：走到上游（mock），同时写入缓存
curl -s -X POST http://localhost:8012/v1/v1/chat/completions \
  -H 'authorization: Bearer sk-test' \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'

# 第二次：同 payload → 直接从内存返回（毫秒级，不再打上游）
curl -s -X POST http://localhost:8012/v1/v1/chat/completions \
  -H 'authorization: Bearer sk-test' \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'

# 看一眼当前缓存状态
curl -s http://localhost:8012/admin/cache/stats
# -> {"size": 1, "live": 1}
```

## 测试

```bash
pytest tests/test_s12_caching.py -v
```

一个测试覆盖主契约：

| 测试 | 断言 |
| --- | --- |
| `test_identical_request_hits_cache` | 同样 body 连发两次，两次都 200，但 `upstream_openai.calls.call_count == 1` ——证明第二次命中缓存，没打上游。 |

测试里 respx mock 的 `calls.call_count` 是关键：respx 给每个 mock
路由维护一个调用计数器，第二次请求没经过 mock 路由（被中间件短路
了），所以计数不会涨。这是"缓存真的命中了"的硬证据。

`_clean` fixture 重置了 `s12_caching.cache`、`s10_channel_management
.channels`、`s09_user_system.users`、`s05_api_key_auth.storage`、
`s07_pre_consume_settle.quota`、`s08_rate_limiting.bucket`、
`s11_call_logs.log_store`——所有 chat 端点依赖的状态加本章节的缓存。

## → new-api 源码

真实部署里同样的"响应缓存"长这样（路径区分大小写，Windows 上看
着像 `Redis.go`、Linux/macOS 是 `redis.go`）：

- `common/Redis.go` —— Redis 客户端单例 + 健康检查。生产缓存的实
  际后端；我们这里的 `dict[str, bytes]` 就是它的最小内存替身。
- `pkg/cachex/codec.go` —— 键的编解码。Go 里序列化用 `json.Marshal`
  ；这里同样的目的但场景不同——new-api 是把 `RequestPayload` struct
  序列化、我们是把客户端任意 JSON 序列化。两者都用 sha256 做摘要。
- `pkg/cachex/hybrid_cache.go` —— `Get/Set/Delete/Stats` 的统一接口，
  内部可以走 Redis、可以走本地 `Ristretto`（一个进程内 LRU）。我们
  这一章就是它的"教学最小集"：只有内存 + 完整 TTL，接口是
  `cache.get/set/stats/reset_cache`，签名上下一一对应。
- `pkg/cachex/namespace.go` —— key 加前缀做命名空间隔离（不同业务
  方不互相覆盖）。我们没有做这一步——教学版假设整张缓存都归 chat
  用；真实部署多业务混用时必须加 namespace。

> Windows 文件系统不分大小写，本地 IDE 里看着像 `Redis.go` 不少见；
> 部署到 Linux/macOS 时按实际的小写路径访问。

## 取舍

- **进程内 `dict` 而不是 Redis** —— YAGNI。本章只演示"缓存中间件
  这个模式存在、键怎么算、TTL 怎么管"，多进程部署、跨实例共享、
  持久化全部不在这一章的范围。v2 切 Redis 时只动 `cache.py` 一个
  文件，接口不变；中间件不动。注意：单进程内存缓存在多 worker
  下会变成"N 个独立缓存"——上 Redis 之前不能横向扩。
- **不缓存 `stream=true`** —— SSE 是一段持续输出，没法用一个
  `bytes` 整体缓存。简单做法是直接跳缓存（fast/slow 都不算），
  复杂做法是按 chunk 缓存到流结束（partial-stream cache），那是 v2
  的事，本章不做。
- **精确匹配，不做语义缓存** —— "讲个笑话"和"给我说个笑话"在这一
  章算两个不同 key。语义缓存需要把 query 做 embedding、算相似度、
  再决定是否命中——这一整套是另一章节的工作量，本章只管"同字节
  同响应"。
- **路径 `/v1/v1/chat/completions` 是双前缀** —— 已知债务。s09 把
  s08 挂在 `/v1` 下，s08 自己的 chat 路由又是 `/v1/chat/comple
  tions`，所以对外可访问路径是 `/v1/v1/chat/completions`。本章中间
  件的路径检查按实际可达路径写。修法是把 s09 的
  `app.mount("/v1", s08_app)` 改成 `app.mount("/", s08_app)` + s08
  路由改名 `/chat/completions`，但那会改动 s08-s11 所有章节的测试，
  留到接口契约统一清理那一章再做。
- **body 读完仍可被下游 middleware 复用** —— Starlette 的
  `BaseHTTPMiddleware` 在首次 `await request.body()` 后会把字节缓存
  到 `request._body`，下游中间件（包括 s11 的 `LogMiddleware`）再读
  拿到的依然是同一份完整 body，所以 s11 的 `request.state.model` 字
  段会正常记录实际 model，不需要走 `request.state` 透传。
- **TTL 固定 300 秒** —— 不暴露给客户端、不按模型分级。生产里
  高频问答（"今天天气"）应该 TTL 短（30s），代码补全（"写个快排"）
  可以 TTL 长（1 天）；本章先让缓存生效，配置粒度是 v2 的事。
- **没有缓存击穿保护** —— 同一秒 1000 个请求都拿同一 key 的过期边
  界，理论上有 1000 个都判过期、都打到上游。生产里要在 `set` 还没
  落定时先放一个 short-TTL 占位；本章 YAGNI。