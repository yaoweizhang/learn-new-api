# s16:可观测性——Prometheus 指标 + 结构化日志 + trace_id — x-trace-id 串起来,Prometheus 拉请求 / 延迟 / 错误

> Previous: [s15](../s15_docker_deployment/) · Next: [s_full](../s_full/)

> *"trace_id 把请求串起来"* —— trace 先有，指标后跟。

> **Layer**：L5 运维与可观测

## 本章要做什么

引入三件套:Prometheus(`/metrics`)、`structlog` JSON 行日志、`x-trace-id` 透传。中间件在 chat 端点读 body 解 model、写指标、回写 trace 头。学完一处故障可端到端追踪。

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

## 测试

```bash
python -m pytest tests/test_s16_observability.py -v
```

三个用例:

- `test_metrics_endpoint_exposes_counters` —— `/metrics` 返回 200 且文本含 `learn_new_api_requests_total`(counter 名字)。
- `test_trace_id_propagates_to_response` —— 客户端发的 `x-trace-id` 头出现在响应头里。
- `test_chat_request_increments_counter` —— 走完一次 chat 调用后,`/metrics` 文本里出现 `model="gpt-4o-mini"` 的样本行(证明中间件从 JSON body 读到 model 并打了 label)。

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
| `model` 标签 | **从 JSON body 读** | 中间件用 `await request.body()` 把 chat body 读出来、`json.loads` 解出 `model` 写到 `request.state.model`,再用 `receive()` 把同一份 bytes 喂回给下游(Starlette 标准做法)。label 真正携带模型名而不是 "unknown"。生产里 `model` label 高基数会爆 Prometheus,需要采样。 |
| `/v1/chat/completions` 路径匹配 | **只匹配 `/v1/chat/completions`** | chat 端点的真实入口是 `/v1/chat/completions`(挂载链里其它层不再独立注册此路径)。中间件只在这条路径打点,避免 `/healthz`、`/metrics` 把基数撑爆。 |
| `/metrics` 与 mount 顺序 | **`/metrics` 在 mount 之前注册** | Starlette 路由顺序坑。 |
| 中间件 vs 装饰器 | **中间件** | 一处定义全局生效;装饰器需要给每个 handler 加 `@track_metrics`,易漏。 |
