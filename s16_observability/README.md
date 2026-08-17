# s16: 可观测性——Prometheus 指标 + 结构化日志 + trace_id — trace_id 串请求,Prometheus 拉指标

> Previous: [s15](../s15_docker_deployment/) · Next: [s_full](../s_full/)

> *"trace_id 把请求串起来"* —— trace 先有，指标后跟。

> **Layer**：L5 运维与可观测

## 问题

到 s15 为止,我们只能回答两类问题:"服务有没有起来"(看 `/healthz`)、"请求成功没有"(看 HTTP 状态码)。如果用户报告"今天 chat 很慢",我们需要:

- 每分钟请求数、错误率、P50/P99 延迟——现在没有指标。
- 按模型(`gpt-4` / `claude-3-5-sonnet` / ...)分桶的能力——现在只能去翻日志。
- 把一次请求的入口日志、出口日志、上游调用日志串起来——现在没有 `trace_id`。

`/healthz` 是深检查(Docker `HEALTHCHECK` 友好),但**指标**(Prometheus 抓取)和**日志**(grep / 聚合到 Loki)是两类独立的可观测性支柱。本章补齐这两类。

## 本章要做什么

现在场景是:到 s15 为止,我们只能回答两类问题:"服务有没有起来"(看 `/healthz`)、"请求成功没有"(看 HTTP 状态码)。如果用户报告"今天 chat 很慢",我们需要:每分钟请求数 / 错误率 / P50/P99 时延;按 model 分桶;把一次请求的入口 + 出口 + 上游日志串起来。要解决这个——**我们引入三件套**:**Prometheus**(**Prometheus / Prom 指标**(一种"拉模式"的指标系统:进程内维护 `Counter` + `Histogram`,通过 `/metrics` 路由把样本暴露成文本,Prometheus 服务每 15s 来拉一次;指标可带 label 分桶)、**结构化日志**(**structlog JSON 日志**(`structlog` 把 `logging` 输出重写成 JSON 一行一条,`docker logs | jq` 直接过滤;每条都带 trace_id))、`x-trace-id` 在中间件读 / 回写并塞进每条日志——**trace_id**(**trace_id / 追踪 ID**(贯穿一次请求的唯一 ID,通常 `uuid4().hex`;从入口读,没有就生成,回写响应头,塞进每条日志,跨服务透传))。学完一次请求的入口 + 出口 + 上游三段日志能用 `trace_id` 串起来,Prometheus 拉指标给你看错误率 / 延迟 / 按模型分桶。本章把这套可观测性最小骨架写出来:

1. **挂一个 `TraceAndMetricsMiddleware` —— 为什么用中间件而不在 handler 里调**: `@app.middleware("http")` 装在 s16 这层 app 上,包裹下面 s15 → s14 → ... 整条挂载链。`dispatch` 流程: `trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex` → 写到 `request.state.trace_id` → `start = perf_counter()` → `await call_next(request)` → `elapsed = perf_counter() - start` → 若 chat 路径就 `REQUESTS.labels(model, status).inc()` + `LATENCY.labels(model).observe(elapsed)` → `log.info("request", trace_id=..., ...)` → 响应头写 `x-trace-id`。**为什么用中间件**: 一处定义全局生效,装饰器要给每个 handler 加 `@track_metrics` 易漏;**为什么只匹配 `/v1/chat/completions` 打指标**: `/healthz`、`/metrics` 也走中间件,但只有 chat 路径打 label——避免 `/healthz`、`/metrics` 把 Prometheus 基数撑爆。

2. **Prometheus `Counter` + `Histogram` 在进程内 —— 为什么不是 OTLP / CollectorRegistry**: `metrics.py` 里 `REQUESTS = Counter("learn_new_api_requests_total", ..., ["model", "status"])`,`LATENCY = Histogram("learn_new_api_request_latency_seconds", ..., ["model"])`。`/metrics` 路由用 `generate_latest()` 暴露成 `text/plain; version=1.0.0; charset=utf-8`,Prometheus 抓取。**为什么是进程内 Counter 而不直接 OTLP 上报**: 教学范围内 YAGNI——Prometheus 拉模式是工业标准,scrape 一次 15s、延迟可接受;OTLP 要起 collector,工作量 +1 天;**为什么不分组 CollectorRegistry**: 教程里所有指标在一起,生产需要按子系统分组(才能禁用单个子系统);**为什么 `model` 是 label 不是 metric**: 用户关心"gpt-4 慢还是 claude 慢",`model` 是维度不是值——但高基数会爆 Prometheus,生产里需要采样。

3. **`structlog` JSON 行日志 —— 为什么不是 `logging.info`**: `configure_logging()` 把 `logging` 输出经 `structlog.processors.JSONRenderer()` 重写成 JSON。**为什么是 JSON 不是普通字符串**: `docker logs | jq` 能直接 `jq 'select(.trace_id=="abc-123")'` 过滤;Loki / Vector / Fluent Bit 天然吃 JSON;普通字符串要写正则才能结构化。**为什么不直接用 `print(json.dumps(...))`**: `structlog` 沿用 `logging` 体系,跟 uvicorn / FastAPI 自带日志同条管道,handler 用 `logging` 也会被 JSONRenderer 包成 JSON;`print` 跟 logging 走两条路,混用会让 Loki 看到两种格式混杂。

4. **`x-trace-id` 透传 —— 为什么是 header 不是 contextvar**: 读 `request.headers.get("x-trace-id")`、没有就 `uuid.uuid4().hex` 生成;`request.state.trace_id = trace_id` 让下游 handler 拿得到;响应头写 `x-trace-id` 让客户端能看到、能把同一个 id 带到下一条请求;每条 `log.info("request", trace_id=..., ...)` 都带它。**为什么是 header 不是 contextvar**: 跨服务透传靠 header(nginx / envoy 不认 contextvar),客户端传的 `x-trace-id` 跟服务端生成的 id 同一种格式;**为什么是 `uuid4().hex` 不是雪花算法**: 教学范围内不需要时序、32 字符 hex 够识别一次请求;**为什么中间件读 body 解 model**: 跟 s11 同一手法——`await request.body()` 读出原始 bytes、`json.loads` 解出 `model` 写到 `request.state.model`;Starlette 在 `BaseHTTPMiddleware` 内部把 bytes 缓存到 `request._body`,下游 handler 重读 body 拿到的是同一份 bytes。

成品: `curl localhost:8016/metrics` 看 Prometheus 文本(每行样本带 model label);发起一次 chat,再看 `/metrics` 出现 `model="gpt-4o-mini"` 样本;`docker logs` 看每条请求一行 JSON 包含 `trace_id / path / status / elapsed`。后续 s_full 把指标接到告警规则,trace_id 接到 Loki / Tempo 持久化。

## 方案

现在的场景是:`## 问题` 提了三件事——每分钟请求数 / 错误率 / P99 时延没法看 (痛点 #1)、按 model 分桶没法做 (痛点 #2)、一次请求的入口 + 出口 + 上游日志没法串 (痛点 #3)——这三件事**没法靠"用户报障时肉眼看日志"或"运营商爬日志写脚本"能解决**,必须有一个 metrics 端点供 Prometheus 定期拉 + structlog JSON 一行一条便于 Loki 聚合 + trace_id 跨调用串日志。

**要解决这个——我们在网关里引入三个最小但够用的机制**:

1. **Prometheus 指标**:**Prometheus**(开源监控系统,以 `text/plain; version=1.0.0; charset=utf-8` 协议周期性"拉"暴露在 `/metrics` 端点的指标样本,内置 Counter / Histogram 等指标类型——本章首次提到这个术语,这里给出定义)——`prometheus_client` 在进程内维护计数器与直方图,`/metrics` 端点暴露。
2. **结构化日志**:**structlog**(结构化日志库,把 `logging` 输出重写成 JSON 一行一条,方便 `jq` / Loki / Vector / Fluent Bit 直接吃 JSON 而不必写正则——本章首次提到这个术语,这里给出定义)——`structlog` 把 `logging` 输出重写成 JSON 一行一条,方便 `jq` / Loki 解析。
3. **`trace_id` 透传**:**trace_id**(**Trace ID** —— 贯穿一次请求的唯一 ID,经 HTTP header 跨服务透传,运维可凭此在 `docker logs | jq` 里一次 select 出整条链路的所有日志条目——本章首次提到这个术语,这里给出定义)—— 一个 `BaseHTTPMiddleware` 读 `x-trace-id` 请求头(没有就生成),回写到响应头,并写进每条日志。

下面这幅图把上面三件痛各放到四个角色里:

- **`Client` (调用方)** —— 在装上 trace + metrics 中间件之前,这是发完请求就忘、报障只能报"大概几点打的"的角色;装上之后,这事被中间件隔走——客户端可以自带 `x-trace-id`(后续请求),服务端无则自动生成;指标在内部累计,无需客户端参与。
- **`Relay` (本章要写的 `TraceAndMetricsMiddleware`)** —— 把痛点 #1 #2 #3 的解决动作集中放在这里:`@app.middleware("http")` 装在自己 app 上、包裹挂载链;`dispatch` 流程:`trace_id = request.headers.get(...) or uuid.uuid4().hex` 写 `request.state.trace_id` → `start = perf_counter()` → `await call_next(request)` → `elapsed = ...` → 若 chat 路径 `REQUESTS.labels(model, status).inc()` + `LATENCY.labels(model).observe(elapsed)` → `log.info("request", trace_id=...)` → 响应头写 `x-trace-id`。中间件一处定义全局生效。
- **`MetricsStore` (进程内 Counter + Histogram 双指标)** —— 本章引入的两个 Prometheus 指标:`REQUESTS = Counter("learn_new_api_requests_total", ..., ["model", "status"])` 累加请求数 / 状态分布;`LATENCY = Histogram("learn_new_api_request_latency_seconds", ..., ["model"])` 累积时延分布。两个指标都是进程内(`prometheus_client.DefaultRegisterer`),由 `/metrics` 路由 `generate_latest()` 拉。Client 不直接触碰指标,Prom 拉模式对上 Service 透明。
- **`Logs` (structlog JSON 一行一条)** —— 日志管线。`configure_logging()` 把 `logging` 输出经 `structlog.processors.JSONRenderer()` 重写成 JSON 一行一条 (`docker logs | jq` 可直接 `select(.trace_id=="...")` 过滤);每条日志都带 `trace_id`。Client 不直接读 stdout,但运营 / Loki 能从 JSON 串起来。
- **`Prom` (Prometheus server)** —— 痛点 #1 #2 的外部拉取方。每 15s 拉一次 `/metrics`,得到 `model="gpt-4o-mini"` / `status="200"` 这种带 label 的样本。拉模式 vs 推模式选择拉模式是因为工业标准,Prom 侧配置简单。

1. **Prometheus 指标**:`prometheus_client` 在进程内维护计数器与直方图,`/metrics` 端点以 `text/plain; version=1.0.0; charset=utf-8` 暴露。
2. **结构化日志**:`structlog` 把 `logging` 输出重写成 JSON 一行一条,方便 `jq` / Loki 解析。
3. **`trace_id` 透传**:`Trace ID`(贯穿一次请求的唯一 ID,用于跨服务串日志)—— 一个 `BaseHTTPMiddleware` 读 `x-trace-id` 请求头(没有就生成),回写到响应头,并写进每条日志。

## 工作原理

**原理**: 一个 chat 请求从客户端进来,整个可观测性流程是: `TraceAndMetricsMiddleware` (Starlette 的"包整个 app 的可注入钩子") 在 `dispatch` 入口读 `request.headers.get("x-trace-id")`,没有就用 `uuid.uuid4().hex` 生成一个,写到 `request.state.trace_id` → `start = perf_counter()` 计时 → `await call_next(request)` 放行到挂载链(s15/s14/.../upstream) → `elapsed = perf_counter() - start` → 若 chat 路径,`REQUESTS.labels(model, status).inc()` + `LATENCY.labels(model).observe(elapsed)` 进 Prometheus 指标 → `log.info("request", trace_id=..., ...)` 经 structlog 出一行 JSON 日志 → 响应头 `x-trace-id` 回写给客户端。`/metrics` 路由在 mount 之前注册,Prometheus 服务 15s 拉一次得到样本。整章所有部件都为"trace_id 串一次请求 + 指标供 Prom 拉 + 日志一行 JSON"这条主线服务。

**1. 一个 `TraceAndMetricsMiddleware` (`code.py`, `@app.middleware("http")` 装在自己 app 上包裹挂载链)** —— `dispatch` 完成"读 / 生成 trace_id → 计时 → call_next → 打指标 → 写日志 → 回写 trace_id 头"这一整条流水线。**为什么用中间件而不用装饰器**: 一处定义全局生效,装饰器要给每个 handler 加易漏。**为什么只匹配 `/v1/chat/completions` 打指标**: 避免 `/healthz` `/metrics` 把 Prometheus 基数 (基数——指标 label 维度组合的可能值数量,过大时存储和查询成本爆炸) 撑爆。

**2. 一个 Prometheus `Counter` + `Histogram` 双指标 (`metrics.py`, 进程内 + `prometheus_client` 默认注册器)** —— `REQUESTS = Counter("learn_new_api_requests_total", ..., ["model", "status"])` 累计请求数 + 状态分布;`LATENCY = Histogram("learn_new_api_request_latency_seconds", ..., ["model"])` 累积时延分布。`/metrics` 路由用 `generate_latest()` 暴露 `text/plain; version=1.0.0`。**为什么 `model` 是 label 不是独立 metric**: 用户关心"gpt-4 慢还是 claude 慢",`model` 是分桶维度不是值;但高基数会爆 Prometheus,生产需要采样 + 配置上限。

**3. 一个 `structlog` JSON 行日志管线 (`configure_logging()`, 重写 logging 输出)** —— `logging` 经 `structlog.processors.JSONRenderer()` (结构化日志的 JSON 序列化器) 一行 JSON;每条 `log.info("request", trace_id=..., ...)` 都带 trace_id。**为什么是 JSON 不是普通字符串**: `docker logs | jq 'select(.trace_id=="abc")'` 直接过滤;Loki / Vector / Fluent Bit 天然吃 JSON;普通字符串要写正则才能结构化。**为什么不直接 `print(json.dumps(...))`**: structlog 沿用 logging 体系,uvicorn / FastAPI 自带日志都在同条管道,走 logging 全变 JSON;`print` 跟 logging 走两条路会混。

**4. 一个 `x-trace-id` 透传 (HTTP header + uuid4 hex)** —— 读 `request.headers.get("x-trace-id")`、没有就 `uuid.uuid4().hex` 生成;写 `request.state.trace_id` 供下游 handler 共享;响应头 `x-trace-id` 回写给客户端(下一次请求客户端可自带);每条日志都带它。**为什么是 header 不是 contextvar** (Python 的上下文本地变量——同一请求内任意代码都能拿到,但跨 HTTP 边界就丢): 跨服务透传靠 header(nginx / envoy 不认 contextvar),客户端传的 trace_id 跟服务端生成的同一种格式。

### 时序图

下面这张 ASCII 时序图画一次请求穿过中间件——图里有 `client`、`TraceAndMetricsMiddleware`、下游挂载链三个角色,纵向时间、横向消息流向,中间那一块就是本章要写的中间件——读 trace_id、计时、写日志、回写 trace_id 头:

```
client ──GET /healthz  x-trace-id: abc-123──▶
            │
            ▼
   TraceAndMetricsMiddleware
   ├─ trace_id = "abc-123"
   ├─ t0 = perf_counter()
   ├─ call_next(...)           ──▶  s15 /healthz → s14 → ...
   ├─ elapsed = t1 - t0
   ├─ log.info("request", trace_id=..., path=..., elapsed=...)
   └─ response.headers["x-trace-id"] = "abc-123"
            │
            ▼
client ◀── 200 + x-trace-id: abc-123 + JSON 日志一行──
```

指标在 `metrics.py` 里集中定义,中间件只在匹配 chat 路径时打点(避免 `/healthz`、`/metrics` 把基数撑爆):

```python
if request.url.path == "/v1/chat/completions":
    model = getattr(request.state, "model", None) or "unknown"
    REQUESTS.labels(model=model, status=response.status_code).inc()
    LATENCY.labels(model=model).observe(elapsed)
```

`/metrics` 路由必须在 `app.mount("/", s15_app)` 之前注册——Starlette 按注册顺序遍历路由,根 mount 会吞掉 `/metrics` 产生 404。中间件注册顺序无关紧要,因为它包裹所有路由。

## 运行

```bash
cd learn-new-api
python -m s16_observability.code     # 或:uvicorn s16_observability.code:app
# 端口:8016(PORT 环境变量可改)
```

验证指标端点格式: 打这条 `curl -s localhost:8016/metrics`——首行 `# HELP learn_new_api_requests_total ...` + `# TYPE ... counter` 是 Prometheus scrape 接受的格式,说明 prometheus_client 注册器 + `/metrics` 路由挡 mount 都到位:

抓取指标(从 Prometheus / 浏览器 / curl):

```bash
curl -s localhost:8016/metrics | head -20
# HELP learn_new_api_requests_total Total /v1/chat/completions requests
# TYPE learn_new_api_requests_total counter
# (HELP/TYPE 这两行在进程启动时就会出现,所以现有测试不发起 chat 也能过;
# 实际带 label 的样本行只有在有 chat 请求触发 inc() 之后才会出现 —— 见下方取舍)
```

发个 chat 请求(让计数器产生数据):

```bash
curl -s localhost:8016/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4","messages":[{"role":"user","content":"hi"}]}'
curl -s localhost:8016/metrics | grep learn_new_api_requests_total
```

每条请求会输出一行 JSON 日志到 stdout:

```json
{"trace_id": "5e7e03838c4d470187b5a9868565ddcb", "method": "GET", "path": "/healthz", "status": 200, "elapsed": 0.0006, "event": "request"}
```

Docker 镜像下,这行 JSON 直接进 `docker logs`;K8s 下进 stdout,由 Fluent Bit / Vector 采集。

## → new-api 源码

- `logger/logger.go` —— new-api 的日志门面:zap sugared logger + rotating file + console,统一格式。所有 `logger.*` 调用都会带 `trace_id`(如果上游传了 `X-Request-Id`)。
- `pkg/perf_metrics/metrics.go` + `types.go` + `flush.go` —— new-api 的 Prometheus 注册中心:全局 `prometheus.DefaultRegisterer`,所有自定义指标(请求计数、通道延迟、配额)在这里注册。

我们用 `prometheus_client` 的进程内 `Counter` / `Histogram`,没走 `CollectorRegistry` 分组——教程范围内 YAGNI;new-api 在生产里需要分组(否则不同子系统不能单独禁用)。

## 本章不做什么

- **分布式追踪导出(OpenTelemetry / OTLP,OpenTelemetry Protocol——分布式追踪导出协议,把 trace 送到 Jaeger / Tempo 等后端)** —— YAGNI。教程只演示"传 trace_id",不演示"把它送到 Tempo / Jaeger"。要加就是 `opentelemetry-instrumentation-fastapi` + OTLP exporter,工作量 +1 天。→ s_full 接入 Tempo 时一并做。
- **日志聚合(Loki / ELK 客户端,集中化日志存储与查询系统)** —— 结构化 JSON 已经够 `docker logs \| jq` 用了,真正接入 Loki 是部署侧的事。→ s_full 部署时上 Loki + Vector / Fluent Bit 采集。
- **告警规则(Prometheus alerting rules——Prometheus 侧的阈值告警配置,如"5 分钟内 5xx > 10% 触发告警")** —— 告警是 SRE (Site Reliability Engineering,站点可靠性工程) 域,不是代码域。→ s_full 部署时跟运维一起配。
- **没有按用户 / 按渠道的细粒度指标** —— `REQUESTS.labels(model, status)` 只按 model 区分,没有 `user_id` / `channel_id` 这种二级 label。渠道 / 用户维度切分留给业务层做。
- **没有失败率 / 慢请求自动抓取** —— `LATENCY` 提供原始直方图分桶,但不主动抓"近 5 分钟慢请求 topN"。这种 smart sampling 是商业 APM (Application Performance Management,应用性能管理) 的活,本章 YAGNI。

## 已知限制

- **`model` label 高基数会爆 Prometheus** —— 生产里 model list 是开放的(用户随时调新模型),label 维度组合可能上万。要么采样(只记录 top 50 model + "other")、要么限流(单 model 限 X 维度)。本章不做。生产里 +1 天的工程量。→ s_full 接配额系统时一并加 limit。
- **`prometheus_client` 默认注册器单进程限制** —— 多 worker (例如 `--workers 4`) 部署下,每个 worker 各持一份计数器,Prometheus 拉一次只能看到当前被 routed 到的那个 worker 的样本。生产里要起 sidecar Pushgateway 或在 LB 层聚合。YAGNI:本章默认单 worker 够用。
- **structlog 配置是全局一次,后续注入新字段麻烦** —— `configure_logging()` 配好后,后续代码想加字段得改 `bind()` 上下文,不是 sticky 的。要 sticky 用 `structlog.contextvars.bind_contextvars`。YAGNI:本章每条日志现场传 `trace_id=...` 够简单。
- **`elapsed` 用 `perf_counter()` 不是 `time.monotonic()`** —— `perf_counter` 含 sleep 时间,不适合长任务;短请求(< 1s)纳秒精度足够。本章 elapsed 都是 < 1s 的 HTTP 端点。生产长任务用 `time.monotonic_ns()`。
- **HELP/TYPE 在启动时就出现,带 label 的样本要触发后才出现** —— 这是 Prometheus 拉模式的特性:空指标也有元数据行,实际样本在 inc() / observe() 之后才出现。本章测试断言"启动后 HELP/TYPE 存在"通过,但实际样本必须触发一次 chat 请求才会出现 ——见下方设计选择的"`/metrics` 端点先注册"。

## 设计选择

- **中间件 vs 装饰器** —— 中间件。一处定义全局生效;装饰器要给每个 handler 加 `@track_metrics` 易漏。
- **只匹配 `/v1/chat/completions` 打指标** —— chat 端点的真实入口是 `/v1/chat/completions`(挂载链里其它层不再独立注册此路径)。中间件只在这条路径打点,避免 `/healthz` `/metrics` 把 Prometheus 基数撑爆。
- **`/metrics` 在 mount 之前注册** —— Starlette 按注册顺序匹配路由,本地 `/metrics` 命中,根本不会落到挂载链;若 mount 在前,会被 mount 吞掉变 404。这是 `s04_multi_provider` / `s05_api_key_auth` 都在踩的同一个 Starlette 坑。
- **`model` 从 JSON body 读,不从 URL 查** —— 中间件用 `await request.body()` 读 bytes、`json.loads` 解出 `model` 写到 `request.state.model`,依赖 Starlette 在 `BaseHTTPMiddleware` 内部把 bytes 缓存到 `request._body`(请求体的字节缓存,下游重读拿到同一份)。这比 s11 显式安装 `receive()` 更简洁,Starlette 已经做了 body 重放。label 真正携带模型名而不是 `"unknown"`。反方:`model` 高基数要采样,要 +1 天工程量,留给 s_full。
- **`uuid4().hex` 而非雪花算法 trace_id** —— 教学范围内不需要时序;32 字符 hex 唯一性够识别一次请求,雪花算法需要 worker_id + sequence 增 5-10 行书架代码。生产里真分布式追踪都用 W3C Trace Context 的 16-byte traceparent,不在这两个之间选。
