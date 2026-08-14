# s13：临时性错误重试 + 渠道级回退（tenacity + 下一优先级渠道）

> Previous: [s12](../s12_caching/) · Next: [s14](../s14_admin_dashboard/)

## 问题

走到第 12 章，网关已经把"上游能不能正常响应"这件事赌在一个渠道
上：s08 拿到请求 → s10 选一条渠道 → 调上游 → 把响应吐回客户端。上游
只要抖一下（HTTP 503 "模型服务暂时过载"、502 "网关挂了"、504
"超时"、429 "被限流"），客户端直接看到一个错误，然后重试。

但客户端重试是**全栈重试**——重试一次要把整条请求重新走一遍认证、
计费、配额、日志、缓存，对上游的视角看又是 2 次请求，第一次失败
的账已经被记了一次。在多个用户并发问同一个问题时尤其浪费：明明
临时抖一下，等 200ms 再试大概率就好，我们却把整次推理丢给客户端
去重试。

我们需要两道"闸门"：

1. **临时性错误 → 在网关内部重试**。502/503/504/429 这四个状态码代
   表"上游现在不行，但马上可能行"——指数退避 0.2s / 0.4s / 0.8s 最
   多试 3 次。3 次都失败才算真的失败。
2. **永久性失败 → 切到下一个渠道**。如果某条渠道整个挂了（3 次都
   503），把它标记为 unhealthy，往下走下一条渠道（按 priority 从小
   到大）。同一份 prompt 不应该因为一家上游抖一下就 502。

这道闸门只针对**临时性错误**——400（参数错）、401（鉴权错）这种
"重试一万次也不会变好"的错误必须**立刻失败**回客户端，不能被闸门
吞掉。

## 方案

引入一个 tenacity 装饰器 + 一个渠道级 for 循环：

- **`s13_retry_fallback/code.py`** —— FastAPI 装配。**重新定义**
  `/v1/chat/completions`（不再走 s12→s11→s10→s09→s08 那条链）；
  用 tenacity 包装单次上游调用 `_call_with_retry`，外层循环遍历
  `ch_mod.list_channels()`，失败一条就 `mark_unhealthy` 切下一条。
  最后挂载 s12（不是 s08），让 `/admin/cache/stats` 仍可达。
- **`_call_with_retry`** —— tenacity 装饰的协程。3 次 attempts、
  exponential backoff（multiplier 0.2，min 0.2，max 2.0），只对
  `httpx.HTTPError` 重试——其它异常透传。装饰器把"瞬态 5xx/429"也
  转成 `httpx.HTTPError` 抛出（httpx 默认不会把状态码当异常）。
- **s13 自有路由** —— `@app.post("/v1/chat/completions")` 写在
  `app.mount("/", s12_app)` **之前**，Starlette 按注册顺序匹配路由，
  本地路由把挂载的同名路由挡住。这跟 s04.3 / s04.2 一样的 Starlette
  坑。

路由形状：

```
POST /v1/chat/completions     body={model, messages}        -> 200 + 上游响应
GET  /admin/cache/stats                                       -> 200 {size, live}（来自 s12）
```

`mark_unhealthy` 是**当下立刻**生效的——失败一条就标 unhealthy，下
一次同渠道的请求直接被 `for ch in candidates` 的 `if not ch["healthy
"]: continue` 跳掉。要恢复得手动调 `/admin/channels/{id}/enable`
（s10 已实现，本章不重复）。这是有意的——临时抖一下不该永久下线一
条渠道，但当下"再调一次也大概率 503"的渠道不值得再花 200ms 试。

## 工作原理

### `_call_with_retry`：tenacity 装饰器

```python
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.2, min=0.2, max=2.0),
    retry=retry_if_exception_type(httpx.HTTPError),
    reraise=True,
)
async def _call_with_retry(client, url, headers, body) -> httpx.Response:
    r = await client.post(url, content=body, headers=headers)
    if r.status_code in TRANSIENT:
        raise httpx.HTTPError(f"transient {r.status_code}")
    return r
```

四个关键点：

1. **`stop=stop_after_attempt(3)`** —— 最多 3 次。0.2s + 0.4s = 0.6s
   的总退避 + 3 次实际请求 = 至少 1 秒花在重试上。客户端应该会自己
   也有超时，所以这个上限是合理的。
2. **`wait=wait_exponential(multiplier=0.2, min=0.2, max=2.0)`** ——
   第 1 次后等 0.2s，第 2 次后等 0.4s。第 3 次没机会等（再失败就抛
   出去）。`min=0.2` 是 tenacity 默认值，写出来显式强调。
3. **`retry=retry_if_exception_type(httpx.HTTPError)`** —— 只对
   `httpx.HTTPError` 重试。其它异常（连接拒绝、DNS 失败）也是它的
   子类，所以一并覆盖。`HTTPStatusError` 也是子类——但我们走的是
   "手动 raise HTTPError"，不依赖 httpx 的 `raise_for_status()`。
4. **`reraise=True`** —— 第 3 次失败后 tenacity 把最后的异常原样抛
   出去，否则它会抛 `RetryError` 包一层，外层 try/except 接不到
   `httpx.HTTPError`，得专门 unwrap。

**为什么不用 `tenacity.AsyncRetrying` + `for attempt in ...`？**
装饰器形态最简单——直接 `@retry(...)` 贴在协程上就行，调用点完全
不知道有重试这回事；可读性最好。`AsyncRetrying` 的写法控制更细但
啰嗦，本章 YAGNI。

### 把"瞬态 5xx"伪装成 `httpx.HTTPError`

```python
r = await client.post(url, content=body, headers=headers)
if r.status_code in TRANSIENT:
    raise httpx.HTTPError(f"transient {r.status_code}")
return r
```

`httpx` 默认不把状态码当异常——4xx/5xx 都是正常返回的 `Response`，
不 raise。tenacity 的 `retry_if_exception_type(httpx.HTTPError)` 也
不会主动重试一个非异常。所以我们自己把"瞬态状态码"包成
`httpx.HTTPError` 抛出去，让 tenacity 接得到。这样做的好处是**只对
瞬态状态码重试**，4xx 客户端错、5xx 之外的错误（501/505/...）不会
被错误地重试。

### 渠道级 for 循环 + mark_unhealthy

```python
async with httpx.AsyncClient(timeout=30.0) as client:
    for ch in candidates:
        if not ch["enabled"] or not ch["healthy"]:
            continue
        url = f"{ch['base_url']}/v1/chat/completions"
        payload = req.model_dump(exclude_none=True)
        body = marshal(payload)
        try:
            r = await _call_with_retry(client, url, headers, body)
            if r.status_code < 400:
                return r.json()
            last_error = f"{r.status_code}: {r.text}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        ch_mod.mark_unhealthy(ch["id"])
raise HTTPException(status_code=502, detail=last_error or "all channels failed")
```

三件事：

1. **遍历所有 enabled & healthy 渠道**。`list_channels()` 出来的顺
   序是按注册顺序，不是按 priority 排——priority 在 s10 的
   `pick_channel_for` 里排过，**本章直接信任调用方传入的
   `candidates` 顺序**。如果你的调用方乱序，外面自己 sort 一下。
   本章用 `list_channels()` 是因为 channel 数量小（< 10 条），priority
   重排的复杂度不值。
2. **`_call_with_retry` 内部已经做了指数退避重试**。所以这里一个渠
   道最多花 ~1s（3 次请求 + 0.6s 退避）。3 次都失败 → 跳出 try →
   `mark_unhealthy` → 进入下一条渠道。
3. **最后一道总闸**：`raise HTTPException(502, ...)`。所有渠道都
   失败才返回 502，把最后一次的错误文本当 detail 报给客户端，方便
   排查。

### 路由顺序——本地路由挡挂载

```python
app = FastAPI(title="learn-new-api s13")

@app.post("/v1/chat/completions")  # 本地路由先注册
async def chat_with_retry(req): ...

app.mount("/", s12_app)  # 挂载 s12 最后
```

Starlette 按注册顺序迭代路由。客户端打 `/v1/chat/completions`，Sta
rlette 看到本地有这条路由，直接处理，根本不进 s12 那条挂载链——所以
s12 → s11 → s10 → s09 → s08 那一长串 chat 端点逻辑本章**完全跳过**。
这是有意的：s13 是 chat 端点的"升级版"，应该替换而不是叠加。

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
# s13 自动重试 3 次、间隔 0.2s / 0.4s；如果 3 次都失败，
# 把当前渠道标记为 unhealthy，然后下一条渠道继续试

# 看一眼缓存状态（来自挂载的 s12）
curl -s http://localhost:8013/admin/cache/stats
# -> {"size": 1, "live": 1}
```

## 测试

```bash
pytest tests/test_s13_retry_fallback.py -v
```

一个测试覆盖主契约：

| 测试 | 断言 |
| --- | --- |
| `test_retries_transient_then_succeeds` | 上游 mock 返回 503, 503, 200。客户端拿到 200；mock.calls.call_count == 3（说明 tenacity 重试了 3 次，最后一次 200 成功）。 |

`_clean` fixture 重置 `s10_channel_management.channels` —— chat 端点
依赖的唯一状态。

**为什么 `assert mock.calls.call_count == 3` 而不是 `== 1`？** 本章
测试就是要证明 tenacity 在重试。如果 mock 只被调 1 次，说明重试没
生效——这正是我们要防的回归。`call_count == 3` 是**稳定断言**（3
次尝试：503, 503, 200），不依赖时序。

## → new-api 源码

真实部署里同样的"重试 + 渠道回退"在 Go 里长这样：

- `service/channel_select.go` —— 渠道选择 + 重试的真正核心。里面定
  义了 `RetryParam` 结构（`Retry`、`resetNextTry`、`IncreaseRetry()`
  `ResetRetryNextTry()`），还有 `CacheGetRandomSatisfiedChannel` 处
  理跨分组轮询、按 priority 选下一条渠道。我们这一章用 Python 模拟
  的"for ch in candidates + mark_unhealthy"是它的最小替身——同分组
  内 priority 用完才换组、按 priority 从小到大选下一条。
- `service/channel.go` —— 渠道状态机（启用/禁用、健康/不健康、禁用
  时间）。`mark_unhealthy` 标的就是这里的 `Channel.Status`。本章直
  接改内存 dict；真实部署里要走 GORM 写库 + Redis 缓存（避免每条请
  求都打 DB）。
- `relay/helper/` 里的 relay 入口 —— 这里是真正调上游的地方。我们
  这一章把"调上游 + 重试 + 切渠道"全塞进一个 handler；new-api 是
  按层拆开：handler 调 relay、relay 调 provider、provider 调 HTTP，
  重试和切渠道在 relay 那一层。

> Windows 文件系统不分大小写，本地 IDE 里看着像 `ChannelSelect.go`
> 不少见；部署到 Linux/macOS 时按实际的小写路径 `channel_select.go`
> 访问。

## 取舍

- **没有熔断器（circuit breaker）的滞后判定** —— 我们 `mark_unhealthy`
  是**当下立刻**生效：第一次失败就标 unhealthy，下一条请求直接跳过
  这条渠道。熔断器模式会做滞后判定（比如"30 秒内失败率 > 50% 才断
  开"，避免单次毛刺把好渠道误下线）。本章 YAGNI：单次抖动就下线一
  条渠道至少省了 600ms 的重试时间，代价是偶尔把好渠道误下线 1 个
  请求窗口。生产里上熔断器（`sony/gobreaker` 之类的库）是下一步。
- **`mark_unhealthy` 没有自动恢复** —— 一旦被标 unhealthy，得手动
  调 s10 的 `/admin/channels/{id}/enable`。真实部署里会跑一个后台
  健康检查任务（比如每 30s 发一次 OPTIONS 请求），发现恢复了就
  `mark_healthy`。本章不实现后台任务，YAGNI。
- **指数退避无 jitter** —— tenacity 默认是 `wait_exponential(multi
  plier, min, max)`，没有 jitter。多条请求同时被同一上游 503 时，
  没有 jitter 会让它们在同一时刻一起重试（thundering herd）。
  生产里建议加 `wait_random_exponential(multiplier=0.2, max=2.0)`
  把重试时刻错开；本章两条请求同一时刻重试撞在一起的概率低，YAGNI。
- **`TRANSIENT = (502, 503, 504, 429)` 是硬编码** —— 没有按渠道、
  按 provider 区分。某些上游可能把 503 用作"请求格式错"（永久错），
  对它们重试是浪费；某些上游把 429 用作"配额不足"（重试还是要 429）。
  生产里这表应该来自配置（`setting/operation.go` 之类），可以热更。
  本章用 4 个标准状态码是 YAGNI——覆盖了 80% 场景。
- **本地路由挡挂载 → s11 日志记不到 chat 调用的 model 了** —— s11
  的 `LogMiddleware` 是包在挂载链里的，s13 把 chat 路由提到本地，
  请求不再经过 s11 的中间件。这意味着这一章之后 chat 调用的日志丢
  失了。下一章 s14 要么把日志中间件也提上来、要么重新设计——本章
  故意不动，等下一章决定。
- **没有按渠道的 rate limit 联动** —— 渠道被限流（429）后重试 3 次
  还是会 429；理想做法是判断 429 立刻 `mark_unhealthy` 切下一条，
  不再花 600ms 重试 3 次同一个被限流的渠道。生产里这是显著优化；
  本章统一走 `TRANSIENT` 重试 + 失败后切渠道，简单但保守。
- **路径 `/v1/chat/completions` 而不是 `/v1/v1/chat/completions`** ——
  s13 自有路由注册在本地 + 路径写 `/v1/chat/completions`，**不**
  走 s12→s11→s10→s09→s08 那条会出 `/v1/v1/...` 的链。这是本章的有
  意设计：s13 是 chat 端点的替代实现，路径回到最自然的 `/v1/chat/
  completions`。如果以后想做"老路径仍可达"的兼容性挂载，可以同时
  在挂载 s12 之后挂载一个 strip-prefix 适配层。