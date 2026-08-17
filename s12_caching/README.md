# s12: 精确匹配响应缓存(in-memory dict + sha256 key) — 同样的请求不再问上游——sha256 一致就返

> Previous: [s11](../s11_call_logs/) · Next: [s13](../s13_retry_fallback/)

> *"完全相同 prompt 才命中"* —— 规范化后的消息数组作 key。

> **Layer**：L4 路由与韧性

## 问题

前 11 章里,每次客户端问"gpt-4o-mini 给我讲个笑话",请求都会原封
不动打到上游——哪怕 1000 个用户问的是同一个问题,上游也会被叫 1000
次。两个痛点立刻出现:

1. **上游账单爆炸**。OpenAI 这类按 token 计费的服务,最贵的不是单
   次推理而是重复请求:同一个 prompt + temperature 跑 1000 次就是
   1000 倍的钱,而答案完全一样。
2. **用户体感延迟**。同一段 prompt 冷启动时延可能 800ms,缓存命中
   之后只要几毫秒。能不能 30ms 之内把"重复问题"挡在网关层,是用户
   体感差别的关键。

所以我们加一道闸门:相同的请求只放一次过去,剩下 999 次从内存里
读出上次的结果直接返回。这一章只做**精确匹配**——同 model、同
messages、同 temperature 才算相同。语义相似("讲个笑话" vs "给我
说个笑话")留到 v2 或专门的语义缓存层。

## 本章要做什么

要解决这个——**我们给 chat 端点包一层精确匹配缓存**(**响应缓存 / TTL 缓存**(在内存里把"请求 JSON → 上游响应 bytes"这一对原样存下来,下次同样的请求直接吐缓存,根本不打上游;每条缓存带一个过期时间 **TTL**(time-to-live,过期就清掉)):同 model、同 messages、同 temperature 的请求,首次落缓存,之后命中直接吐 bytes,根本不打上游。本章把这层缓存写出来:

1. **写一个内存缓存后端 `cache.py`**。`_store: dict[str, tuple[float, bytes]] = {}` 加 `threading.Lock`,对外暴露 `reset_cache / get / set / stats`。本章只演示"缓存中间件这个模式存在、键怎么算、TTL 怎么管",多进程共享、持久化、跨实例全部不在这一章的范围——接口照搬 `redis-py` 的签名(`get/set/stats`),v2 切 Redis 时只动实现不动接口,中间件一行不用改,所以先不接 Redis。
2. **键用 `sha256(canonical JSON)`**。`_key(payload)` = `hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()`。`sort_keys + separators` 让序列化结果与字段顺序无关——`{"a":1, "b":2}` 和 `{"b":2, "a":1}` 算同一个 key,改一个字符(哪怕加个空格)就完全不一样。`lru_cache` 只支持 hashable 位置参数;我们要按任意 dict 内容做键,还要自己控 TTL,所以用 sha256 而不是 `functools.lru_cache`。
3. **`CacheMiddleware` 包在 s11 外层**。只对 `POST /v1/chat/completions` 起效:命中 → 直接 `Response(content=hit)`,短路所有下游;miss → `await call_next(request)` 转发,200 后把 `response.body_iterator` 读一遍重组再 `cache.set(payload, body)`。SSE 是一段持续字节流,没法用一个 `bytes` 整体缓存,跳掉比 partial-stream cache 简单得多,那部分留到 v2——所以 `stream=true` 跳过缓存。Starlette 的 `BaseHTTPMiddleware` 在首次 `await request.body()` 后会把字节缓存到 `request._body`,s11 的 `LogMiddleware` 再读时拿到的依然是同一份完整 body——所以 body 读一次不破坏下游,不需要走 `request.state` 透传。
4. **TTL 300 秒 + 时间戳用 `time.monotonic()`**。`set` 时记 `time.monotonic() + ttl_seconds`,`get` 时检查过期就清掉。系统时钟跳变(NTP 校时、跨时区)时 `time.time()` 会回退或跳跃,可能把缓存集体判过期或集体"复活",`monotonic` 只往前走,语义干净——所以用 `monotonic`。本章先让缓存生效——按模型分级、按客户端覆盖是 v2 的事,所以 TTL 固定 300 秒。

成品:同样 body 连发两次,第一次打上游 + 写缓存,第二次直接吐缓存 bytes,`upstream_openai.calls.call_count == 1`。`curl /admin/cache/stats` 看到 `{size, live}` 实时反映缓存状态。后续 s13 在 `CacheMiddleware` 内侧加一层失败回落,落到下一条渠道;真上 Redis 时接口不动、`cache.py` 一文件替换。

## 方案

现在的场景是:`## 问题` 提了两件痛——上游账单被重复请求翻倍 (痛点 #1)、用户体感 800ms 冷启动延迟 (痛点 #2)——这两件事客户端自带缓存搞不定、客户端 JS 优化也搞不定,必须由网关在 chat 路由外层包一道精确匹配闸门:同 prompt 命中直接吐 bytes,短路所有下游;未命中照常转发,响应写回缓存。

**要解决这个——我们在网关里引入一个中间件 + 一个内存字典后端**:

- **`s12_caching/cache.py`** —— 进程内字典 `dict[key, (expires_at,
  value)]`,外面包一层 `threading.Lock`。对外暴露 `get/set/stats/
  reset_cache`,签名故意照搬 `redis-py` 的样子(v2 把字典换成
  `redis.Redis(...)` 时只动实现,不动接口)。
- **`s12_caching/code.py`** —— FastAPI 装配。挂载 s11 整块 app,新增
  一个 `CacheMiddleware`(包在 s11 的 `LogMiddleware` 外层)和一条
  调试路由 `/admin/cache/stats`。

**响应缓存 / TTL 缓存** —— 在网关层按请求 payload 算 key、把相同请求的响应原样缓存 TTL 秒、命中时短路所有下游。它在本章里承担的是"同 prompt 命中秒级吐回、未命中照常转发并写回"的两段动作。

缓存键:`sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))`。
`sort_keys` + `separators` 让序列化结果**与字段顺序无关**——`{"a":1,
"b":2}` 和 `{"b":2,"a":1}` 算同一个 key。

下面这幅图把上面两件痛点各放到一个角色里:

- **`Client` (调用方)** —— 在装上缓存之前,这是被账单和延迟困住两难的角色;装上之后,这事被网关解——Client 只管发请求,同 prompt 第二次起由网关秒级吐回。
- **`Gateway` (本章要写的 CacheMiddleware)** —— 把痛点 #1 #2 的解决动作集中放在这里:中间件按 `request.method == POST and request.url.path == /v1/chat/completions` 触发 → 算 `key = sha256(canonical JSON)` → `cache.get(key)` 命中直接 `Response(content=hit)` 短路返回;未命中走 `await call_next(request)`,响应 200 后把 body 重组写回 `cache.set(payload, body)`。Client 看不见有没有走缓存,Upstream 看不见命中,缓存层藏在中间件里。
- **`Cache` (进程内 `dict` + Lock,`cache.py`)** —— 本章新引入的进程内存储。`_store: dict[key, (expires_at, value)]`,每个 key 是 `sha256(canonical JSON)`,value 是上游响应 bytes,`expires_at = monotonic() + ttl_seconds`(默认 300s)。所有读写在 `threading.Lock` 下原子——单进程多线程够用,s_full 切 Redis 时接口不动、`cache.py` 一文件替换。
- **`Upstream` (LLM 厂商)** —— 服务提供方。它在响应里只看到自己被调了一次还是被调了 N 次,完全不知道网关外面有一层缓存——被网关"短路"掉的请求根本不会到达 Upstream。

路由形状——下面这张块状路由表把本章要写的 2 条接口压成一览:左是 `method + path`,中间是入参(`/v1/chat/completions` 接 `Authorization: Bearer API key` 和 body),右是返回码与返回体;本章要写的核心就是"读缓存或写缓存"一条转发路径 + 一条统计接口:

```
POST /v1/chat/completions      Bearer API key, body={model, messages, stream?, temperature?}  -> 200 + (首次写缓存, 命中直接返回)
GET  /admin/cache/stats                                                              -> 200 {size, live}
```

`stream=true` 的请求**跳过**缓存——SSE 是一段持续的字节流,没法
用一个 `bytes` 整体缓存。

## 工作原理

**原理**: 一个 chat 请求从客户端进来, 它的生命周期是: `CacheMiddleware.dispatch` 拦在 s11 mount 外层 → 检查 `request.method == POST and request.url.path == /v1/chat/completions` → 调 `await request.body()` 拿原始字节(Starlette 首次读后会自动缓存到 `request._body`,下游中间件可复用)→ 解析成 `payload` dict → 算 `key = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()` → 调 `cache.get(payload)` 命中直接 `Response(content=hit, media_type=application/json)` 短路返回,未命中走 `await call_next(request)` → 200 响应时把 `response.body_iterator` 读一遍重组 bytes,调 `cache.set(payload, body)` 写回 TTL 300s。`stream=true` 的请求跳过缓存(无法整体缓存 SSE 字节流)。所有部件都围着"按精确 key 短路 / 写回"这条主线展开。

**1. 一个 cache store (`cache.py`,进程内 `dict` + `threading.Lock`)** —— `_store: dict[str, tuple[float, bytes]]` 存 `(expires_at, value)`;`reset_cache / get / set / stats` 四个公开函数签名照搬 `redis-py`(`get` 返 bytes 或 None,`set` 接 `(key, value, ttl_seconds=300)`),v2 切 Redis 时只动实现不动接口。所有读写都在 `_lock` 下原子——单进程多线程够用,真上 Redis 后这部分开销归零。

**2. 一个 canonical key (`cache._key`,`sha256` 摘要)** —— `hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()`。`sort_keys + separators` 让序列化结果**与字段顺序无关**——`{"a":1,"b":2}` 和 `{"b":2,"a":1}` 算同一个 key;改一个字符(哪怕加个空格)就完全不一样。不用 `functools.lru_cache` 是因为它只支持 hashable 位置参数,我们要按任意 dict 内容做键,还要自己控 TTL。

**3. 一个 TTL eviction (`cache.set/get`,`time.monotonic()` 计时)** —— `set` 时记 `time.monotonic() + ttl_seconds`,`get` 时检查过期就清掉。用 `monotonic` 而不是 `time.time()` 是因为系统时钟跳变(NTP 校时、跨时区)时 `time.time()` 会回退或跳跃,可能把缓存集体判过期或集体"复活",`monotonic` 只往前走,语义干净。默认 TTL 固定 300 秒——按模型分级、按客户端覆盖是 v2 的事。

**4. 一个 CacheMiddleware (`code.py`,`BaseHTTPMiddleware` 子类)** —— 包在 s11 mount 外层(Starlette 按注册顺序匹配):命中路径直接 `Response(content=hit)` 不调下游;miss 路径走 `await call_next(request)`,响应 200 后把 `response.body_iterator` 异步迭代重组 bytes 再 `cache.set(payload, body)` 写回。重组 + `Response(content=body, headers=..., media_type=...)` 重包是必须的——下游不重组会拿到空响应。`request.body()` 一次读完自动被 Starlette 缓存到 `request._body`,s11 的 `LogMiddleware` 再读时拿到的依然是同一份完整 body,不需要手动 replay。

### `cache.py`:内存字典后端

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

- **键的来源**:把请求 JSON 序列化 → 算 sha256 → hex digest。同一份
  payload 任何时候算出来都是同一个 key;改一个字符(哪怕加个空格)
  就完全不一样。
- **TTL**:`set` 时记下过期时间(用 `time.monotonic()` 而不是
  `time.time()`,免得系统时钟跳变时把缓存集体判过期)。`get` 时检
  查过期;过期则当作没命中、清掉。
- **线程安全**:整个 `_store` 读写都加锁。教学版够用;真上 Redis
  后这部分开销归零。
- **为什么不用 `functools.lru_cache`**?因为我们要的是"按 payload
  内容(任意 dict)做键",`lru_cache` 只支持 hashable 位置参数;要
  自己控 TTL。

### `code.py`:中间件装配

```python
app.mount("/", s11_app)

class CacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method == "POST" and request.url.path == "/v1/chat/completions":
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

三个关键点:

1. **请求体缓存由 Starlette 处理,下游可复用**。`await request.body()`
   把 body 取出来做键;Starlette 的 `BaseHTTPMiddleware` 在首次读取
   后会把字节缓存到 `request._body`,下游中间件(包括 s11 的
   `LogMiddleware`)再读时拿到的依然是同一份完整 body。所以本章
   不需要手动 replay——s11 的 `request.state.model` 字段会正常记
   录实际 model。
2. **响应体读一次,重组成 `Response`**。`response.body_iterator`
   读完必须重组:否则下游拿到的会是空响应。`Response(content=body,
   status_code=..., headers=..., media_type=...)` 把同样的 bytes 包
   回去,保留状态码和 headers。
3. **路径检查必须对齐挂载链**。`s09` 现在把 s08 挂在 `/`(已经统一成
   `/`),s08 的 chat 路由是 `/v1/chat/completions`;所以从 s12 对外看,
   路径就是 `/v1/chat/completions`(单前缀,已无历史双前缀债务)。
   `request.url.path` 看到的是客户端实际打过来的路径——这里写
   `/v1/chat/completions`。

### `/admin/cache/stats`:可观测

```python
@app.get("/admin/cache/stats")
def cache_stats() -> dict:
    return cache.stats()
```

返回 `{size, live}`:总条目数和没过期的条目数(方便看 TTL 淘汰的
速度)。生产里会加 hit_rate、eviction_rate、单 key 大小。

## 运行

```bash
# 起服务
python s12_caching/code.py          # PORT 默认 8012

# 准备一个用户 + API key + 渠道 + 配额(chat 端点依赖的状态)
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

# 第一次:走到上游(mock),同时写入缓存
curl -s -X POST http://localhost:8012/v1/chat/completions \
  -H 'authorization: Bearer sk-test' \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'

# 第二次:同 payload → 直接从内存返回(毫秒级,不再打上游)
curl -s -X POST http://localhost:8012/v1/chat/completions \
  -H 'authorization: Bearer sk-test' \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'

确认缓存命中 / 未命中两条路径都能跑?打上面三连 curl——同 payload 连发两次,第一次 mock 的 `call_count == 1` 说明 `CacheMiddleware` 把未命中请求转发到了上游且响应写回缓存;第二次拿到一模一样的 body 且 mock 的 `call_count` 仍为 `1`(没增)说明 `cache.get(payload)` 命中,中间件直接 `Response(content=hit)` 短路返回,根本没调上游;`/admin/cache/stats` 返 `{"size": 1, "live": 1}` 说明 `cache.py` 的 `_store: dict` 和 TTL 计时都在响应。两条路径都在跑:

```bash
# 看一眼当前缓存状态
curl -s http://localhost:8012/admin/cache/stats
# -> {"size": 1, "live": 1}
```

## → new-api 源码

真实部署里同样的"响应缓存"长这样(路径区分大小写,Windows 上看
着像 `Redis.go`、Linux/macOS 是 `redis.go`):

- `common/redis.go` —— Redis 客户端单例 + 健康检查。生产缓存的实
  际后端;我们这里的 `dict[str, bytes]` 就是它的最小内存替身。
- `pkg/cachex/codec.go` —— 键的编解码。Go 里序列化用 `json.Marshal`
  ;这里同样的目的但场景不同——new-api 是把 `RequestPayload` struct
  序列化、我们是把客户端任意 JSON 序列化。两者都用 sha256 做摘要。
- `pkg/cachex/hybrid_cache.go` —— `Get/Set/Delete/Stats` 的统一接口,
  内部可以走 Redis、可以走本地 `Ristretto`(一个进程内 LRU)。我们
  这一章就是它的"教学最小集":只有内存 + 完整 TTL,接口是
  `cache.get/set/stats/reset_cache`,签名上下一一对应。
- `pkg/cachex/namespace.go` —— key 加前缀做命名空间隔离(不同业务
  方不互相覆盖)。我们没有做这一步——教学版假设整张缓存都归 chat
  用;真实部署多业务混用时必须加 namespace。

> Windows 文件系统不分大小写,本地 IDE 里看着像 `Redis.go` 不少见;
> 部署到 Linux/macOS 时按实际的小写路径访问。

## 本章不做什么

- **不缓存 `stream=true`** (SSE 流式响应:逐 token 推送的持续字节流)——SSE 是一段持续输出,没法用一个 `bytes` 整体缓存。简单做法是直接跳缓存(fast/slow 都不算),复杂做法是按 chunk 缓存到流结束(partial-stream cache),那是 v2 的事,本章不做。
- **不做语义缓存** (把 prompt 做 embedding 后按相似度命中,而不按字节相同)——"讲个笑话"和"给我说个笑话"在这一章算两个不同 key。语义缓存需要把 query 做 embedding、算相似度、再决定是否命中——这一整套是另一章节的工作量,本章只管"同字节同响应"。
- **没有缓存击穿保护** (同一 key 在过期瞬间被并发请求同时打到上游)——同一秒 1000 个请求都拿同一 key 的过期边界,理论上有 1000 个都判过期、都打到上游。生产里要在 `set` 还没落定时先放一个 short-TTL 占位;本章 YAGNI。
- **没有写穿 / 读穿策略** (write-through 同步刷 DB / read-through 缓存空时回源加载)——本章只演示"内存 dict + TTL"这条最简路径,写穿/读穿是引入持久化后端的副产物,不在本章选。
- **不做按客户端 / 按模型的 TTL 差异化** ——TTL 固定 300s,所有请求共用一份配置。生产里高频问答("今天天气")应该 TTL 短(30s),代码补全("写个快排")可以 TTL 长(1 天);按场景分级是 v2 的事。

## 已知限制

- **进程内 `dict` 不跨 worker** (单进程内存,多 worker 下变 N 个独立缓存)——YAGNI。本章只演示"缓存中间件这个模式存在、键怎么算、TTL 怎么管",多进程部署、跨实例共享、持久化全部不在这一章的范围。v2 切 Redis 时只动 `cache.py` 一个文件,接口不变;中间件不动。注意:单进程内存缓存在多 worker 下会变成"N 个独立缓存"——上 Redis 之前不能横向扩。
- **`threading.Lock` 不跨 worker** (单进程锁,多 worker 进程下不互斥)——`asyncio` 单 worker 部署够用,但多 worker 时每个 worker 各持一份 `_store`,同一请求在不同 worker 上可能拿到不一致的 cache hit/miss;上 Redis 共享是后续优化项。
- **`stream=true` 跳过缓存 → SSE 请求每次都打上游** ——不缓存 stream 不是 bug,是设计:用户主动开启流式是要看真实进度,缓存了反而违反用户意图;代价是流式场景无法复用本章的成果。
- **body 重组后下游 s11 的 `LogMiddleware` 拿到的是重组 bytes** ——Starlette 的 `BaseHTTPMiddleware` 在 `await call_next(request)` 后,`response.body_iterator` 被消费一次就清空;我们 `cache.set` 后重新 `Response(content=body, ...)` 包回,headers 和 status_code 都保留,s11 再读时拿到完整 bytes + headers,日志字段正常落地。
- **路径 `/v1/chat/completions` 是单前缀** —— 不再有双前缀债务。s09 改 `app.mount("/", s08_app)` 后,整条链路(s12 → s11 → s10 → s09 → s08)对外的 chat 路径就是 `/v1/chat/completions`,中间件路径检查按真实可达路径写。

## 设计选择

- **TTL 用 `time.monotonic()` 而不是 `time.time()`** ——系统时钟跳变(NTP 校时、跨时区)时 `time.time()` 会回退或跳跃,可能把缓存集体判过期或集体"复活",`monotonic` 只往前走,语义干净;代价是不能拿"绝对时间戳"看"这条缓存是何时写入的"——要那信息得另存一个 wall-clock 字段。
- **`sha256` 而不是 `md5`** (cryptographic hash,把任意长度字节映成固定长度摘要)——sha256 输出 64 hex 字符,md5 输出 32;两者都不会真出碰撞(就算用 md5 也几乎不会冲突),选 sha256 是行业惯例、和 JWT header alg 命名一致,可读性稍好。
- **`sort_keys + separators=(",", ":")` 的紧凑序列化** (字段按字母排序 + 紧凑分隔符)——保证 `{"a":1,"b":2}` 和 `{"b":2,"a":1}` 算同一个 key,改一个字符(哪怕加个空格)就完全不一样;代价是序列化输出不漂亮——但反正不进日志,只在 key 计算时用。
- **TTL 固定 300s 不做配置** ——本章先让缓存"生效";配置粒度是引入 config 层之后的副产物,本章不背这个复杂度。
- **`Response(content=body, ...)` 重包而不是直接返回原 `response`** ——`body_iterator` 被读一次后就没了,不重包下游拿到空响应;重包保留 status_code + headers,FastAPI 客户端感知不到这是从缓存来的。

## 下章预告

s12 缓存保证"快",但上游瞬时失败没有重试。s13 失败 → 标记渠道 → 换下一个,直到成功或全失败。