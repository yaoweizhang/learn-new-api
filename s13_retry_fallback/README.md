# s13: 失败即回落——渠道级 fallback(不重试,换下一条) — 失败 → 标 unhealthy → 换下一个

> Previous: [s12](../s12_caching/) · Next: [s14](../s14_admin_dashboard/)

> *"失败即换下一条"* —— 重试浪费，换渠道更便宜。

> **Layer**：L4 路由与韧性

## 本章要做什么

把 chat 端点提到本地,遍历 `list_channels()`,失败立刻 `mark_unhealthy` + 切换下一条,所有渠道都失败才回错误。学完你拿到一条不会被单条抖动拖垮的 chat 路径。

## 上一章复盘

s12 缓存加速,但渠道瞬时 502 就 502。

## 在整体中的位置

通道的"韧性层"——把 s10 的渠道表从"配置"变成"运行时调度"。

## 问题

走到第 12 章,网关把"上游能不能正常响应"这件事赌在一条渠道上:
s08 拿到请求 → s10 选一条渠道 → 调上游 → 把响应吐回客户端。上游
只要抖一下(HTTP 503、502、504、429),客户端直接看到一个错误,
然后自己重试。

问题在于客户端重试是**全栈重试**——重试一次要把整条请求重新走一遍
认证、计费、配额、日志、缓存,对上游看又是 2 次请求,第一次失败
的账已经被记了一次。多个用户并发问同一个问题时尤其浪费:明明临
时抖一下,我们却把整次推理丢给客户端去重试。

**这一章的判断:网关不再做"瞬态错误原地重试"——一次失败就回落到
下一条渠道。**三个理由:

1. **重试 vs 降级**:重试同一渠道是在赌"上游马上就好";多渠道架
   构下,赌输的代价是浪费 0.6s 退避 + 3 次请求。下一步更好的赌是
   换一条渠道。
2. **真实 new-api 也没在 relay 层做 retry**:Go 那边是切到下一优
   先级渠道,而不是 tenacity 风格的就地重试。我们这一章跟上。
3. **"瞬态"判定模糊**:4xx 是请求格式错,重试一万次还是 4xx;某
   些上游把 429 用作"配额不足"——重试也无济于事。明确重试白名单需
   要按 provider 分情况配置,超出本章范围。

所以这道闸门变成:

- **任何失败(transport error 或非 2xx)→ 把当前渠道标
  unhealthy,立即切到下一条渠道**。
- **所有渠道都失败 → 一次性返回最后的 status/body 给客户端**。

永久性错误(400、401 这种)也走"切下一条"——某条渠道返回 401 不代
表别条渠道也返回 401,多渠道架构的代价是每条之间要付失败成本(额
外的鉴权握手、额外的网络),但好处是单条故障不会拖垮整次请求。

## 方案

引入一个渠道级 for 循环：

- **`s13_retry_fallback/code.py`** —— FastAPI 装配。**重新定义**
  `/v1/chat/completions`（不再走 s12→s11→s10→s09→s08 那条链）；
  每次上游调用直接用 `client.post`，外层循环遍历
  `ch_mod.list_channels()`，失败一条就 `mark_unhealthy` 切下一条。
  最后挂载 s12（不是 s08），让 `/admin/cache/stats` 仍可达。
- **s13 自有路由** —— `@app.post("/v1/chat/completions")` 写在
  `app.mount("/", s12_app)` **之前**，Starlette 按注册顺序匹配路由，
  本地路由把挂载的同名路由挡住。这跟 `s04_multi_provider` 一样的 Starlette
  坑。

路由形状——下面这张块状路由表把本章要写的接口压成一览：左是 `method + path`，中间是入参（`/v1/chat/completions` 接 body），右是返回码与返回体；本章要写的核心就是"遍历渠道、失败即回落"一条转发路径 + 仍然挂着从 s12 来的一条统计接口。

```
POST /v1/chat/completions     body={model, messages}        -> 200 + 上游响应（或 502）
GET  /admin/cache/stats                                       -> 200 {size, live}（来自 s12）
```

`mark_unhealthy` 是**当下立刻**生效的——失败一条就标 unhealthy，下
一次同渠道的请求直接被 `for ch in candidates` 的 `if not ch["healthy
"]: continue` 跳掉。要恢复得手动调 `/admin/channels/{id}/enable`
（s10 已实现，本章不重复）。这是有意的——"再调一次也大概率失败"
的渠道不值得浪费一次回落机会。

## 工作原理

### 直接 `client.post`，没有重试包装

```python
async with httpx.AsyncClient(timeout=30.0) as client:
    for ch in candidates:
        if not ch["enabled"] or not ch["healthy"]:
            continue
        url = f"{ch['base_url']}/v1/chat/completions"
        try:
            r = await client.post(url, content=body, headers=upstream_headers)
        except httpx.HTTPError as exc:
            # Transport 失败（超时、连接拒绝、DNS）——回落到下一条
            last_error = str(exc)
            ch_mod.mark_unhealthy(ch["id"])
            continue
        if r.status_code < 400:
            ...
            return JSONResponse(translated)
        # 非 2xx——回落到下一条
        last_status = r.status_code
        last_body = r.text
        last_error = f"{r.status_code}: {r.text}"
        ch_mod.mark_unhealthy(ch["id"])
```

三件事：

1. **遍历所有 enabled & healthy 渠道**。`list_channels()` 出来的顺
   序是按注册顺序，不是按 priority 排——priority 在 s10 的
   `pick_channel_for` 里排过，**本章直接信任调用方传入的
   `candidates` 顺序**。如果你的调用方乱序，外面自己 sort 一下。
   本章用 `list_channels()` 是因为 channel 数量小（< 10 条），为几
   条渠道重排优先级不值。
2. **每条渠道最多被调用一次**——失败立刻 `mark_unhealthy` + 进入下
   一条循环。失败成本只是"多付出一次 HTTP 握手"，比重试同一渠道
   省 600ms+ 的退避等待。
3. **最后一道总闸**：`raise HTTPException(last_status or 502, ...)`。
   所有渠道都失败才返回错误。如果最后一次是非 2xx，就把那个 status
   原样转给客户端（带上 `last_body`，方便排查）；如果都是 transport
   error，则返回 502 + 最后一次的错误文本。

### 不做熔断器

`mark_unhealthy` 是**当下立刻**生效：一次失败就标 unhealthy，下一
条请求直接跳过这条渠道。熔断器模式会做滞后判定（"30 秒内失败率
> 50% 才断开"，避免单次毛刺把好渠道误下线）。本章 YAGNI：单次抖
动就下线一条渠道至少省 600ms 的重试时间，代价是偶尔把好渠道误下
线 1 个请求窗口。生产里上熔断器（`sony/gobreaker` 之类的库）是下
一步。

### 路由顺序：本地路由挡挂载

```python
app = FastAPI(title="learn-new-api s13")

@app.post("/v1/chat/completions")  # 本地路由先注册
async def chat_with_retry(req): ...

app.mount("/", s12_app)  # 挂载 s12 最后
```

Starlette 按注册顺序迭代路由。客户端打 `/v1/chat/completions`，Sta
rlette 看到本地有这条路由，直接处理，根本不进 s12 那条挂载链——所以
s12 → s11 → s10 → s09 → s08 那一长串 chat 端点逻辑本章**完全跳过**。
这是有意的：s13 是 chat 端点的"带回落"版本，应该替换而不是叠加。

但 s12 的 `/admin/cache/stats` 路由仍然可达——因为本地没定义它，
Starlette 接着往下走到挂载链，s12 自己 match 上。

## 运行

```bash
# 起服务
python s13_retry_fallback/code.py          # PORT 默认 8013

# 准备渠道（chat 端点要至少一条渠道；多条时按 priority 排）
python -c "
from s10_channel_management.channels import create_channel
create_channel('c1', 'openai', 'https://api.openai.com', weight=100, priority=0)
create_channel('c2', 'openai', 'https://api.openai-2.com', weight=100, priority=1)
"

# 正常请求
curl -s -X POST http://localhost:8013/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'

# 模拟上游 503（用 mock 或自己搭一个返回 503 的服务）
# s13 立即把当前渠道标 unhealthy，跳到下一条

# 看一眼缓存状态（来自挂载的 s12）
curl -s http://localhost:8013/admin/cache/stats
# -> {"size": 1, "live": 1}
```

## 测试

```bash
pytest tests/test_s13_retry_fallback.py -v
```

两个测试覆盖主契约：

| 测试 | 断言 |
| --- | --- |
| `test_escalates_to_next_channel_on_failure` | 两条渠道：primary 返回 503、secondary 返回 200。最终客户端拿到 200；mock.calls.call_count == 2（每条渠道被调 1 次）。 |
| `test_unauthenticated_request_rejected` | 没带 Bearer token 直接 401，上游不被调用（call_count == 0）。 |

`_clean` fixture 重置 `s10_channel_management.channels`、`s07` 的 quota、
`s08` 的 rate-limit bucket、`s05` 的 api-key 注册表——`/v1/chat/completions`
依赖的所有 in-memory 状态。

**为什么 `assert mock.calls.call_count == 2`？** 本章测试就是要锁住
"立即回落，不重试"这个不变量。如果第一条渠道被调 2 次，说明回到
了 tenacity 式重试——这正是我们要防的回归。

## → new-api 源码

真实部署里同样的"渠道级回落"在 Go 里长这样：

- `service/channel_select.go` —— 渠道选择 + 回落的真正核心。里面定
  义了 `RetryParam` 结构（`Retry`、`resetNextTry`、`IncreaseRetry()`
  `ResetRetryNextTry()`），还有 `CacheGetRandomSatisfiedChannel` 处
  理跨分组轮询、按 priority 选下一条渠道。注意这里的 `Retry` 不是
  我们理解的"原地重试"——它的语义是"对当前 priority tier 内的渠道
  多试几次，再用 retry counter 切换到下一 tier"。我们这一章用
  Python 模拟的"for ch in candidates + mark_unhealthy"是它的最小
  替身——同分组内 priority 用完才换组、按 priority 从小到大选下一条。
- `service/channel.go` —— 渠道状态机（启用/禁用、健康/不健康、禁用
  时间）。`mark_unhealthy` 标的就是这里的 `Channel.Status`。本章直
  接改内存 dict；真实部署里要走 GORM 写库 + Redis 缓存（避免每条请
  求都打 DB）。
- `relay/helper/` 里的 relay 入口 —— 这里是真正调上游的地方。我们
  这一章把"调上游 + 回落渠道"全塞进一个 handler；new-api 是按层拆
  开：handler 调 relay、relay 调 provider、provider 调 HTTP，回落和
  切渠道在 relay 那一层。

> Windows 文件系统不分大小写，本地 IDE 里看着像 `ChannelSelect.go`
> 不少见；部署到 Linux/macOS 时按实际的小写路径 `channel_select.go`
> 访问。

## 取舍

- **不做 in-request 重试** —— 一次失败就回落，单条抖动就被换掉。
  在信道稳定的 production 里，这点代价是值得的：在 1s 内换掉 1 个
  不稳定的渠道 vs 死磕 0.6s + 3 次同渠道重试，前者更可预测。
- **`mark_unhealthy` 没有自动恢复** —— 一旦被标 unhealthy，得手动
  调 s10 的 `/admin/channels/{id}/enable`。真实部署里会跑一个后台
  健康检查任务（比如每 30s 发一次 OPTIONS 请求），发现恢复了就
  `mark_healthy`。本章不实现后台任务，YAGNI。
- **没有"瞬态 vs 永久"区分** —— 4xx 也走回落。会浪费一两次握手
  换另一条渠道 401，但好处是状态码白名单不再需要维护。生产里如
  果在意这点延迟，可以参考 new-api 的 `RetryParam` 加一道"401/403
  立刻 upgrade"的快速路径。
- **没有按渠道的 rate limit 联动** —— 渠道被限流（429）后我们只
  是换下一条，但不会把 rate-limit 信息缓存下来做"这家过去 1 分钟
  限流 3 次，跳过"。本章每条请求独立判。
- **本地路由挡挂载 → s11 日志记不到 chat 调用的 model 了** —— s11
  的 `LogMiddleware` 是包在挂载链里的，s13 把 chat 路由提到本地，
  请求不再经过 s11 的中间件。这意味着这一章之后 chat 调用的日志丢
  了。下一章 s14 要么把日志中间件也提上来、要么重新设计——本章
  故意不动，等下一章决定。
- **chat 端点提到本地，挂载链里的同名路由被本章节的注册顺序遮蔽** —— `@app.post("/v1/chat/completions")` 注册在 `app.mount("/", s12_app)` 之前，Starlette 按顺序匹配，本地路由胜出。挂载 s12 仍然存在，只是为了让 `/admin/cache/stats` 这种 s12 独有路由可达。

## 下章预告

s13 通道稳了,但调用方只能等响应,看不到调用历史。s14 加后台 dashboard,运营能看到日志和渠道状态。
