# s_full——生产形态整合版 — 教学挂载 → 生产装配,include_router 替代 mount

> Previous: [s16](../s16_observability/) · Next: —

> *"16 章合一就是生产形态"* —— 教学挂 mount，生产 include_router。

> **Layer**：LX 整合形态

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

解法就是把 16 章的功能**重新装配**成一个独立的 FastAPI app：`include_router` 替代 `mount`，目录拆成 `routes / services / models / adapters / middleware` 五层。下面就讲怎么做。

---

## 本章要做什么

现在场景是:经过 s01-s16 的逐步拆解,我们拥有 16 个独立的 FastAPI app、通过 `app.mount("/...", sNN_app)` 串成一条链(s16 → s15 → ... → s02)、每个 chapter 内部都有自己的 routes / services / models,但都塞在 `sNN_topic/` 一个目录下。这适合教学(每个 chapter 独立可读、独立可测),但**不像真实项目**——读者去看 `new-api` 仓库,看到的是 `router/ controller/ service/ model/ middleware/ common/` 这种目录。要解决这个——**我们把 16 章的功能重新装配成一个独立的 FastAPI app**:不再挂载 chapter chain,改为在 `s_full/code.py` 入口用 `app.include_router(...)` 把 `routes/auth.py / routes/admin.py / routes/chat.py` 三个本地 router 接进来——**include_router**(**`include_router`**(把 APIRouter 的路由直接注册进当前 app 的路由表,路径保持原样,跟 `app.mount` 把子 app 整体挂到某子路径下完全不同))替代 `mount`;目录按 `routes / services / models / adapters / middleware` 五层重组,对应 `new-api` 的 `Router → Controller → Service → Model → Middleware`。本章就把这个"教学挂载 → 生产装配"的过程做出来:

1. **把 16 章代码复制到 `s_full/` 下的五层子目录 —— 为什么是复制而不是 import**: `routes/auth.py` ← s09 的注册/登录、`routes/chat.py` ← s04+s05+s06+s07+s08 的 chat 转发链路、`services/quota.py` ← s07 的预扣结算、`services/rate_limit.py` ← s08 的 token bucket、`models/user.py` ← s09 的 SQLite+bcrypt、`adapters/{openai,claude,gemini}.py` ← s04 的 Provider ABC、`middleware/trace.py` ← s16 的 `TraceAndMetricsMiddleware`。**为什么不 `from s09_user_system.auth import router` 跨章 import**:(a) tutorial 章节本身要保持自包含可读,跨章 import 会把"s09 跑得动"绑到"s07 的某个内部细节没改";(b) `s_full` 要展示**独立项目**的目录长什么样——独立项目不能 import 教学章节;(c) pytest collection 时跨章 import 容易触发意料之外的初始化副作用(比如 s07 启动时把 `models/user.py` 的 sqlite 文件创建到 s07 自己的 cwd)。

2. **入口用 `include_router` 替代 `app.mount` —— 为什么 production 不挂载 chapter chain**: `s_full/code.py` 写 `app.include_router(auth.router)` / `app.include_router(admin.router)` / `app.include_router(chat.router)`,**不**挂任何 chapter。**为什么不挂**:(a) `app.mount("/...", sNN_app)` 会让 mounted 子 app 的 routes 注册到挂载点,生产里如果把整应用挂到 `/api/v1` 下,内部 16 层的路径都要再前缀化一次——一旦 mount 链某层忘了加前缀,客户端 404 排查要追 16 层;(b) 教学形态用 mount 是为了**章节之间能复用前章的代码**(s02 直接 import s01 的 `app`,这样 s02 的 README 只讲"换协议"那一件事,前面"能转发"那一章就被复用掉了),生产里功能被独立组织后,这种复用不再必要;(c) 路由散落在 16 个 app 里,新读者打开 `s16/code.py` 看到 `app.mount("/", s15_app)` 第一反应是"这是入口吗?再追 15 层才知道"——`include_router` 让 `code.py` 一眼看到全部对外路由。

3. **挂上 `TraceAndMetricsMiddleware` 和日志 flush loop —— 为什么这两个必须随装配一起搬**: `app.add_middleware(TraceAndMetricsMiddleware)` 装在 s16 那层,s_full 直接把同一个中间件挂到自己的 app 上(代码逐字复制,不动逻辑),`prometheus_client.generate_latest()` 通过 `app.get("/metrics")` 暴露;`models/log.py` 的 `flush_loop` 在 `@app.on_event("startup")` 启动、`@app.on_event("shutdown")` 停。**为什么不能只复制 routes 不复制中间件**:(a) `chat.py` 里的 `p: Principal = Depends(require_api_key)` 依赖 `middleware/auth.py` 注入 Principal,auth 必须在 chat 之前 import;(b) `routes/chat.py` 调 `models/log.enqueue_log()` 记调用,`flush_loop` 不启动日志永远不落盘;(c) `TraceAndMetricsMiddleware` 是唯一给 `/metrics` 喂数据的地方,不挂中间件 `/metrics` 就是空响应。

4. **保留教学里的所有 invariant —— 为什么 s_full 不"优化"任何细节**: `_pick(model)` 仍按模型前缀选 provider(`gpt-*` → OpenAI、`claude-*` → Anthropic、`gemini-*` → Google),跟 s04 保持一致;`Principal` 从 `middleware/auth.py` 注入,跟 s08 修正后的 typed-parameter 模式一致;`quota.settle` 双向结算(超量补扣、节约退还)对齐 s07。**为什么不借机"加" channel pool / retry / caching**: 这些是 s13 / s12 / s10 的独立主题,s_full 是"装配"不是"扩展"——一旦在装配视图里偷偷加新功能,读者对照"目录映射"那一节就会发现"文件多了一行注释里没写",装配视图的诚实性就破了。**已知限制**(跟教学一致):没有 channel pool 真实选路、没有 retry/fallback、没有 caching、流式 4xx 客户端拿到 200+空 body(Starlette 头已发)、admin 操作没审计。

成品: `python s_full/code.py`(或 `python -m s_full.code`)拿到单一 FastAPI app,端口默认 8099,`curl localhost:8099/health` 看到 `{"status":"ok"}`,`curl -X POST localhost:8099/auth/signup -d '...'` 注册、`/auth/login` 拿 token、再 `curl -X POST localhost:8099/v1/chat/completions -H 'Authorization: Bearer ...'` 走完整 16 章链路(限流 → 预扣 → 选 provider → 转发 → 结算 → 记日志),`curl localhost:8099/metrics` 看到 Prometheus 指标。`tests/test_s_full_smoke.py` 14 个 smoke 测试覆盖完整端到端流程。**后续**: 这就是教程终点——再往下是部署(s15 已演示)、告警规则、生产级 channel pool,都是工程实践不是教学。

---

## 方案

现在的场景是：`## 问题` 提了三件痛——16 个 chapter 各自是一个独立 FastAPI app、各跑各的端口（痛点 #1）；靠 `app.mount("/", sNN_app)` 串成 s16 → s15 → … → s02 的挂载链（痛点 #2）；每个 chapter 的 routes / services / models 全塞在 `sNN_topic/` 一个扁平目录里，不像真实项目（痛点 #3）——这三件事**没有一件能靠"再多挂一层 mount"解决**——挂载链本身就是病因。

**要解决这个——我们把 s01-s16 的代码**复制**到 `s_full/` 下的清晰子目录里，对外提供**单一** FastAPI app**。`s_full` 拥有自己的 routers，不挂载 chapter chain——`include_router` 替代 `mount`：三次 `app.include_router(...)` 把三个 `APIRouter` 的路由直接注册进同一张路由表，路径就是它声明的那个；`app.mount("/api/v1", sub_app)` 是把另一个 ASGI app 整体挂到子路径下，挂载点会叠加到子 app 的每条路径上——挂载链有 16 层，某一层忘了改前缀，客户端就是一个要追 16 层的 404。

**首次引入**：**include_router**（`include_router` —— FastAPI 把一个 `APIRouter` 的全部路由注册进主 app 路由表的方法，路由声明的路径就是最终对外暴露的路径，不会叠加挂载前缀——本章首次提到这个术语，所以这里多说一句）。它在本章里承担的是"装配视图"比"挂载链"更能把对外入口讲清楚的全部职责。

本章不画角色图（`s_full` 是整合章，没有新角色，只有装配动作），所以下面直接把每个集成层挑战对到 `s_full` 的解法上：

- **挑战 #1：16 个 app、16 个端口** → **单一 8099 端口的 FastAPI app**。`s_full/code.py` 里只有一个 `app = FastAPI(...)`，16 章的功能全部收拢到它下面。部署方只需要暴露一个进程、一个端口、一份配置，而不是 16 份。
- **挑战 #2：mount 链让路径被重前缀化** → **`include_router` 替代 `mount`**。`app.include_router(auth.router)` / `admin.router` / `chat.router` 把三个 `APIRouter` 的路由**直接注册进同一张路由表**，路径就是它声明的那个；而 `app.mount("/api/v1", sub_app)` 是把另一个 ASGI app 整体挂到子路径下，挂载点会叠加到子 app 的每条路径上——挂载链有 16 层，某一层忘了改前缀，客户端就是一个要追 16 层的 404。
- **挑战 #3：路由散落、目录不像真实项目** → **五层子目录 + 一眼可读的 entrypoint**。目录按 `routes / services / models / adapters / middleware` 重组，对齐 `new-api` 的 `Router → Controller → Service → Model → Middleware`；打开 `s_full/code.py` 看到的是全部对外路由的清单（三行 `include_router` + 一行 `add_middleware`），而不是 `app.mount("/", s15_app)` 这种"入口在下一层"的接力。
- **横切能力怎么办** → **middleware stack 随装配一起搬**。`TraceAndMetricsMiddleware`（s16）挂在 app 层而不是某条路由上，所以 trace_id 和 Prometheus 计数对三个 router 一视同仁——这正是"装配视图"比"挂载链"更能表达的东西：横切关注点属于 app，不属于某一章。

下面这张目录映射表是这套装配的落地形态：

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

**原理**: 一个 HTTP 请求打到 8099 端口上唯一那个 FastAPI app, 它的生命周期是: ASGI 服务器先把请求交给 middleware stack (中间件链, 这里只有 `TraceAndMetricsMiddleware`——生成 trace_id + 记 Prometheus 计数) → 路由表按方法和路径挑出对应 handler (路由表的内容是启动时三次 `include_router` 注册进来的) → handler 通过 dependency injection 拿到 `Principal` (鉴权后代表当前调用者的对象) → 依次调 `services/` 层的限流与预扣、`adapters/` 层的协议翻译 → httpx 发出站请求给上游 → 回包翻回 OpenAI 形态 → `services/` 结算配额、`models/log` 入队落盘 → 响应沿 middleware stack 原路吐回客户端。整章所有部件都为这条主线服务, 而且这条主线跟 s01-s16 里逐章写过的**完全是同一份代码**, 只是换了摆放位置。

**1. 一个 application entrypoint (`s_full/code.py`)** —— 三行 `app.include_router(...)` + 一行 `app.add_middleware(...)` + `/health` `/metrics` 两条自带路由, 就是整个应用的对外面貌。读者不需要往下追任何一层就能看全对外路由。

**2. 一个 middleware stack (`app.add_middleware(TraceAndMetricsMiddleware)`)** —— 横切关注点 (cross-cutting concern, 对所有路由一视同仁的能力) 挂在 app 层: trace_id 注入 + Prometheus 指标采集。`/metrics` 的数据唯一来源就是它, 不挂中间件 `/metrics` 就是空响应。

**3. 五层包结构 (`routes / services / models / adapters / middleware`)** —— `routes` 是 APIRouter (路由层)、`services` 是业务逻辑、`models` 是数据层、`adapters` 是厂商协议适配器、`middleware` 是鉴权与观测。依赖方向单向向下: routes → services → models, adapters 只被 routes 调, middleware 谁都不依赖。

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

启动后确认整合版真的起来了没?打这个 curl——能返回 `{"status":"ok"}` 说明 8099 端口上那个唯一的 FastAPI 进程在响应,而且启动阶段三次 `include_router`(auth / admin / chat,16 章的功能全在这三个 router 里)和 `add_middleware` 都跑完了——只要其中任何一个子模块 import 失败,进程根本起不来,这条 curl 会直接连不上:

```sh
curl http://localhost:8099/health
# {"status":"ok"}
```

起好之后可以访问的端点：

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
| `routes/chat.py`         | `router/relay-router.go` → `controller/relay.go` → `service/` (RelayTask etc) | Gin handler → Go controller → service 三层；FastAPI 的 `APIRouter` 兼路由+编排，所以这里只有路由文件 |
| `routes/auth.py`         | `router/api-router.go` 中 `/api/user/register`   | new-api 用 gin；教程用 FastAPI |
| `routes/admin.py`        | `router/api-router.go` 中 `/api/channel/*`        | 同上 |
| `services/quota.py`      | `service/quota.go`                                | new-api 走 Redis Lua 脚本保证原子扣减；教程用 `threading.Lock` + dict 保持可读性 |
| `services/rate_limit.py` | `middleware/rate-limit.go` (Redis token bucket)   | 注意: rate-limit 在 middleware 里、不在 service 里 |
| `services/billing.py`    | `service/billing.go` (PreConsume/Settle/Refund)  | 注意: 没有 `pre_consume_quota.go`,逻辑全在 billing.go |
| `models/user.py`         | `model/user.go` (user + quota 字段合一)           | new-api 没有独立的 `userquota.go` 表,user 表带 quota 字段 |
| `models/channel.py`      | `model/channel.go`                                | 教程 in-memory；new-api 是 DB-backed |
| `models/log.py`          | `model/log.go`                                    | 教程 deque + 100ms flush；new-api 是 MySQL 批量 insert |
| `adapters/*.py`          | `relay/channel/openai/adaptor.go` + `relay/channel/claude/relay-claude.go` + `relay/channel/gemini/relay-gemini.go` | new-api 每个 provider 一个子目录；openai/claude/gemini 各自有自己的 adaptor 实现（**没有 `relay/adaptor/` 目录、没有 `anthropic/` 子目录——Claude 在 `claude/`**）|
| `middleware/auth.py`     | `middleware/auth.go`                              | new-api 走 JWT；教程同样 |
| `middleware/trace.py`    | `middleware/request-id.go` + `pkg/perf_metrics/metrics.go` | new-api 没有独立的 `trace.go`,request-id 由 `request-id.go` 设置;指标由 `pkg/perf_metrics/` 提供（包名 `perfmetrics`）|

### 一个重要区别

new-api 的 **relay** 不只是"按模型前缀选 provider"——它会维护一个 channel pool，每个 channel 是某个上游的实例，按 (priority, weight, healthy) 排序自动 failover。`s_full` 当前**不做 channel pool 的真实选路**——`routes/chat.py` 直接按模型前缀选 provider，与 s04 保持一致（s04 也只挑 provider，不挑 channel）。channel pool 的真正使用参见 s13 (retry + fallback) 和 s14 (admin dashboard)。如果以后要扩展 channel failover，需要在 `routes/chat.py` 里加一个新选择函数（按 priority/weight 在同 provider 的 channel 里挑一条）。

---

## 本章不做什么

- **没有 `Dockerfile` / `docker-compose`** (容器镜像构建文件 + 多容器编排文件)——s15 已经演示过，`s_full` 是同一份代码的目录重整，不是部署示例。→ s15。
- **没有 Grafana dashboard** (把 Prometheus 指标画成折线图的看板)——s16 演示过，`s_full` 只保证 `/metrics` 有数据可拉。→ s16。
- **没有 Redis / MySQL** (跨进程共享状态的外部存储；进程内的 dict 一重启就没了、多副本也各存各的)——用 SQLite + `threading.Lock` 替代，对应教程的"in-memory 教学"风格；单进程够用，要横向扩副本就不够。
- **没有 pytest fixtures 隔离 `app.state`** (fixture：测试之间自动重置进程内状态的固定装置)——每个测试自己 `reset_db()` / `reset_channels()` / `top_up(...)`，显式重置比隐式 fixture 更适合逐章阅读。
- **没有 lifespan context manager** (FastAPI 0.110+ 推荐的启动/关闭钩子写法，用一个 async 上下文管理器取代两个 `on_event` 回调)——仍用 `@app.on_event("startup"/"shutdown")` 启停日志 flush loop，与 s11/s16 保持一致；教学一致性优先于 API 新旧。

## 已知限制

- **没有 channel pool 的真实选路** (channel pool：同一家上游的多个实例组成的池，按 priority / weight / healthy 自动挑一条并在故障时切换)：`_pick(model)` 只按模型前缀选 provider —— s_full 的 `Channel` 模型只保留 `id` + `base_url` + `enabled`,没有 `provider` 字段,`mark_unhealthy` 函数也移除了,因为 s_full 没有 channel pool 选路、所有渠道都按模型前缀直接派发。如果要扩展 channel failover,需要:重新加 `provider` 字段、恢复 `mark_unhealthy`、在 `routes/chat.py` 里加 channel 选路调用。
- **没有 retry / fallback** (重试 + 失败后换一条上游再试，把上游抖动挡在客户端之外)（参见 s13）：单次上游调用失败直接 refund + 502。
- **没有 caching** (prompt cache：相同输入直接命中缓存、不花上游 token)（参见 s12）：同样的 prompt 不会走 prompt cache。
- **streaming 部分支持** (SSE 流式：上游边生成边推字节，客户端逐 token 看到结果)：客户端发 `stream=true` 时走 `StreamingResponse`
  把上游 SSE 字节原样透传（OpenAI / Claude 已通）。每个 `Provider`
  标了 `supports_streaming: bool` 能力位——`GeminiProvider` 设为
  `False`，因为 Gemini 的 `generateContent` 不原生 SSE，所以
  `stream=true` 命中 gemini-* 模型直接返回 400。Pre-consume 在流开始
  前扣，stream 正常结束用最后一个 `data:` chunk 的 usage 调 `settle`
  退差额；中途 `BaseException`（含 `GeneratorExit` 客户端断连）走
  `refund` 全额退还。
- **流式响应在 Starlette 下有"已发头不能再改"的限制** (HTTP 头一旦写出，status code 就定死了，后面再抛异常也改不了客户端看到的状态码)：
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
- **admin 操作没有审计** (审计日志：记录谁在什么时候改了哪条 channel，出事后能回溯)：直接改 channel 池，没有记录是谁改的。
- **错误响应没有结构化** (structured error：`{"error": {...}}` 这种机器可解析的错误体，OpenAI SDK 会去读它)：上游返回 5xx 时，路由直接 `raise HTTPException(502, r.text)`，没有 `{"error": {...}}` 结构。
- **`require_api_key` 不查 token 黑名单** (blacklist：登出后主动作废的 token 名单；不查就意味着已登出的 token 在过期前仍然能用)：s09 在自己的 `_current_user`
  里加了 token 黑名单（`/auth/logout`），但 s_full 的
  `middleware/auth.py` 走的是 JWT decode-only 路径，不查同一个黑名单。
  这是有意为之：本教程把"撤销 token"作为 s09 这一章单独讲透；跨章
  共享一份黑名单是合并章节的工作。把 s_full 当独立应用跑时，要撤销
  token 直接换 `JWT_SECRET` 让所有未过期 token 集体失效。

## 设计选择

- **复制而不是 import** (跨章节 `from sNN_topic.x import y` 的替代方案：把代码逐字搬过来)：s07/s08/s09/s10/s11/s16 的代码被**逐字复制**到 `s_full/` 下的对应位置。**不** `from s07_pre_consume_settle.quota import deduct` 之类的跨章节导入。三个理由：
  1. tutorial 章节本身要保持自包含可读；
  2. `s_full` 要展示"如果这是一个**独立**项目，目录应该长什么样"——重新组织就意味着不引用。
  3. 跨章节 import 在 pytest collection 时容易触发意料之外的初始化副作用。
  代价：同一段逻辑存在两份，改教学章不会自动同步到 `s_full`。
- **routers 而不是 mount** (`include_router`：把 APIRouter 的路由注册进当前 app 的路由表，路径保持原样；`app.mount`：把另一个 ASGI 子 app 整体挂到某个子路径下，挂载点会叠加到子 app 的每条路径上)：`s_full/code.py` 走 `include_router`，**不**挂载任何 chapter chain。chapter chain 是教学形态（s16 -> s15 -> ... -> s02），`s_full` 是生产形态（装配视图：把功能按职责重新摆放，而不是按讲解顺序层层包裹）。
- **`Principal` 定义在 `middleware/auth.py`** (Principal：鉴权通过后代表"当前调用者是谁"的对象，往下传给每个 handler)：在 s05/s08/s09 里 Principal 是 `dataclass(user_id, scopes)`；`s_full` 把 `email` 和 `is_admin` 也加进来，因为 JWT 本身带这俩字段。`user_id` 由 `int` 取代 `str`，对齐 s09 的 SQLite 主键类型。
- **billing.pre_consume 抛 `PermissionError` 而不是 `HTTPException`** (`HTTPException` 是 FastAPI 的 HTTP 错误类型，抛它就等于让 service 层知道自己活在 HTTP 里)：保持 service 层不依赖 HTTP，同一份 service 换个入口（CLI / 定时任务）也能用。路由层 catch 后翻译成 402。

## 与 chapter 链的对应关系

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
