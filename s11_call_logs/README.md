# s11: 异步调用日志（buffer + flush_loop）

> Previous: [s10](../s10_channel_management/) · Next: [s12](../s12_caching/)

## 设计要点

The log store is a `LogStore` Protocol with `InMemoryLogStore` as the default.
Tests can call `set_default(...)` to substitute a recording fake. Future
chapters (s_full onward) reuse the same shape.

## 问题

前 10 章里，每条 `/v1/chat/completions` 请求走完一遍就消失了：上游返回 200 → FastAPI 把 JSON 塞进响应 → 客户端拿到结果 → 服务端把这次调用忘得一干二净。一旦对外服务，三个问题立刻出现：

1. **看不到用量**。运营要回答"昨天 gpt-4o-mini 调用了多少次"只能看上游控制台，看不到自己这一层的总量和拆分。
2. **出了问题无法排查**。某个用户报"刚刚那次调用挂了三秒"，没有日志就只能凭记忆猜：是配额耗尽？是某个渠道挂了？是限流？
3. **没法做对账**。配额系统（s07）扣的是预估量，事后才知道真实用量；没有调用日志，对账就是凭空。

说白了，我们需要一张"调用日志表"：每条成功的 chat 调用记一行（路径、时间、状态码、model），然后异步落库（这一章只落内存，v2 落数据库），暴露一个 `/admin/logs` 让管理员看、一个 `/admin/stats` 按 model 聚合统计。

## 方案

引入两个最小部件：

- **`s11_call_logs/log_store.py`** —— `LogStore` 协议 + `InMemoryLogStore` 默认实现。`LogStore` 是个 `typing.Protocol`（Python 的结构性子类型/鸭子类型 + 类型注解，实现类只要方法签名对得上就算满足，不必显式继承），只有 `enqueue` / `list` / `reset` / `drain_now` 四个方法——这四条是这个抽象对外的全部契约。实现是一个 `threading.Lock` + `deque` 缓冲 + `list` 落盘列表：`enqueue` 把一行塞进缓冲，后台 `flush_loop` 每 100ms 把整段搬到 `list`。重置用 `reset_logs()`。模块层保留 `enqueue`/`list_logs` 等 thin wrapper 转发到一个 `_default` 实例，方便测试用 `set_default(rec)` 注入假实现。
- **`s11_call_logs/code.py`** —— FastAPI 装配。挂载 s10 整块 app，在自己身上新增一个中间件（`LogMiddleware`）和两条管理员路由（`/admin/logs`、`/admin/stats`）。中间件在 `/v1/chat/completions` 返回 200 时把响应日志塞进 default 实例，由后台循环搬运到落盘列表。

路由形状：

```
POST /v1/v1/chat/completions      Bearer API key, body={model, messages...}   -> 200 + 异步日志
GET  /admin/logs                                                          -> 200 [log entries]
GET  /admin/stats                                                         -> 200 {total, by_model}
```

`/admin/logs` 和 `/admin/stats` 不挂 `_require_admin`——和 s10 一样留到后续章节统一收紧（取舍里展开）。

## 工作原理

### `log_store.py`：Protocol + InMemory 实现

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

- `_buffer` 是写入端，写一行塞一个；`_flushed` 是读取端，`/admin/logs` 直接读它——两块都在具体实现 `InMemoryLogStore` 实例上，锁也是实例的。
- 后台 `flush_loop` 每 100ms 把 `_buffer` 整段搬到 `_flushed`。单进程内同步原语够用，跨进程的 flush 留给 v2。
- 模块层 `_default` 是懒加载的默认实例；测试可以 `set_default(fake)` 注入一个 duck-typed 的 fake，验证中间件真的走了我们的注入路径。

为什么不用 `asyncio.Queue`？因为日志条目来自中间件（在 asyncio 上下文里），但运维接口和测试代码可能从同步线程直接读。`deque + threading.Lock` 对两种调用方都安全；`asyncio.Queue` 跨线程就需要 `run_coroutine_threadsafe` 包一层，对一个内存实现来说重了。

**为什么抽 `LogStore` Protocol 而不是直接用 `InMemoryLogStore`？** 两件事今天就受益：(1) 测试可以用 `Recording` 这种 duck-typed fake 替换 default，验证中间件真的不直接读模块全局；(2) 日后切 SQLite 实现时只换一个构造模块层 `_default` 的语句，对外契约不变。

### `code.py`：FastAPI 装配 + 中间件

```python
class LogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # 1) 进入：如果是 chat 端点，先把 body 读出来、解出 model、
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

        # 2) 返回：状态 200 时把响应 body 读出来、再装回去，
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

三个关键点：

1. **`request.state.model` 把 model 从 body 传到中间件**。Brief 原本用 `request.query_params.get("model", "?")`——这是 bug，因为 model 在请求体里，不在查询串里。我们先 `await request.body()` 读出原始 bytes，`json.loads` 解出 `model`，写到 `request.state.model`；然后用一个新的 `receive()` 函数把同样的 bytes 重新喂回去，下游 FastAPI 看到的就是"这次请求没动过"。这是 Starlette 里读取并回放 body 的标准手法。
2. **`body_iterator` 替换**。下游已经把响应体包成异步迭代器，我们要读出全部 bytes 用 `enqueue` 入队，就要把迭代器换成我们自己生成同一份 bytes 的版本。对非流式响应（`JSONResponse`）完全没问题；流式响应会被这条捷径打乱，所以对 SSE 我们不替换——见取舍。
3. **挂载顺序**：`app.mount("/", s10_app)` 放在最后。Starlette 按注册顺序匹配路由；先 mount 会让 `Mount("/")` 吸收掉 `/admin/logs`、`/admin/stats`。

后台任务：

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

`on_event("shutdown")` 里多做一次同步 drain，是为了兼容 TestClient——测试在 `with TestClient(app)` 块结束后立刻读 `list_logs()`，而此刻事件循环已经停掉，`flush_loop` 不会再有下一次 tick；同步 drain 保证最后一批条目不会丢在 buffer 里。

### `/admin/stats`：按 model 聚合

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

# 准备一个普通用户 + 一个 API key（这里走的是 s05 的内存 key 表，
# 不是 s09 的 JWT——chat 端点的鉴权在 s08，是 API key 那一路）
python -c "
from s05_api_key_auth.storage import register_key
register_key(user_id='u1', key='sk-test')
"

# 调用 chat（注意：实际可访问路径是 /v1/v1/chat/completions，见取舍）
curl -s -X POST http://localhost:8011/v1/v1/chat/completions \
  -H 'authorization: Bearer sk-test' \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'

# 等 100ms 让 flush_loop 跑一次，然后看日志
curl -s http://localhost:8011/admin/logs
# -> [{"path":"/v1/v1/chat/completions","ts":...,"status":200,"model":"gpt-4o-mini"}]

curl -s http://localhost:8011/admin/stats
# -> {"total":1,"by_model":{"gpt-4o-mini":1}}
```

## 测试

```bash
pytest tests/test_s11_call_logs.py -v
```

两个测试覆盖主契约：

| 测试 | 断言 |
| --- | --- |
| `test_logs_written_after_call` | 走完一次 chat 调用 200 后，1 条日志出现在落盘列表，model 字段等于请求里写的 `gpt-4o-mini`。 |
| `test_injected_log_store_observes_calls` | 用 `set_default(rec)` 注入一个 recording fake，请求走完后 fake 里能看到同一行——证明中间件走的是 `_default` 实例而不是直接读模块全局。 |

测试里有一行 `time.sleep(0.2)`——这是已知的、可接受的时序依赖：`flush_loop` 真的是异步循环（`await asyncio.sleep(0.1)` + 加锁搬运），没有 event 就只能靠睡眠让出几个 tick。v2 会把 `_buffer → _flushed` 改成"每条 enqueue 后 fire 一个 Event"，测试就能去掉 `time.sleep`。本测试保留睡眠是为了对齐本章节"异步"的语义，并在注释里标出。

`_clean` fixture 额外重置了 `s05_api_key_auth.storage`、`s07_pre_consume_settle.quota`、`s08_rate_limiting.bucket`——这些是 chat 端点所依赖的内部状态，跨测试需要清零，否则会污染 s11 之后的测试。

## → new-api 源码

真实部署里同样的"调用日志"长这样（路径区分大小写，Windows 上看着像 `Log.go`、Linux/macOS 是 `log.go`）：

- `model/log.go` —— `Log` struct 定义 + GORM 映射 + 表名钩子。字段远多于我们这一章的最小集：`user_id / token_id / model / prompt_tokens / completion_tokens / quota / elapsed_ms / is_stream / status_code / request_body / response_body / ...`。这里的字段是生产级别，我们这里的 `dict` 是教学最小集，含义对应。
- `service/log_info_generate.go` —— 负责把请求/响应翻译成"调用日志行"的业务逻辑：把 `prompt_tokens + completion_tokens` 算出来、扣 quota、决定要不要落库（异步 channel）。和我们这里的 `LogMiddleware.dispatch + log_store.enqueue` 是同一层。

> Windows 文件系统不分大小写，本地 IDE 里看着像 `Log.go` 不少见；部署到 Linux/macOS 时按实际的小写路径访问。

## 取舍

- **纯内存存储，进程一重启日志全丢** —— YAGNI。生产里调用日志是高频写入 + 需要长期查询的数据，必须走数据库；v2 切 Postgres 时把 `enqueue` 改成 `INSERT ... RETURNING id`，`list_logs` 改成带分页的 `SELECT`。教学版先保证"看得见、能聚合"。
- **没有流式（SSE）调用的日志** —— 中间件里 `body_iterator` 替换对流式响应会破坏流（迭代器只能读一次，我们读完后塞回的那份已经丢失了"分块"语义）。本章测试只覆盖非流式路径，**SSE 流式调用不会进日志**——这是已知缺口，README 和测试都标了出来。v2 要么用 FastAPI 的 `add_event_handler` 在流式响应结束时钩一次，要么干脆放弃中间件、改在 s08 的 `chat_completions` 函数体里直接 `enqueue`。
- **`model` 通过 `request.state.model` 传递** —— Brief 原本写的是 `request.query_params.get("model", "?")`，永远是 `"?"` 因为 model 在 body 里。本章的修正手法是：中间件先 `await request.body()` 读出原始 bytes、解出 model 写到 `request.state.model`，再用一个新的 `receive()` 把同一份 bytes 喂回去给下游 FastAPI。这是 Starlette 标准做法；副作用是 body 会被读两次（小开销，kilobytes 级），换来干净的"中间件读 model"语义。
- **没有 `_require_admin` 闸门** —— `/admin/logs`、`/admin/stats` 当前对所有能访问的人开放。生产里必须收紧，但"管理员能看自己的调用日志"和"调用方能看到自己的用量"是两个不同的产品决策（前者运维、后者用户控制台），先分开再讨论统一鉴权。
- **外路径 `/v1/v1/chat/completions` 是双前缀** —— s09 把 s08 挂在 `/v1` 下，而 s08 自己又用 `/v1/chat/completions`，结果是整条链路（s11 → s10 → s09 → s08）对外的 chat 路径变成 `/v1/v1/chat/completions`。本测试已经按实际可达路径写。README 这里只点出来；修正 `app.mount("/v1", s08_app)` 到 `app.mount("/", s08_app)` + 把 s08 改名 `/chat/completions` 是后续章节清理接口契约时一起做。
- **`time.sleep(0.2)` 是已知的时序依赖** —— 见"测试"一节。Brief 原本就是这么设计的；这是"异步 + 周期 flush"的固有特性。v2 改成事件驱动后就消除。
- **`on_event("startup")` 已弃用** —— FastAPI 0.110+ 推荐用 lifespan context manager。本章沿用 brief 的写法保持一致；后续章节统一升级时一起改。
- **shutdown 钩子做同步 drain** —— 见上文 `code.py` 注释。直接动机是 TestClient：测试退出 `with` 块时事件循环停掉，async flush 不再 tick，最后一批会留在 `_buffer` 里没出来；同步 drain 把这一批强制落 `_flushed`。生产 uvicorn 关闭流程里 asyncio 自然会让最后一次 tick 跑完，但同步 drain 仍然是无害的双保险。