# s08: 按用户的令牌桶限速 — 60 个令牌突发,稳定 1 req/s

> Previous: [s07](../s07_pre_consume_settle/) · Next: [s09](../s09_user_system/)

> *"桶里取 token"* —— rate limit 就是漏桶原理。

> **Layer**：L3 计量与扣费

## 本章要做什么

s07 防住"算钱",但一个余额充足的用户仍可能 1 秒打 100 次,把同一代理上其它所有租户的延迟都拖下水——配额说"这个用户能花多少";我们还得说"这个用户能以**这个速率**花"。

要解决这个,在闸门之后、预扣之前插一份按用户的令牌桶:每个用户一份桶,容量 60、`refill_per_sec` 默认 1.0,每条请求消耗一枚令牌——令牌耗尽就 `429 Too Many Requests`,根本不到配额扣减和上游调用这一步。本章就把这条限速带写出来:

1. **写一个 `bucket` 模块 —— 为什么用 token bucket 而不是 fixed window**: `bucket.py` 暴露 `reset_buckets()` / `configure(uid, capacity, refill_per_sec)` / `take(uid, cost=1.0) -> bool`。**为什么是 token bucket**: 突发量天然在桶里 + 按时间窗线性补,允许"先冲一波然后稳定速率",语义直观;**为什么不选 fixed window**: fixed window 在窗口切换瞬间会出现"两倍突发"(上一秒尾 + 这一秒头),平滑度差;**为什么不选 leaky bucket**: leaky bucket 强制恒定速率、丢多余请求,网关场景里我们更想吸收突发再限速,所以 token bucket 才对路。
2. **take 整段原子 —— 为什么必须在同一把锁里**: `_refill(uid)`(按流逝时间补 token 至 cap)+ 检查 `tokens < cost`(不够就 `False`)+ `_buckets[uid] = (tokens - cost, now)`(扣)——三步合在 `threading.Lock` 下。**为什么不拆开**:同一用户并发请求不能"读到的都是刚补到位的 token、然后都被扣过"——拆开必双花;**为什么还是 `threading.Lock` 而非 `asyncio.Lock`**:纳秒级算式,加锁开销忽略,`asyncio.Lock` 在 await 点会让出反而误事。
3. **handler 在闸门后第一时间 `take` —— 为什么顺序是 401 → 429 → 402 → 上游**: `if not take(p.user_id): raise 429` 放在 `require_api_key` 通过之后、`deduct` 之前。**为什么 401 先于 429**:没有合法 key 的探测请求不该计入限速统计(否则匿名攻击能轻松用光别人的桶);**为什么 429 先于 402**:令牌耗尽说明用户对**当前速率**负责,不该让他继续预扣再失败退款——浪费配额表的写;**为什么 429 先于上游**:令牌是限速的唯一信号,放行到上游再发现超速就回不来了。
4. **默认 60 + 1/s —— 为什么是这两个数字**: `configure` 没显式调过时会自动给 `(60.0, 1.0)` —— 默认容量 60 突发 + 每秒 1 枚补充。**为什么是 60**:够普通用户一次会话突发用完,够攻击者在 1 秒内打满就被 429 顶回去;**为什么是 1/s**:稳态速率,符合"单租户别把上游打爆"的初衷。生产从 DB 读每用户 tier,本章写死。

成品: 一个 1000 token 配额的 `u1` 突发打 3 条 `/v1/chat/completions`,前两条 200、第三条 `429 rate limited`(桶里只剩 0 枚时被 `take` 拒掉);`sleep 1.1s` 后再发,令牌补到 ≥1, 又能过。后续 s09 把 `_buckets` 从进程内 dict 挪进 Redis 用 `INCR` + `EXPIRE` 让多 worker 共享状态;s10 给管理员加按 channel 的限速;s_full 给"按 user × model"做更细粒度的桶。

## 上一章复盘

s07 防住"算钱",但用户一秒可以打 100 次,公共资源被一个人吃完。

## 在整体中的位置

鉴权后的第一个"流量整形"步骤——保证一个吵闹用户不会把别人的延迟拖下水。

## 问题

s07 的配额控制的是**花费**——一个余额很足的用户仍可能把上游打爆,把同一代理上其它所有租户的延迟都拖下水。一个吵闹的调用方 1 秒打 100 次 `/v1/chat/completions`,所有其它租户的体验都会跟着劣化。

配额说"这个用户能花多少";我们还得说"这个用户能以**这个速率**花"。

## 方案

在 handler 之前放一份**按用户的令牌桶**(token bucket:桶里装令牌,按速率补充,按请求消耗)。每个用户有 `capacity` 个令牌,按 `refill_per_sec` 速率补充。每条请求消耗一枚令牌;令牌耗尽的话,这次请求会在任何配额扣减和上游调用之前,以 `429 Too Many Requests` 拒掉。

默认值是 60 令牌突发、每秒 1 令牌的补充——一个新用户能突发打满 60 条,之后稳定在约 1 req/s。这套默认值放在那里主要为了让"普通用户"和"故意刷接口的用户"看起来不一样,后者会立刻撞到 429。

## 工作原理

`s08_rate_limiting/bucket.py` 暴露令牌桶:

- `reset_buckets()` —— 清空所有桶(测试用)
- `configure(user_id, capacity, refill_per_sec)` —— 设置用户级限制并把桶填满
- `take(user_id, cost=1.0) -> bool` —— 原子:按流逝时间补充、对 `cost` 做检查、成功就扣减,耗尽返 `False`

整个检查-扣减在同一个 `threading.Lock` 下,所以同一用户的并发请求不可能花超。

`s08_rate_limiting/code.py` 把桶接进 handler:

```python
@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, p: Principal = Depends(require_api_key)):
    if not take(p.user_id):
        raise HTTPException(status_code=429, detail="rate limited")
    # ... 预扣估算、调上游、结算,和 s07 一致 ...
```

> Note: fastapi 的依赖注入有两种写法:typed-parameter(`p: Principal = Depends(require_api_key)`)和 `dependencies=[Depends(require_api_key)]` 列表。`s05` 当时用的是后一种写法——只在路由层跑依赖,不会把 `Principal` 注入 handler 签名,handler 只能去碰 `request.state.principal`。本章节用 typed-parameter,详见 s_full 的 "request.state.principal 的陷阱" 一节。

**检查顺序很重要**:鉴权 → 限速 → 配额扣减 → 调上游。没有合法 key 的用户根本到不了桶这步(401);令牌充足但没有配额的用户拿到 `402`,而不是 `429`。倒过来排会浪费上游调用、把没有 key 的探测请求也算进限速统计。

被 429 时响应里没有 `Retry-After`(限速响应头:告诉客户端何时可以重试)头——本章保持响应 body 极简,生产实现会加这个让礼貌客户端知道何时退避。

## 运行

```python
from s05_api_key_auth.storage import register_key
from s07_pre_consume_settle.quota import set_balance
from s08_rate_limiting.bucket import configure

register_key("u1", "sk-u")
set_balance("u1", 10_000_000)
configure("u1", capacity=60, refill_per_sec=1.0)  # 默认值
```

```bash
python s08_rate_limiting/code.py
```

```bash
# 突发 3 条 —— 当桶只有 2 枚时,第 3 条会拿到 429
for i in 1 2 3; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8008/v1/chat/completions \
    -H 'authorization: Bearer sk-u' \
    -H 'content-type: application/json' \
    -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
done
```

`PORT`(默认 8008)和 `RATE_PER_TOKEN`(默认 1)都可由环境变量覆盖。

## → new-api 源码

- `middleware/RateLimit.go` —— 用 Redis 计数器做按用户限速的中间件。这里我们把它内联成一个函数调用,契约更直观。

## 取舍

- **进程内桶是单进程的**。每个 worker 有自己的计数器,所以多 worker 部署下用户实际能拿到 `N_workers × capacity` 的突发。真实部署要把桶挪到 Redis,用 `INCR` + `EXPIRE` 让所有 worker 共享状态——但那是后续章节重写的事,本章是单进程 in-memory。
- **默认限制是全局的**。生产从数据库读每用户限制(tier、channel、plan)。本章把 60/1 写死——重点是算法,不是策略。
- **没有 `Retry-After` 头**。真实实现会加这个让礼貌的客户端知道何时退避;我们保持响应 body 极简。
- **按用户,不是按 token**。限速作用于 API key 持有者,而不是按上游模型分。多租户的模型级配额会把桶再按 `(user_id, model)` 拆。

## 下章预告

s08 之前所有用户都是匿名的"key 持有者"。s09 把用户变成有注册、有密码、有 JWT 的真身份。