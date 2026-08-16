# s_full——生产形态整合版 — 教学挂载 → 生产装配,include_router 替代 mount

> Previous: [s16](../s16_observability/) · Next: —

> *"16 章合一就是生产形态"* —— 教学挂 mount，生产 include_router。

> **Layer**：LX 整合形态

## 本章要做什么

经过 s01-s16 的逐步拆解,16 个章节各自能跑、能测,但都是教学形态:每个 chapter 自己的 `code.py` 是一个独立 FastAPI app,s16 通过 `app.mount("/", s15_app)` 把 15 → 14 → ... → 02 串成一条链。**生产里这条路走不通**——挂载链会让路径被前缀化(挂到 `/` 之外的子路径时整条链的路径都得跟着改),路由散落在 16 个 app 里排查时要顺着 mount 追,新读者打开 `s16/code.py` 看到的不是入口而是 16 层 mount。

要解决这个,把 16 章的功能**重新装配**成一个独立的 FastAPI app:不再挂载 chapter chain,改为在 `s_full/code.py` 入口用 `app.include_router(...)` 把 `routes/auth.py / routes/admin.py / routes/chat.py` 三个本地 router 接进来;目录按 `routes / services / models / adapters / middleware` 五层重组,对应 `new-api` 的 `Router → Controller → Service → Model → Middleware`。本章就把这个"教学挂载 → 生产装配"的过程做出来:

1. **把 16 章代码复制到 `s_full/` 下的五层子目录 —— 为什么是复制而不是 import**: `routes/auth.py` ← s09 的注册/登录、`routes/chat.py` ← s04+s05+s06+s07+s08 的 chat 转发链路、`services/quota.py` ← s07 的预扣结算、`services/rate_limit.py` ← s08 的 token bucket、`models/user.py` ← s09 的 SQLite+bcrypt、`adapters/{openai,claude,gemini}.py` ← s04 的 Provider ABC、`middleware/trace.py` ← s16 的 `TraceAndMetricsMiddleware`。**为什么不 `from s09_user_system.auth import router` 跨章 import**:(a) tutorial 章节本身要保持自包含可读,跨章 import 会把"s09 跑得动"绑到"s07 的某个内部细节没改";(b) `s_full` 要展示**独立项目**的目录长什么样——独立项目不能 import 教学章节;(c) pytest collection 时跨章 import 容易触发意料之外的初始化副作用(比如 s07 启动时把 `models/user.py` 的 sqlite 文件创建到 s07 自己的 cwd)。

2. **入口用 `include_router` 替代 `app.mount` —— 为什么 production 不挂载 chapter chain**: `s_full/code.py` 写 `app.include_router(auth.router)` / `app.include_router(admin.router)` / `app.include_router(chat.router)`,**不**挂任何 chapter。**为什么不挂**:(a) `app.mount("/...", sNN_app)` 会让 mounted 子 app 的 routes 注册到挂载点,生产里如果把整应用挂到 `/api/v1` 下,内部 16 层的路径都要再前缀化一次——一旦 mount 链某层忘了加前缀,客户端 404 排查要追 16 层;(b) 教学形态用 mount 是为了**章节之间能复用前章的代码**(s02 直接 import s01 的 `app`,这样 s02 的 README 只讲"换协议"那一件事,前面"能转发"那一章就被复用掉了),生产里功能被独立组织后,这种复用不再必要;(c) 路由散落在 16 个 app 里,新读者打开 `s16/code.py` 看到 `app.mount("/", s15_app)` 第一反应是"这是入口吗?再追 15 层才知道"——`include_router` 让 `code.py` 一眼看到全部对外路由。

3. **挂上 `TraceAndMetricsMiddleware` 和日志 flush loop —— 为什么这两个必须随装配一起搬**: `app.add_middleware(TraceAndMetricsMiddleware)` 装在 s16 那层,s_full 直接把同一个中间件挂到自己的 app 上(代码逐字复制,不动逻辑),`prometheus_client.generate_latest()` 通过 `app.get("/metrics")` 暴露;`models/log.py` 的 `flush_loop` 在 `@app.on_event("startup")` 启动、`@app.on_event("shutdown")` 停。**为什么不能只复制 routes 不复制中间件**:(a) `chat.py` 里的 `p: Principal = Depends(require_api_key)` 依赖 `middleware/auth.py` 注入 Principal,auth 必须在 chat 之前 import;(b) `routes/chat.py` 调 `models/log.enqueue_log()` 记调用,`flush_loop` 不启动日志永远不落盘;(c) `TraceAndMetricsMiddleware` 是唯一给 `/metrics` 喂数据的地方,不挂中间件 `/metrics` 就是空响应。

4. **保留教学里的所有 invariant —— 为什么 s_full 不"优化"任何细节**: `_pick(model)` 仍按模型前缀选 provider(`gpt-*` → OpenAI、`claude-*` → Anthropic、`gemini-*` → Google),跟 s04 保持一致;`Principal` 从 `middleware/auth.py` 注入,跟 s08 修正后的 typed-parameter 模式一致;`quota.settle` 双向结算(超量补扣、节约退还)对齐 s07。**为什么不借机"加" channel pool / retry / caching**: 这些是 s13 / s12 / s10 的独立主题,s_full 是"装配"不是"扩展"——一旦在装配视图里偷偷加新功能,读者对照"目录映射"那一节就会发现"文件多了一行注释里没写",装配视图的诚实性就破了。**已知限制**(跟教学一致):没有 channel pool 真实选路、没有 retry/fallback、没有 caching、流式 4xx 客户端拿到 200+空 body(Starlette 头已发)、admin 操作没审计。

成品: `python s_full/code.py`(或 `python -m s_full.code`)拿到单一 FastAPI app,端口默认 8099,`curl localhost:8099/health` 看到 `{"status":"ok"}`,`curl -X POST localhost:8099/auth/signup -d '...'` 注册、`/auth/login` 拿 token、再 `curl -X POST localhost:8099/v1/chat/completions -H 'Authorization: Bearer ...'` 走完整 16 章链路(限流 → 预扣 → 选 provider → 转发 → 结算 → 记日志),`curl localhost:8099/metrics` 看到 Prometheus 指标。`tests/test_s_full_smoke.py` 14 个 smoke 测试覆盖完整端到端流程。**后续**: 这就是教程终点——再往下是部署(s15 已演示)、告警规则、生产级 channel pool,都是工程实践不是教学。

## 上一章复盘

s16 是教学最后一章,代码结构是 mount chain,目录扁。

## 在整体中的位置

16 章的"生产装配"——把 mount 换成 include_router,目录拆成 routes / services / models / middleware,代码仍是同一份。

> 这是教程最后一章,把 s01-s16 的所有功能**重新组织**成一个生产形态的应用,目录结构对齐 `new-api` 的 `Router → Controller → Service → Model`。
>
> **本章不引入任何新功能**——把同样的代码整理到更干净的目录里,让读者理解"教学拆解 vs 生产目录"之间的对应关系。

---

## 问题

经过 s01-s16 的逐步拆解，我们拥有：

- 16 个独立的 FastAPI app（每个 chapter 都有自己的 `code.py`）
- 通过 `app.mount("/...", sNN_app)` 串成一条链：`s16 -> s15 -> s14 -> ... -> s02`
- 每个 chapter 内部都有自己的 routes / services / models，但都塞在 `sNN_topic/` 一个目录下

这适合教学（每个 chapter 独立可读、独立可测），但**不像真实项目**。读者去看 `new-api` 仓库，看到的是另一种目录风格：

下面这张目录示意把 `new-api` 的标准目录列出来——每一行是一个目录分类，旁边注释说明它在新-api 里的职责（gin 的 Handler / 业务编排 / 业务逻辑 / 数据模型 / 中间件 / 公共工具）；`s_full` 的目标就是把这套目录结构用 FastAPI 复刻一遍。

```
new-api/
  router/      # 路由层（gin 的 Handler）
  controller/  # 业务编排
  service/     # 业务逻辑
  model/       # 数据模型
  middleware/  # 中间件
  common/      # 公共工具
```

`s_full` 要回答：**"如果把教程里累积的所有功能放到这套目录里，应该长什么样？"**

---

## 方案

把 s01-s16 的代码**复制**到 `s_full/` 下的清晰子目录里，对外提供**单一** FastAPI app。`s_full` 拥有自己的 routers，不挂载 chapter chain。

### 目录映射

下面这张目录映射表把 s_full 的子目录与各自章节来源一一对应——每行是一个文件，注释里写职责 + 来源章节；左列是新目录（`routes/services/models/adapters/middleware` 五层），右列是章节溯源（`← s05` 之类），回答"独立项目该长什么样"。

```
s_full/
  code.py               # 入口：拼装 app、include_router、add_middleware
  routes/               # 路由层（new-api: router/）
    auth.py             # /auth/signup, /auth/login, /me      ← s09
    admin.py            # /admin/channels, /admin/logs, /admin/stats
                        #   ← s10 + s11 admin surface
    chat.py             # /v1/chat/completions                ← s04+s05+s06+s07+s08
  services/             # 业务层（new-api: service/）
    quota.py            # deduct/refund/settle                 ← s07
    rate_limit.py       # token bucket                          ← s08
    billing.py          # top_up + pre_consume + settle         ← 组合 s06+s07
  models/               # 数据层（new-api: model/）
    user.py             # SQLite + bcrypt                       ← s09
    channel.py          # in-memory + locks                     ← s10
    log.py              # deque + flush loop                    ← s11
  adapters/             # Provider ABC（new-api: relay/adaptor/）
    base.py             # Provider ABC                           ← s04
    openai.py           # OpenAI wire format
    claude.py           # Anthropic Messages
    gemini.py           # Google generateContent
  middleware/           # 中间件（new-api: middleware/）
    auth.py             # require_api_key + issue_token          ← s05+s09
    trace.py            # TraceAndMetricsMiddleware              ← s16
```

---

## 工作原理

### `/v1/chat/completions` 的请求生命周期

下面这张 ASCII 流程图把一次 chat 请求从进入到出画出来——图里有客户端、本章要写的 `TraceAndMetricsMiddleware`、`chat_completions` handler 三个角色。纵向是步骤顺序，`▶` 往下走、`▼` 进下一段；本章写的是把 s01-s16 累积的功能串成单一入口。

```
HTTP POST /v1/chat/completions
    │
    ▼
TraceAndMetricsMiddleware           ← s16：trace_id + Prometheus 计数
    │
    ▼
chat_completions(req, p=Depends(require_api_key))
    │
    │  ① 鉴权：require_api_key 解析 JWT -> Principal{user_id, email, is_admin}
    │     （注意：用 typed-parameter `p: Principal = Depends(...)`，不是 `dependencies=[...]`；
    │      后者不会把依赖结果注入 handler 签名，参见 s08 review）
    │
    │  ② 限流：rate_limit.take(p.user_id) → 429 if False
    │
    │  ③ 计量 + 预扣：billing.pre_consume() → 402 if insufficient
    │     · count_prompt 用 tiktoken（OpenAI）或 char/4（其它）
    │     · 预扣 = (prompt_tokens + expected_completion) × RATE_PER_TOKEN
    │
    │  ④ 选 Provider：adapters/{openai,claude,gemini}.py  按 model 前缀匹配
    │
    │  ⑤ to_upstream()：把 OpenAI wire 转成各上游格式
    │
    │  ⑥ 调上游：httpx.AsyncClient.post(url, body_bytes, headers)
    │
    │  ⑦ from_upstream()：把上游响应翻译回 OpenAI wire
    │
    │  ⑧ 结算：billing.settle() 用上游报的 `prompt/completion_tokens`
    │     直接算 `actual`，然后调 `quota.settle()`：
    │       · actual < estimate → 退还差额（estimate - actual）
    │       · actual > estimate → 补扣超出（actual - estimate）
    │     其中 `quota.settle` 是双向的（mirror s07）。
    │     上游未报 usage（SSE 流式常见）→ 把 estimate 当作 actual。
    │
    │  ⑨ 记日志：models/log.enqueue_log()（deque，100ms 异步 flush）
    │
    ▼
JSONResponse(translated)
```

### 为什么不挂载 chapter chain

`s16` 通过 `app.mount("/", s15_app)` 把 16 个 chapter 串起来，让 `s01` 的代码可以**直接被外层 s02-s16 重用**——这是教学方便。但到了 `s_full`，三件事变了：

1. **每个章节已经有自己独立的 routers**（`routes/auth.py` 等），不需要借 chapter 的实现。
2. **`app.mount` 会让 mounted 子 app 的 routes 注册到挂载点**——`s_full` 不希望 `/v1/chat/completions` 后面再跟一个 `/v1/chat/completions` 的别名链。
3. **生产形态的应用边界是显式的**——读者应该看到一个清楚的入口，而不是 17 层 mount。

所以 `s_full/code.py` 走 `app.include_router(...)`，**不挂载**任何 chapter。

### 关于 `request.state.principal` 的陷阱

教程早期版本曾在路由上写：

```python
@router.post("/v1/chat/completions", dependencies=[Depends(require_api_key)])
async def chat_completions(req, request: Request):
    p: Principal = request.state.principal  # 永远是 None
```

`dependencies=[Depends(...)]` **不会**把依赖结果塞到 `request.state`，也不会注入 handler 签名。修正：

```python
async def chat_completions(req, p: Principal = Depends(require_api_key)):
    # FastAPI 看到签名里有 Principal 参数，自动调用 require_api_key 并传入
```

s08 的 review 已经切换到 typed-parameter 模式；`s_full` 一开始就采用这个写法。

---

## 运行

```bash
# 安装依赖（一次性）
pip install -r requirements.txt

# 启动 s_full
python -m s_full.code

# 或指定端口
PORT=8099 python -m s_full.code
```

启动后访问：

- `GET  /health`             —— 健康检查
- `GET  /metrics`            —— Prometheus 指标
- `POST /auth/signup`        —— 注册
- `POST /auth/login`         —— 登录（返回 access_token）
- `GET  /me`                 —— 当前用户信息（需 Bearer token）
- `POST /admin/channels`     —— 创建 channel（需 admin token）
- `GET  /admin/channels`     —— 列出 channel
- `GET  /admin/logs`         —— 列出 call logs
- `GET  /admin/stats`        —— 按 model 聚合的调用统计
- `POST /v1/chat/completions`—— 业务主入口（需 Bearer token；自动 rate limit + quota + 上游转发）

### 配置环境变量

```bash
export UPSTREAM_OPENAI_KEY="sk-..."     # OpenAI 上游 key
export UPSTREAM_CLAUDE_KEY="sk-ant-..." # Anthropic 上游 key
export UPSTREAM_GEMINI_KEY="AIza..."    # Google 上游 key
export JWT_SECRET="..."                 # JWT 签名密钥（默认 "change-me-in-production"）
```

---

## → new-api 源码

| s_full 组件              | new-api 对应位置                                  | 备注 |
|--------------------------|--------------------------------------------------|------|
| `routes/chat.py`         | `router/relay-router.go` → controller/relay → service/RelayService | Gin handler → Go controller → service 三层；FastAPI 的 `APIRouter` 兼路由+编排，所以这里只有路由文件 |
| `routes/auth.py`         | `router/api-router.go` 中 `/api/user/register`   | new-api 用 gin；教程用 FastAPI |
| `routes/admin.py`        | `router/api-router.go` 中 `/api/channel/*`        | 同上 |
| `services/quota.py`      | `service/quota.go`                                | new-api 走 Redis Lua 脚本保证原子扣减；教程用 `threading.Lock` + dict 保持可读性 |
| `services/rate_limit.py` | `service/ratelimit.go`                            | new-api 走 Redis token bucket；教程是 in-memory |
| `services/billing.py`    | `service/pre_consume_quota.go`                    | 同上 |
| `models/user.py`         | `model/user.go` + `model/userquota.go`            | 教程把 user 表和 quota 表都用 SQLite；new-api 是 MySQL + 单独 quota 表 |
| `models/channel.py`      | `model/channel.go`                                | 教程 in-memory；new-api 是 DB-backed |
| `models/log.py`          | `model/log.go`                                    | 教程 deque + 100ms flush；new-api 是 MySQL 批量 insert |
| `adapters/*.py`          | `relay/adaptor/{openai,anthropic,gemini}/adaptor.go` | new-api 每个 provider 一个子包；教程单文件更易读 |
| `middleware/auth.py`     | `middleware/auth.go`                              | new-api 走 JWT；教程同样 |
| `middleware/trace.py`    | `middleware/trace.go` + `middleware/metrics.go`  | new-api 用 OpenTelemetry；教程用 prometheus_client |

### 一个重要区别

new-api 的 **relay** 不只是"按模型前缀选 provider"——它会维护一个 channel pool，每个 channel 是某个上游的实例，按 (priority, weight, healthy) 排序自动 failover。`s_full` 当前**不做 channel pool 的真实选路**——`routes/chat.py` 直接按模型前缀选 provider，与 s04 保持一致（s04 也只挑 provider，不挑 channel）。channel pool 的真正使用参见 s13 (retry + fallback) 和 s14 (admin dashboard)。如果以后要扩展 channel failover，需要在 `routes/chat.py` 里加一个新选择函数（按 priority/weight 在同 provider 的 channel 里挑一条）。

---

## 取舍

### 决策

- **复制而不是 import**：s07/s08/s09/s10/s11/s16 的代码被**逐字复制**到 `s_full/` 下的对应位置。**不** `from s07_pre_consume_settle.quota import deduct` 之类的跨章节导入。三个理由：
  1. tutorial 章节本身要保持自包含可读；
  2. `s_full` 要展示"如果这是一个**独立**项目，目录应该长什么样"——重新组织就意味着不引用。
  3. 跨章节 import 在 pytest collection 时容易触发意料之外的初始化副作用。
- **routers 而不是 mount**：`s_full/code.py` 走 `include_router`，**不**挂载任何 chapter chain。chapter chain 是教学形态（s16 -> s15 -> ... -> s02），`s_full` 是生产形态。
- **`Principal` 定义在 `middleware/auth.py`**：在 s05/s08/s09 里 Principal 是 `dataclass(user_id, scopes)`；`s_full` 把 `email` 和 `is_admin` 也加进来，因为 JWT 本身带这俩字段。`user_id` 由 `int` 取代 `str`，对齐 s09 的 SQLite 主键类型。
- **billing.pre_consume 抛 `PermissionError` 而不是 `HTTPException`**：保持 service 层不依赖 HTTP。路由层 catch 后翻译成 402。

### 已知限制（与教程目标一致，YAGNI）

- **没有 channel pool 的真实选路**：`_pick(model)` 只按模型前缀选 provider —— s_full 的 `Channel` 模型只保留 `id` + `base_url` + `enabled`,没有 `provider` 字段,`mark_unhealthy` 函数也移除了,因为 s_full 没有 channel pool 选路、所有渠道都按模型前缀直接派发。如果要扩展 channel failover,需要:重新加 `provider` 字段、恢复 `mark_unhealthy`、在 `routes/chat.py` 里加 channel 选路调用。
- **没有 retry / fallback**（参见 s13）：单次上游调用失败直接 refund + 502。
- **没有 caching**（参见 s12）：同样的 prompt 不会走 prompt cache。
- **streaming 部分支持**：客户端发 `stream=true` 时走 `StreamingResponse`
  把上游 SSE 字节原样透传（OpenAI / Claude 已通）。每个 `Provider`
  标了 `supports_streaming: bool` 能力位——`GeminiProvider` 设为
  `False`，因为 Gemini 的 `generateContent` 不原生 SSE，所以
  `stream=true` 命中 gemini-* 模型直接返回 400。Pre-consume 在流开始
  前扣，stream 正常结束用最后一个 `data:` chunk 的 usage 调 `settle`
  退差额；中途 `BaseException`（含 `GeneratorExit` 客户端断连）走
  `refund` 全额退还。
- **流式响应在 Starlette 下有"已发头不能再改"的限制**：
  `StreamingResponse` 在第一个 byte 写出之前就发完 HTTP 头（`status:
  200`），之后再在生成器里 `raise HTTPException` 改不了客户端看到的
  状态码。结果是：上游返回 4xx 且 body 为空时，**客户端拿到 200
  + 空 body**，但 pre-consume 已经在 `except BaseException` 里全额
  退还（不扣钱）。`tests/test_s_full_smoke.py::test_streaming_429_
  refunds_estimate` 就是锁住"不扣钱 + body 为空"这个不变量。要让
  客户端真正看到 4xx，要么在选 `StreamingResponse` 之前先做一次
  非流式探测（破坏流式语义），要么在第一个 chunk 写出前探测
  upstream status（也就是现在这条路，但只能改 body 改不了 status）。
  教程选后者，简单可读；生产里再权衡。
- **admin 操作没有审计**：直接改 channel 池，没有记录是谁改的。
- **错误响应没有结构化**：上游返回 5xx 时，路由直接 `raise HTTPException(502, r.text)`，没有 `{"error": {...}}` 结构。
- **`require_api_key` 不查 token 黑名单**：s09 在自己的 `_current_user`
  里加了 token 黑名单（`/auth/logout`），但 s_full 的
  `middleware/auth.py` 走的是 JWT decode-only 路径，不查同一个黑名单。
  这是有意为之：本教程把"撤销 token"作为 s09 这一章单独讲透；跨章
  共享一份黑名单是合并章节的工作。把 s_full 当独立应用跑时，要撤销
  token 直接换 `JWT_SECRET` 让所有未过期 token 集体失效。

### 没做的事（YAGNI）

- 没有 `Dockerfile` / `docker-compose`：s15 已经演示过，`s_full` 是同一份代码的目录重整，不是部署示例。
- 没有 Grafana dashboard：s16 演示过。
- 没有 Redis / MySQL：用 SQLite + threading.Lock 替代，对应教程的"in-memory 教学"风格。
- 没有 pytest fixtures 隔离 `app.state`：每个测试自己 `reset_db()` / `reset_channels()` / `top_up(...)`。
- 没有 lifespan context manager：仍用 `@app.on_event("startup"/"shutdown")`，与 s11/s16 保持一致（虽然 FastAPI 0.110+ 推荐用 lifespan，但教学一致性优先）。

### 与 chapter 链的对应关系

| 功能                    | chapter(s)       | s_full 文件                         |
|-------------------------|------------------|-------------------------------------|
| 多 provider 转发        | s04              | `adapters/{openai,claude,gemini}.py`|
| Bearer JWT 鉴权         | s05, s09         | `middleware/auth.py` + `routes/auth.py` |
| Token 计数              | s06              | `services/billing._count_*`         |
| 预扣 + 结算             | s07              | `services/quota.py` + `services/billing.pre_consume/settle` |
| 限流                    | s08              | `services/rate_limit.py`            |
| 用户系统                | s09              | `models/user.py` + `routes/auth.py` |
| Channel 管理            | s10              | `models/channel.py` + `routes/admin.py` |
| Call logs               | s11              | `models/log.py` + `routes/admin.py` |
| Prometheus + trace_id   | s16              | `middleware/trace.py` + `/metrics`  |
