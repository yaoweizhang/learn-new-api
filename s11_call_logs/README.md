# s11: 异步调用日志(buffer + flush_loop) — 中间件 peek body,后台 100ms 批量 flush

> Previous: [s10](../s10_channel_management/) · Next: [s12](../s12_caching/)

> *"100ms 异步刷一次"* —— 日志不该卡请求主流程。

> **Layer**：L5 运维与可观测

## 本章要做什么

前 10 章里,每条 `/v1/chat/completions` 走完一遍就消失了:上游返回 200 → FastAPI 把 JSON 塞进响应 → 客户端拿到结果 → 服务端把这次调用忘得一干二净。用户报"刚刚那次调用挂了三秒",你只能凭记忆猜——是配额耗尽、渠道挂了、还是限速?运营问"昨天 gpt-4o-mini 调用了多少次",你只能去上游控制台看。

要解决这个,在请求路径上挂一份"异步落日志":中间件在 chat 端点返回 200 时把一行日志塞进内存缓冲(`_buffer`),后台 `flush_loop` 每 100ms 整段搬到落盘列表(`_flushed`),管理员通过 `/admin/logs` 和 `/admin/stats` 看。学完你看到每条调用"在飞 + 已落盘"两条痕迹:

1. **挂一个 `LogMiddleware` —— 为什么用中间件而不是在 handler 里调函数**: `@app.middleware("http")` 装在 s11 这层 app 上,包裹下面 s10 → s09 → ... 整条挂载链。`dispatch` 在 `call_next(request)` 拿到 `response` 后判断:`response.status_code == 200` 且路径以 `/v1/chat/completions` 结尾,就把 `{"path", "ts", "status", "model"}` 一行塞进 `log_store.enqueue`。**为什么用中间件而非在 chat handler 里直接 enqueue**: 中间件对所有 chat 调用一视同仁(不依赖具体 handler 实现),后续 s13 改了挂载结构也照样能看到;**为什么只在 200 时记**: 4xx/5xx 不算"成功调用",s07 的配额结算失败、s08 的 429 限速、s10 的渠道故障,这些是另一类观测信号,留到 s16 才统一处理。

2. **中间件读 body 拿 model —— 为什么要在中间件解 JSON 而不是 `request.query_params`**: `model` 在请求体里、不在查询串里(Brief 原本写的是 `query_params.get("model")`,永远是 `"?"`——这是已知 bug)。中间件先 `await request.body()` 读出原始 bytes,`json.loads` 解出 `model` 写到 `request.state.model`,再用一个新的 `receive()` 把同一份 bytes 喂回去给下游 FastAPI。**为什么要重放 body**: Starlette/FastAPI 下游 handler 需要重新读 body 才能拿到 `messages`、做转发;不重放就 422。**这是 Starlette 里读取并回放 body 的标准手法**——body 被读两次,小开销换干净的"中间件读 model"语义。

3. **`LogStore` Protocol + `InMemoryLogStore` —— 为什么抽 Protocol 而不直接用类**: `LogStore` 是 `typing.Protocol`(Python 的结构性子类型,只靠方法签名匹配),只有 `enqueue / list / reset / drain_now` 四方法。默认实现是 `InMemoryLogStore`:`_buffer: deque[dict]` 写入端 + `_flushed: list[dict]` 读取端 + `threading.Lock` 串两条路。**为什么抽 Protocol**: (1) 测试可以用 duck-typed fake `set_default(rec)` 注入默认实例,验证中间件走的是注入路径;(2) v2 切 SQLite 时只换构造 `_default` 的语句,对外契约不变;**为什么用 deque+Lock 而非 asyncio.Queue**: 日志条目来自异步中间件,但 `/admin/logs` 可能从同步线程读,deque+Lock 对两种调用方都安全,asyncio.Queue 跨线程就得 `run_coroutine_threadsafe`,对一个内存实现来说重了。

4. **后台 `flush_loop` 每 100ms 搬运 —— 为什么异步搬不直接同步写**: `flush_loop` 在 `@app.on_event("startup")` 启动,`while not stop_event: await asyncio.sleep(0.1); drain_now()`。`drain_now()` 把 `_buffer` 整段搬到 `_flushed`。**为什么不直接同步写**: 单条 chat 调用几百 ms,瓶颈在等上游;落日志不能阻塞这条调用,100ms 批量 flush 把"每条调用写一次"的 IO 成本摊到"每 100ms 写一次";**为什么是 100ms**: 够短让运营不会看到空日志、够长让 IO 开销可忽略;`@app.on_event("shutdown")` 里再同步 drain 一次,兼容 TestClient 退出 `with` 块时事件循环已停、最后一批不能留在 buffer 里。

成品: `curl localhost:8011/v1/chat/completions` 触发一次 chat,等 100ms 后 `curl localhost:8011/admin/logs` 看到 `[{path, ts, status:200, model:"gpt-4o-mini"}]`,`curl localhost:8011/admin/stats` 看到 `{total:1, by_model:{gpt-4o-mini:1}}`。后续 s14 把这两个端点换成 Jinja2 浏览器页面;s16 把这一行的字段扩成 Prometheus 指标 + trace_id。

## 上一章复盘

s10 解决了挑通道，但每次调用有没有发生、在哪失败、谁打的——全靠脑补。

## 在整体中的位置

可观测性的"原始信号"层——s14 dashboard 从 s11 的 log_store 读，s16 提供单独的 trace + metric 中间件。

## 设计要点

日志存储是一个 `LogStore` 协议,默认实现是 `InMemoryLogStore`。测试可以调
`set_default(...)` 注入一个 recording fake;后续章节(s_full 之后)也复用
同一个形状。

## 问题

前 10 章里,每条 `/v1/chat/completions` 请求走完一遍就消失了:上游返回 200 → FastAPI 把 JSON 塞进响应 → 客户端拿到结果 → 服务端把这次调用忘得一干二净。一旦对外服务,三个问题立刻出现:

1. **看不到用量**。运营要回答"昨天 gpt-4o-mini 调用了多少次"只能看上游控制台,看不到自己这一层的总量和拆分。
2. **出了问题无法排查**。某个用户报"刚刚那次调用挂了三秒",没有日志就只能凭记忆猜:是配额耗尽?是某个渠道挂了?是限流?
3. **没法做对账**。配额系统(s07)扣的是预估量,事后才知道真实用量;没有调用日志,对账就是凭空。

说白了,我们需要一张"调用日志表":每条成功的 chat 调用记一行(路径、时间、状态码、model),然后异步落库(这一章只落内存,v2 落数据库),暴露一个 `/admin/logs` 让管理员看、一个 `/admin/stats` 按 model 聚合统计。

## 方案

引入两个最小部件:

- **`s11_call_logs/log_store.py`** —— `LogStore` 协议 + `InMemoryLogStore` 默认实现。`LogStore` 是个 `Protocol`（Python 的结构性子类型，仅靠方法签名匹配），只有 `enqueue` / `list` / `reset` / `drain_now` 四个方法——这四条是这个抽象对外的全部契约。实现是一个 `threading.Lock` + `deque` 缓冲 + `list` 落盘列表:`enqueue` 把一行塞进缓冲,后台 `flush_loop` 每 100ms 把整段搬到 `list`。重置用 `reset_logs()`。模块层保留 `enqueue`/`list_logs` 等 thin wrapper 转发到一个 `_default` 实例,方便测试用 `set_default(rec)` 注入假实现。
- **`s11_call_logs/code.py`** —— FastAPI 装配。挂载 s10 整块 app,在自己身上新增一个中间件(`LogMiddleware`)和两条管理员路由(`/admin/logs`、`/admin/stats`)。中间件在 `/v1/chat/completions` 返回 200 时把响应日志塞进 default 实例,由后台循环搬运到落盘列表。

路由形状——下面这张块状路由表把本章要写的 3 条接口压成一览:左是 `method + path`,中间是入参(`/v1/chat/completions` 接 `Authorization: Bearer API key` 和 body),右是返回码与返回体;本章要写的核心就是"转发 + 异步落日志 + 读日志/统计"三件事:

```
POST /v1/chat/completions      Bearer API key, body={model, messages...}   -> 200 + 异步日志
GET  /admin/logs                                                          -> 200 [log entries]
GET  /admin/stats                                                         -> 200 {total, by_model}
```

`/admin/logs` 和 `/admin/stats` 不挂 `_require_admin`(管理员闸门依赖,验证 token 是否带 is_admin=true)——和 s10 一样留到后续章节统一收紧(取舍里展开)。

## 工作原理

### `log_store.py`:Protocol + InMemory 实现

```python
class LogStore(Protocol):
    def enqueue(self, entry: dict) -> None: ...
    def list(self) -> list[dict]: ...
    def reset(self) -> None: ...
    def drain_now(self) -> None: ...

class InMemoryLogStore:
    def __init__(self, flush_interval: float = 0.1) -> None:
        self._lock = threading.Lock()
        self._buffer: deque[dict] = deque()
        self._flushed: list[dict] = []
        ...

    def enqueue(self, entry):
        with self._lock:
            self._buffer.append(entry)

    async def flush_loop(self, stop_event):
        while not stop_event.is_set():
            await asyncio.sleep(0.1)
            self.drain_now()

    def list(self):
        with self._lock:
            return list(self._flushed)

_default: LogStore = InMemoryLogStore()

# 模块层 thin wrappers —— 历史 call sites 保持不变
def enqueue(entry): _default.enqueue(entry)
def list_logs(): return _default.list()
def reset_logs(): _default.reset()
def _drain_now(): _default.drain_now()
async def flush_loop(stop_event): ...  # 包了一层 for s11/code.py

def set_default(store): ...   # 测试专用 seam
def get_default() -> LogStore: ...
```

- `_buffer` 是写入端,写一行塞一个;`_flushed` 是读取端,`/admin/logs` 直接读它——两块都在具体实现 `InMemoryLogStore` 实例上,锁也是实例的。
- 后台 `flush_loop` 每 100ms 把 `_buffer` 整段搬到 `_flushed`。单进程内同步原语够用,跨进程的 flush 留给 v2。
- 模块层 `_default` 是懒加载的默认实例;测试可以 `set_default(fake)` 注入一个 duck-typed 的 fake,验证中间件真的走了我们的注入路径。

为什么不用 `asyncio.Queue`?因为日志条目来自中间件(在 asyncio 上下文里),但运维接口和测试代码可能从同步线程直接读。`deque + threading.Lock` 对两种调用方都安全;`asyncio.Queue` 跨线程就需要 `run_coroutine_threadsafe` 包一层,对一个内存实现来说重了。

**为什么抽 `LogStore` Protocol 而不是直接用 `InMemoryLogStore`?** 两件事今天就受益:(1) 测试可以用 `Recording` 这种 duck-typed fake 替换 default,验证中间件真的不直接读模块全局;(2) 日后切 SQLite 实现时只换一个构造模块层 `_default` 的语句,对外契约不变。

### `code.py`:FastAPI 装配 + 中间件

```python
class LogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # 1) 进入:如果是 chat 端点,先把 body 读出来、解出 model、
        #    把同一份 bytes 通过新的 receive() 重新喂给下游。
        if request.url.path.endswith("/v1/chat/completions") and request.method == "POST":
            body_bytes = await request.body()
            request.state.model = "?"
            try:
                payload = json.loads(body_bytes or b"{}")
                if isinstance(payload, dict) and "model" in payload:
                    request.state.model = payload["model"]
            except Exception:
                pass
            async def receive():
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            request._receive = receive

        response = await call_next(request)

        # 2) 返回:状态 200 时把响应 body 读出来、再装回去,
        #    同时把这条调用塞进 _buffer。
        if request.url.path.endswith("/v1/chat/completions") and response.status_code == 200:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk if isinstance(chunk, bytes) else chunk.encode()
            async def iterbody():
                yield body
            response.body_iterator = iterbody()
            log_store.enqueue({"path": ..., "ts": time.time(),
                               "status": 200, "model": request.state.model})
        return response
```

三个关键点:

1. **`request.state.model` 把 model 从 body 传到中间件**。Brief 原本用 `request.query_params.get("model", "?")`——这是 bug,因为 model 在请求体里,不在查询串里。我们先 `await request.body()` 读出原始 bytes,`json.loads` 解出 `model`,写到 `request.state.model`;然后用一个新的 `receive()` 函数把同样的 bytes 重新喂回去,下游 FastAPI 看到的就是"这次请求没动过"。这是 Starlette 里读取并回放 body 的标准手法。
2. **`body_iterator` 替换**(Starlette/FastAPI 响应体的异步字节迭代器)。下游已经把响应体包成异步迭代器,我们要读出全部 bytes 用 `enqueue` 入队,就要把迭代器换成我们自己生成同一份 bytes 的版本。**本章测试只覆盖非流式路径(流式下 body_iterator 替换会破坏流,我们没有特殊处理)**——见取舍。
3. **挂载顺序**:`app.mount("/", s10_app)` 放在最后。Starlette 按注册顺序匹配路由;先 mount 会让 `Mount("/")` 吸收掉 `/admin/logs`、`/admin/stats`。

后台任务:

```python
@app.on_event("startup")
async def _start_flusher():
    global _stop_event, _task
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(log_store.flush_loop(_stop_event))

@app.on_event("shutdown")
async def _stop_flusher():
    if _stop_event is not None:
        _stop_event.set()
    log_store._drain_now()  # 最后同步落一次盘
```

`on_event("shutdown")` 里多做一次同步 drain,是为了兼容 TestClient——测试在 `with TestClient(app)` 块结束后立刻读 `list_logs()`,而此刻事件循环已经停掉,`flush_loop` 不会再有下一次 tick;同步 drain 保证最后一批条目不会丢在 buffer 里。

### `/admin/stats`:按 model 聚合

```python
@app.get("/admin/stats")
def stats():
    logs = log_store.list_logs()
    by_model = {}
    for entry in logs:
        by_model[entry["model"]] = by_model.get(entry["model"], 0) + 1
    return {"total": len(logs), "by_model": by_model}
```

最简单的"按 key 计数"。v2 会加 prompt/completion tokens、quota 消耗、成功率、p99 时延。

## 运行

```bash
# 起服务
python s11_call_logs/code.py        # PORT 默认 8011

# 准备一个普通用户 + 一个 API key(这里走的是 s05 的内存 key 表,
# 不是 s09 的 JWT——chat 端点的鉴权在 s08,是 API key 那一路)
python -c "
from s05_api_key_auth.storage import register_key
register_key(user_id='u1', key='sk-test')
"

# 调用 chat
curl -s -X POST http://localhost:8011/v1/chat/completions \
  -H 'authorization: Bearer sk-test' \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'

# 等 100ms 让 flush_loop 跑一次,然后看日志
curl -s http://localhost:8011/admin/logs
# -> [{"path":"/v1/chat/completions","ts":...,"status":200,"model":"gpt-4o-mini"}]

curl -s http://localhost:8011/admin/stats
# -> {"total":1,"by_model":{"gpt-4o-mini":1}}
```

## → new-api 源码

真实部署里同样的"调用日志"长这样(路径区分大小写,Windows 上看着像 `Log.go`、Linux/macOS 是 `log.go`):

- `model/log.go` —— `Log` struct 定义 + GORM 映射 + 表名钩子。字段远多于我们这一章的最小集:`user_id / token_id / model / prompt_tokens / completion_tokens / quota / elapsed_ms / is_stream / status_code / request_body / response_body / ...`。这里的字段是生产级别,我们这里的 `dict` 是教学最小集,含义对应。
- `service/log_info_generate.go` —— 负责把请求/响应翻译成"调用日志行"的业务逻辑:把 `prompt_tokens + completion_tokens` 算出来、扣 quota、决定要不要落库(异步 channel)。和我们这里的 `LogMiddleware.dispatch + log_store.enqueue` 是同一层。

> Windows 文件系统不分大小写,本地 IDE 里看着像 `Log.go` 不少见;部署到 Linux/macOS 时按实际的小写路径访问。

## 取舍

- **纯内存存储,进程一重启日志全丢** —— YAGNI。生产里调用日志是高频写入 + 需要长期查询的数据,必须走数据库;v2 切 Postgres 时把 `enqueue` 改成 `INSERT ... RETURNING id`,`list_logs` 改成带分页的 `SELECT`。教学版先保证"看得见、能聚合"。
- **没有流式(SSE)调用的日志** —— 中间件里 `body_iterator` 替换对流式响应会破坏流(迭代器只能读一次,我们读完后塞回的那份已经丢失了"分块"语义)。本章测试只覆盖非流式路径,**SSE 流式调用不会进日志**——这是已知缺口,README 和测试都标了出来。v2 要么用 FastAPI 的 `add_event_handler` 在流式响应结束时钩一次,要么干脆放弃中间件、改在 chat_completions 函数体里直接 `enqueue`——当前 s11 链上 s08 是这条 handler 的最终注册点,以后的章节里(s13 等)如果把 chat 路由提到本地,就在本地那个 chat_with_retry 里 enqueue。
- **`model` 通过 `request.state.model` 传递** —— Brief 原本写的是 `request.query_params.get("model", "?")`,永远是 `"?"` 因为 model 在 body 里。本章的修正手法是:中间件先 `await request.body()` 读出原始 bytes、解出 model 写到 `request.state.model`,再用一个新的 `receive()` 把同一份 bytes 喂回去给下游 FastAPI。这是 Starlette 标准做法;副作用是 body 会被读两次(小开销,kilobytes 级),换来干净的"中间件读 model"语义。
- **没有 `_require_admin` 闸门** —— `/admin/logs`、`/admin/stats` 当前对所有能访问的人开放。生产里必须收紧,但"管理员能看自己的调用日志"和"调用方能看到自己的用量"是两个不同的产品决策(前者运维、后者用户控制台),先分开再讨论统一鉴权。
- **`time.sleep(0.2)` 是已知的时序依赖** —— 见"测试"一节。Brief 原本就是这么设计的;这是"异步 + 周期 flush"的固有特性。v2 改成事件驱动后就消除。
- **`on_event("startup")` 已弃用** —— FastAPI 0.110+ 推荐用 lifespan context manager。本章沿用 brief 的写法保持一致;后续章节统一升级时一起改。
- **shutdown 钩子做同步 drain** —— 见上文 `code.py` 注释。直接动机是 TestClient:测试退出 `with` 块时事件循环停掉,async flush 不再 tick,最后一批会留在 `_buffer` 里没出来;同步 drain 把这一批强制落 `_flushed`。生产 uvicorn 关闭流程里 asyncio 自然会让最后一次 tick 跑完,同步 drain 仍然是无害的双保险。

## 下章预告

s11 存了所有调用,但同样的请求每次都重新转发贵。s12 加响应缓存,300s TTL 命中就跳过上游。
