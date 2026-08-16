# s16: 可观测性——Prometheus 指标 + 结构化日志 + trace_id — trace_id 串请求,Prometheus 拉指标

> Previous: [s15](../s15_docker_deployment/) · Next: [s_full](../s_full/)

> *"trace_id 把请求串起来"* —— trace 先有，指标后跟。

> **Layer**：L5 运维与可观测

## 本章要做什么

到 s15,我们只能回答两类问题:"服务有没有起来"(看 `/healthz`)、"请求成功没有"(看 HTTP 状态码)。如果用户报"今天 chat 很慢",你需要每分钟请求数、错误率、P50/P99 延迟、按模型分桶的能力、把入口日志 + 出口日志 + 上游调用日志串起来的 `trace_id`——这些都没有。报障只能问"大概几点打的"。

要解决这个,引入三件套:Prometheus `/metrics` 暴露计数器和直方图、`structlog` 把日志重写成 JSON 一行一条、`x-trace-id` 在中间件读 / 回写并塞进每条日志。学完一次请求的入口 + 出口 + 上游三段日志能用 `trace_id` 串起来,Prometheus 拉指标给你看错误率 / 延迟 / 按模型分桶。本章把这套可观测性最小骨架写出来:

1. **挂一个 `TraceAndMetricsMiddleware` —— 为什么用中间件而不在 handler 里调**: `@app.middleware("http")` 装在 s16 这层 app 上,包裹下面 s15 → s14 → ... 整条挂载链。`dispatch` 流程: `trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex` → 写到 `request.state.trace_id` → `start = perf_counter()` → `await call_next(request)` → `elapsed = perf_counter() - start` → 若 chat 路径就 `REQUESTS.labels(model, status).inc()` + `LATENCY.labels(model).observe(elapsed)` → `log.info("request", trace_id=..., ...)` → 响应头写 `x-trace-id`。**为什么用中间件**: 一处定义全局生效,装饰器要给每个 handler 加 `@track_metrics` 易漏;**为什么只匹配 `/v1/chat/completions` 打指标**: `/healthz`、`/metrics` 也走中间件,但只有 chat 路径打 label——避免 `/healthz`、`/metrics` 把 Prometheus 基数撑爆。

2. **Prometheus `Counter` + `Histogram` 在进程内 —— 为什么不是 OTLP / CollectorRegistry**: `metrics.py` 里 `REQUESTS = Counter("learn_new_api_requests_total", ..., ["model", "status"])`,`LATENCY = Histogram("learn_new_api_request_latency_seconds", ..., ["model"])`。`/metrics` 路由用 `generate_latest()` 暴露成 `text/plain; version=1.0.0; charset=utf-8`,Prometheus 抓取。**为什么是进程内 Counter 而不直接 OTLP 上报**: 教学范围内 YAGNI——Prometheus 拉模式是工业标准,scrape 一次 15s、延迟可接受;OTLP 要起 collector,工作量 +1 天;**为什么不分组 CollectorRegistry**: 教程里所有指标在一起,生产需要按子系统分组(才能禁用单个子系统);**为什么 `model` 是 label 不是 metric**: 用户关心"gpt-4 慢还是 claude 慢",`model` 是维度不是值——但高基数会爆 Prometheus,生产里需要采样。

3. **`structlog` JSON 行日志 —— 为什么不是 `logging.info`**: `configure_logging()` 把 `logging` 输出经 `structlog.processors.JSONRenderer()` 重写成 JSON。**为什么是 JSON 不是普通字符串**: `docker logs | jq` 能直接 `jq 'select(.trace_id=="abc-123")'` 过滤;Loki / Vector / Fluent Bit 天然吃 JSON;普通字符串要写正则才能结构化。**为什么不直接用 `print(json.dumps(...))`**: `structlog` 沿用 `logging` 体系,跟 uvicorn / FastAPI 自带日志同条管道,handler 用 `logging` 也会被 JSONRenderer 包成 JSON;`print` 跟 logging 走两条路,混用会让 Loki 看到两种格式混杂。

4. **`x-trace-id` 透传 —— 为什么是 header 不是 contextvar**: 读 `request.headers.get("x-trace-id")`、没有就 `uuid.uuid4().hex` 生成;`request.state.trace_id = trace_id` 让下游 handler 拿得到;响应头写 `x-trace-id` 让客户端能看到、能把同一个 id 带到下一条请求;每条 `log.info("request", trace_id=..., ...)` 都带它。**为什么是 header 不是 contextvar**: 跨服务透传靠 header(nginx / envoy 不认 contextvar),客户端传的 `x-trace-id` 跟服务端生成的 id 同一种格式;**为什么是 `uuid4().hex` 不是雪花算法**: 教学范围内不需要时序、32 字符 hex 够识别一次请求;**为什么中间件读 body 解 model**: 跟 s11 同一手法——`await request.body()` 读出原始 bytes、`json.loads` 解出 `model` 写到 `request.state.model`;Starlette 在 `BaseHTTPMiddleware` 内部把 bytes 缓存到 `request._body`,下游 handler 重读 body 拿到的是同一份 bytes。

成品: `curl localhost:8016/metrics` 看 Prometheus 文本(每行样本带 model label);发起一次 chat,再看 `/metrics` 出现 `model="gpt-4o-mini"` 样本;`docker logs` 看每条请求一行 JSON 包含 `trace_id / path / status / elapsed`。后续 s_full 把指标接到告警规则,trace_id 接到 Loki / Tempo 持久化。

## 上一章复盘

s15 部署稳了,但用户报障只能问"大概几点打的"。

## 在整体中的位置

可观测性的"出口"——trace-id 贯穿所有 16 章,从此一处故障可端到端追踪。

## 问题

到 s15 为止,我们只能回答两类问题:"服务有没有起来"(看 `/healthz`)、"请求成功没有"(看 HTTP 状态码)。如果用户报告"今天 chat 很慢",我们需要:

- 每分钟请求数、错误率、P50/P99 延迟——现在没有指标。
- 按模型(`gpt-4` / `claude-3-5-sonnet` / ...)分桶的能力——现在只能去翻日志。
- 把一次请求的入口日志、出口日志、上游调用日志串起来——现在没有 `trace_id`。

`/healthz` 是深检查(Docker `HEALTHCHECK` 友好),但**指标**(Prometheus 抓取)和**日志**(grep / 聚合到 Loki)是两类独立的可观测性支柱。本章补齐这两类。

## 方案

引入三个最小但够用的机制:

1. **Prometheus 指标**:`prometheus_client` 在进程内维护计数器与直方图,`/metrics` 端点以 `text/plain; version=1.0.0; charset=utf-8` 暴露。
2. **结构化日志**:`structlog` 把 `logging` 输出重写成 JSON 一行一条,方便 `jq` / Loki 解析。
3. **`trace_id` 透传**:`Trace ID`(贯穿一次请求的唯一 ID,用于跨服务串日志)—— 一个 `BaseHTTPMiddleware` 读 `x-trace-id` 请求头(没有就生成),回写到响应头,并写进每条日志。

## 工作原理

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

## 取舍

| 取舍 | 选择 | 原因 |
|---|---|---|
| 分布式追踪导出(OpenTelemetry / OTLP) | **不做** | YAGNI。教程只演示"传 trace_id",不演示"把它送到 Tempo / Jaeger"。要加就是 `opentelemetry-instrumentation-fastapi` + OTLP exporter,工作量 +1 天。 |
| 日志聚合(Loki / ELK 客户端) | **不做** | 同上。结构化 JSON 已经够 `docker logs \| jq` 用了,真正接入 Loki 是部署侧的事。 |
| 告警规则(Prometheus alerting rules) | **不做** | 告警是 SRE 域,不是代码域。 |
| `model` 标签 | **从 JSON body 读** | 中间件用 `await request.body()` 把 chat body 读出来、`json.loads` 解出 `model` 写到 `request.state.model`,**依赖 Starlette 在 `BaseHTTPMiddleware` 内部把 bytes 缓存到 `request._body` 的事实**——下游 handler 重读 body 拿到的是同一份 bytes。这比 s11 显式安装 `receive()` 更简洁,因为 Starlette 已经做了重放。label 真正携带模型名而不是 "unknown"。生产里 `model` label 高基数会爆 Prometheus,需要采样。 |
| `/v1/chat/completions` 路径匹配 | **只匹配 `/v1/chat/completions`** | chat 端点的真实入口是 `/v1/chat/completions`(挂载链里其它层不再独立注册此路径)。中间件只在这条路径打点,避免 `/healthz`、`/metrics` 把基数撑爆。 |
| `/metrics` 与 mount 顺序 | **`/metrics` 在 mount 之前注册** | Starlette 路由顺序坑。 |
| 中间件 vs 装饰器 | **中间件** | 一处定义全局生效;装饰器需要给每个 handler 加 `@track_metrics`,易漏。 |
