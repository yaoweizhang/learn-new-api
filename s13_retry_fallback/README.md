# s13: 失败即回落——渠道级 fallback(不重试,换下一条) — 失败 → 标 unhealthy → 换下一个

> Previous: [s12](../s12_caching/) · Next: [s14](../s14_admin_dashboard/)

> *"失败即换下一条"* —— 重试浪费，换渠道更便宜。

> **Layer**：L4 路由与韧性

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

## 本章要做什么

现在场景是:走到第 12 章,网关把"上游能不能正常响应"这件事赌在一条渠道上:s08 拿到请求 → s10 选一条渠道 → 调上游 → 把响应吐回客户端。上游只要抖一下(HTTP 503、502、504、429),客户端直接看到一个错误,然后自己重试。问题在于客户端重试是**全栈重试**——重试一次要把整条请求重新走一遍认证、计费、配额、日志、缓存,对上游看又是 2 次请求,第一次失败的账已经被记了一次。要解决这个——**我们把 chat 端点提到本地,跑一条渠道级 for 循环**:每条渠道最多被调一次,失败立刻 `mark_unhealthy` + 切下一条;所有渠道都失败才一次性返回错误。本章把这条韧性带写出来:

1. **写一段渠道级 for 循环 —— 为什么不用 tenacity 式重试**: `for ch in candidates:` 遍历 `ch_mod.list_channels()` 出来的每条渠道,`try: r = await client.post(url, content=body, headers=upstream_headers)`,成功直接 `return JSONResponse(translated)`,失败一条就 `ch_mod.mark_unhealthy(ch["id"]) + continue`。**为什么不原地重试**:重试同一渠道是在赌"上游马上就好",多渠道架构下赌输的代价是 0.6s 退避 + 3 次请求——下一步更好的赌是换一条渠道。**为什么不重试 4xx**:4xx 是请求格式错,重试一万次还是 4xx;明确重试白名单要按 provider 分情况配置,超出本章范围——多渠道架构的代价是每条之间多付一次失败成本,好处是单条故障不拖垮整次请求。
2. **`mark_unhealthy` 即时生效 —— 为什么不做熔断器**: 失败当下把 `ch.healthy = False`,下一条请求 `for ch in candidates` 的 `if not ch["healthy"]: continue` 直接跳过。**为什么不做滞后判定**:熔断器模式要"30 秒内失败率 > 50% 才断开"避免单次毛刺误下线,本章 YAGNI——单次抖动就下线一条渠道至少省 600ms 重试时间,代价是偶尔把好渠道误下线 1 个请求窗口。要恢复得手动调 s10 的 `/admin/channels/{id}/enable`(s10 已实现,本章不重复);真实部署会跑后台健康检查任务定时 `mark_healthy`。
3. **最后一道总闸 502 —— 为什么把最后一次错误原样回吐**: 所有渠道都失败时,如果最后一次是非 2xx,`raise HTTPException(status_code=last_status, detail=last_body)` 把 status 原样转给客户端(带上 `last_body` 方便排查);如果都是 transport error,则返 502 + 最后一次的错误文本。**为什么不吞掉 status**:客户端在网关层看到 429 还能退避、看到 502 知道"上游挂了/网关全部回落失败",被洗成统一 500 反而丢信息。**为什么失败时 `refund(p.user_id, estimate)`**:全失败的请求不该被算钱,和 s07 的失败整笔退款契约保持一致。
4. **chat 端点提到本地挡挂载 —— 为什么不能继续走 s12 → s11 → s10 → s09 → s08 链**: `@app.post("/v1/chat/completions")` 注册在 `app.mount("/", s12_app)` 之前,Starlette 按顺序匹配路由,本地路由直接胜出。**为什么替换而不是叠加**:s13 是 chat 端点的"带回落"版本,叠加会在 s08 的旧 chat 路由 + s13 的本地路由之间产生 split-brain(一次请求只走一条)。**但 s12 的 `/admin/cache/stats` 仍可达**:本地没定义它,Starlette 接着往下走到挂载链,s12 自己 match 上。

成品:两条渠道——primary 返 503、secondary 返 200,客户端拿到 200,`mock.calls.call_count == 2`(每条渠道各被调 1 次,锁住"立即回落不重试"这个不变量);全失败时一次性返回最后一次的错误给客户端,所有预扣配额整笔 refund。后续 s14 把日志和渠道状态拉到 Jinja2 后台;真实部署把 `mark_unhealthy` 改成 GORM 写库 + Redis 缓存,后台健康检查任务定时 `mark_healthy`。

## 方案

现在的场景是:`## 问题` 提了一件痛——s12 把缓存这条路解决了"快",但上游瞬时 502 / 503 / 504 时,客户端依然直接看到错误,然后全栈重试(鉴权 + 计费 + 配额 + 日志 + 缓存全部重跑一遍)。这件事**没法靠"客户端按渠道切流"或"s12 缓存兜底"能解决**——缓存只对相同 prompt 命中,瞬时错误属于"换一次请求就消失"那一类,根本不命中缓存,必须由网关在 chat 端点层跑一条 for-loop 渠道序列、失败立即 mark_unhealthy + 切下一条。

**要解决这个——我们在网关里引入一个渠道级 for 循环**——按顺序遍历候选渠道,失败一条就立刻切下一条:

- **`s13_retry_fallback/code.py`** —— FastAPI 装配。**重新定义**
  `/v1/chat/completions`(不再走 s12→s11→s10→s09→s08 那条链);
  每次上游调用直接用 `client.post`,外层循环遍历
  `ch_mod.list_channels()`,失败一条就 `mark_unhealthy` 切下一条。
  最后挂载 s12(不是 s08),让 `/admin/cache/stats` 仍可达。
- **s13 自有路由** —— `@app.post("/v1/chat/completions")` 写在
  `app.mount("/", s12_app)` **之前**,Starlette 按注册顺序匹配路由,
  本地路由把挂载的同名路由挡住。这跟 `s04_multi_provider` 一样的 Starlette
  坑。

下面这幅图把这件痛各放到五个角色里:

- **`Client` (调用方)** —— 在装上 s13 之前,这是被"上游一次抖动就 502 + 客户端全栈重试"困住两难的角色;装上之后,这事被中继解——Client 只管发请求,上游瞬时失败在中继内部被"换下一条"消化掉,对客户端看永远是 200 或最后一手的 502。
- **`Relay` (本章要写的本地 chat 路由)** —— 把痛的解决动作集中放在这里:`@app.post("/v1/chat/completions")` 注册在 `app.mount("/", s12_app)` 之前,把 s12→s11→s10→s09→s08 那条挂载链的同名路由挡住;handler 内 `async with httpx.AsyncClient(timeout=30.0) as client:` 开连接池 → `for ch in candidates:` 遍历 → `try: await client.post(url, content=body, headers=upstream_headers)` 调一条,成功直接 `return JSONResponse(translated)`,失败一条就 `ch_mod.mark_unhealthy(ch["id"]) + continue`。Client 不知道有几条渠道,Pool 不知道请求路径,Upstream 看不见其它渠道存在。
- **`Channels pool` (s10 的内存 dict)** —— 本章直接复用 s10 的 `_channels: dict[int, Channel] + threading.Lock`。每次进入 handler 调 `ch_mod.list_channels()` 拿快照;`mark_unhealthy(cid)` 立即把该渠道 `healthy` 字段置 False——下一条请求 `if not ch["healthy"]: continue` 直接跳过。
- **`c1` / `c2` / `c3` (三条候选渠道)** —— 实际可选的多家上游。按 list_channels 顺序遍历(本章 channel 数量小 < 10 条, 按注册顺序够用),优先级 / weight 在 s10 选路时已排过;本章直接信任 caller 传入的顺序。c1 失败 → mark_unhealthy(c1) + 切到 c2;c2 失败 → 切到 c3。
- **`502` (全失败出口)** —— 所有渠道都失败时一次性 `raise HTTPException(last_status or 502, ...)`:如果最后一次是非 2xx,原样把 `last_status` + `last_body` 透传给客户端;如果都是 transport error,则返 502 + 最后一次的错误文本。失败时同步 `refund(p.user_id, estimate)` 整笔退回——用户不为失败调用付费。

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

**原理**: 一个 chat 请求从客户端进来, 它的生命周期是: `Starlette` 按注册顺序匹配路由 → 本地 `@app.post("/v1/chat/completions")` 注册在 `app.mount("/", s12_app)` 之前, 直接胜出挂载的同名路由 → handler 内 `ch_mod.list_channels()` 拿候选渠道快照 → `async with httpx.AsyncClient(timeout=30.0) as client:` 开连接 → `for ch in candidates:` 遍历 → `if not ch["enabled"] or not ch["healthy"]: continue` 跳过 → `try: r = await client.post(f"{ch['base_url']}/v1/chat/completions", content=body, headers=upstream_headers)` 调一条 → 成功路径(`r.status_code < 400`)走 `provider.from_upstream` 翻回 OpenAI 形态 → `return JSONResponse(translated)`; 失败路径(transport error 或非 2xx) 走 `ch_mod.mark_unhealthy(ch["id"]) + continue` → 全部失败时 `raise HTTPException(last_status or 502, ...)`,失败整笔 `refund(p.user_id, estimate)` 退回配额。整章所有部件都为"遍历 + 失败即切"这条主线服务。

**1. 一个本地 chat route handler (`POST /v1/chat/completions`,注册在 mount 之前)** —— Starlette 按注册顺序迭代路由,本地路由先注册,客户端打 `/v1/chat/completions` 时直接被 s13 本地 handler 拦截,根本不走 s12→s11→s10→s09→s08 那条挂载链的 chat 路由;`/admin/cache/stats` 这种 s12 独有路由依然可达,因为本地没定义它,Starlette 接着往下走到挂载链,被 s12 自己 match 上。

**2. 一个 for-loop channel selector (`handler` 内的 `for ch in candidates`)** —— `candidates = ch_mod.list_channels()`(按注册顺序);遍历时 `if not ch["enabled"] or not ch["healthy"]: continue` 跳过——`mark_unhealthy(cid)` 之后该渠道 `healthy=False`, 后续请求的 `continue` 直接跳过,这就是"mark_unhealthy 即时生效"的实现机制。每条渠道最多被调一次——失败立刻进入下一条,失败成本只是"多付出一次 HTTP 握手",比重试同一渠道省 600ms+ 的退避等待。

**3. 一个 direct `client.post` forwarder (`httpx.AsyncClient.post`,无 tenacity 重试包装)** —— `async with httpx.AsyncClient(timeout=30.0) as client:` 开连接池;每次失败 / 成功只 `try / except httpx.HTTPError` 一次, 没有 tenacity 风格的"原地重试"。多渠道架构下"赌输"代价是浪费 0.6s 退避 + 3 次请求, 下一步更好的赌是换一条渠道。

**4. 一个 final-fail total-reject (`raise HTTPException(last_status or 502, ...)`)** —— 所有渠道都失败才一次性返回:如果最后一次是非 2xx,把 `last_status` + `last_body` 原样透传给客户端(带 `last_body` 方便排查);如果都是 transport error,返 502 + 最后一次的错误文本。失败时同步 `quota.refund(p.user_id, estimate)` 整笔退回——s07 的失败整笔退款契约保持一致。

**5. 一个 mark_unhealthy 即时钩子 (`channels.mark_unhealthy(ch["id"])`)** —— 失败当下把 `ch.healthy = False`, 下一条请求 `if not ch["healthy"]: continue` 直接跳过; 不做滞后判定(熔断器模式:30 秒内失败率 > 50% 才断开), 单次抖动就下线一条渠道至少省 600ms 重试时间, 代价是偶尔把好渠道误下线 1 个请求窗口。要恢复得手动调 `/admin/channels/{id}/enable`(s10 已实现)。

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

确认渠道挂一条能切下一条?打上面这条 `curl`(注册 c1 + c2 两条渠道,c1 mock 一个返 503 的端点)——客户端拿到 c2 的 200 而不是 c1 的 503,说明 `for ch in candidates` 遍历到 c1 失败后调 `ch_mod.mark_unhealthy(c1["id"])`,把 `c1.healthy=False`,再 `continue` 进入 c2;mock 的 `call_count == 2`(每条渠道各被调 1 次,锁住"立即回落不重试"这个不变量),说明本地 chat 路由挡 mount 生效、for-loop 顺序遍历 + 失败即切都到位:

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

## 本章不做什么

- **不做 in-request 重试** (tenacity 风格的同渠道原地重试 + 指数退避)——一次失败就回落，单条抖动就被换掉。在信道稳定的 production 里，这点代价是值得的：在 1s 内换掉 1 个不稳定的渠道 vs 死磕 0.6s + 3 次同渠道重试，前者更可预测。真实 new-api 的 `RetryParam` 是在"同 priority tier 内多试几次再切 tier"，跟我们这一章的"一次失败就换渠道"语义不同——本章用最小 Python 模拟替代。
- **不做熔断器** (滞后判定:30 秒内失败率 > 50% 才断开,避免单次毛刺误下线)——单次抖动就下线一条渠道至少省 600ms 的重试时间,代价是偶尔把好渠道误下线 1 个请求窗口。生产里上熔断器(`sony/gobreaker` 之类的库)是下一步。→ 真实部署的 channel_select.go 用熔断器窗口聚合失败率。
- **没有"瞬态 vs 永久"区分** —— 4xx 也走回落。会浪费一两次握手换另一条渠道 401，但好处是状态码白名单不再需要维护。生产里如果在意这点延迟，可以参考 new-api 的 `RetryParam` 加一道"401/403 立刻 upgrade"的快速路径。
- **没有按渠道的 rate limit 联动** (把 rate-limit 信息缓存下来做"这家过去 1 分钟限流 3 次,跳过")——渠道被限流（429）后我们只是换下一条，但不会跨请求记忆"限流频率"。本章每条请求独立判。
- **没有后台健康检查 / 自动恢复** ——一旦被标 `unhealthy`,得手动调 `/admin/channels/{id}/enable`。真实部署里会跑一个后台健康检查任务（比如每 30s 发一次 OPTIONS 请求），发现恢复了就 `mark_healthy`。本章 YAGNI,留给后续章节。

## 已知限制

- **本地路由挡挂载 → s11 日志记不到 chat 调用的 model 了** (s11 `LogMiddleware` 是包在挂载链里的)——s13 把 chat 路由提到本地,请求不再经过 s11 的中间件。这意味着这一章之后 chat 调用的日志丢了。下一章 s14 要么把日志中间件也提上来、要么重新设计——本章故意不动,等下一章决定。
- **`mark_unhealthy` 没有自动恢复** ——一旦被标 unhealthy,得手动调 s10 的 `/admin/channels/{id}/enable`。真实部署里会跑一个后台健康检查任务(比如每 30s 发一次 OPTIONS 请求),发现恢复了就 `mark_healthy`。本章不实现后台任务,YAGNI。
- **没有 chat 端点的真实上游转发** ——本章只用 `client.post` 调上游 + 翻回 OpenAI 形态,但不会像 s08 那样接 s10 的 `pick_channel_for(model)` 选路。本章直接用 `list_channels()` 按注册顺序遍历——channel 数量小(< 10 条)够用;数量上来后应该在外面先 sort 再遍历。
- **`list_channels()` 顺序是注册顺序** ——不按 priority 排序。本章直接信任 caller 传入的 `candidates` 顺序;如果你的调用方乱序,外面自己 sort 一下。
- **`threading.Lock` 不跨 worker** (单进程锁,多 worker 进程下不互斥)——`asyncio` 单 worker 部署够用,但多 worker 时每个 worker 各持一份 `_channels`,`mark_unhealthy` 对其他 worker 不可见;上 Redis 共享 + Pub/Sub 广播是后续优化项。

## 设计选择

- **chat 端点提到本地,挂载链里的同名路由被本章节的注册顺序遮蔽** —— `@app.post("/v1/chat/completions")` 注册在 `app.mount("/", s12_app)` 之前,Starlette 按顺序匹配,本地路由胜出。挂载 s12 仍然存在,只是为了让 `/admin/cache/stats` 这种 s12 独有路由可达。**替换而不是叠加**:s13 是 chat 端点的"带回落"版本,叠加会在 s12 的旧 chat 路由 + s13 的本地路由之间产生 split-brain(一次请求只走一条)。
- **失败整笔 refund 而不是部分退** ——所有渠道都失败时 `quota.refund(p.user_id, estimate)` 整笔退回,用户不为失败调用付费。和 s07 的失败整笔退款契约保持一致;同步 refund 让用户余额状态始终和"成功收到的回复数"对得上,不留并发漏洞窗口。
- **失败状态码原样透传而不是洗成 500** ——最后一次非 2xx 时 `raise HTTPException(status_code=last_status, detail=last_body)`,把 status 原样转给客户端(带 `last_body` 方便排查);客户端在网关层看到 429 还能退避、看到 502 知道"上游挂了 / 网关全部回落失败",被洗成统一 500 反而丢信息。
- **不做白名单区分 4xx vs 5xx** ——4xx 也走回落。代价是"按渠道 401"会多付一次握手换另一条;好处是状态码白名单不再需要维护,运维配置面更窄。生产里如果在意这点延迟,可以参考 new-api 的 `RetryParam` 加一道"401/403 立刻 upgrade"的快速路径。

## 下章预告

s13 通道稳了,但调用方只能等响应,看不到调用历史。s14 加后台 dashboard,运营能看到日志和渠道状态。
