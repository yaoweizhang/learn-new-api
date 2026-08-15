# s08: 按用户的令牌桶限速

> Previous: [s07](../s07_pre_consume_settle/) · Next: [s09](../s09_user_system/)

> *"桶里取 token"* —— rate limit 就是漏桶原理。

> **Layer**：L3 计量与扣费

## 问题

s07 的配额控制的是**花费**——一个余额很足的用户仍可能把上游打爆，把同一代理上其它所有租户的延迟都拖下水。一个吵闹的调用方 1 秒打 100 次 `/v1/chat/completions`，所有其它租户的体验都会跟着劣化。

配额说"这个用户能花多少"；我们还得说"这个用户能以**这个速率**花"。

## 方案

在 handler 之前放一份**按用户的令牌桶**（token bucket：桶里装令牌，按速率补充，按请求消耗）。每个用户有 `capacity` 个令牌，按 `refill_per_sec` 速率补充。每条请求消耗一枚令牌；令牌耗尽的话，这次请求会在任何配额扣减和上游调用之前，以 `429 Too Many Requests` 拒掉。

默认值是 60 令牌突发、每秒 1 令牌的补充——一个新用户能突发打满 60 条，之后稳定在约 1 req/s。这套默认值放在那里主要为了让"普通用户"和"故意刷接口的用户"看起来不一样，后者会立刻撞到 429。

## 工作原理

`s08_rate_limiting/bucket.py` 暴露令牌桶：

- `reset_buckets()` —— 清空所有桶（测试用）
- `configure(user_id, capacity, refill_per_sec)` —— 设置用户级限制并把桶填满
- `take(user_id, cost=1.0) -> bool` —— 原子：按流逝时间补充、对 `cost` 做检查、成功就扣减，耗尽返 `False`

整个检查-扣减在同一个 `threading.Lock` 下，所以同一用户的并发请求不可能花超。

`s08_rate_limiting/code.py` 把桶接进 handler：

```python
@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, p: Principal = Depends(require_api_key)):
    if not take(p.user_id):
        raise HTTPException(status_code=429, detail="rate limited")
    # ... 预扣估算、调上游、结算，和 s07 一致 ...
```

> Note: fastapi 的依赖注入有两种写法：typed-parameter（`p: Principal = Depends(require_api_key)`）和 `dependencies=[Depends(require_api_key)]` 列表。`s05` 当时用的是后一种写法——只在路由层跑依赖，不会把 `Principal` 注入 handler 签名，handler 只能去碰 `request.state.principal`。本章节用 typed-parameter，详见 s_full 的 "request.state.principal 的陷阱" 一节。

**检查顺序很重要**：鉴权 → 限速 → 配额扣减 → 调上游。没有合法 key 的用户根本到不了桶这步（401）；令牌充足但没有配额的用户拿到 `402`，而不是 `429`。倒过来排会浪费上游调用、把没有 key 的探测请求也算进限速统计。

## 运行

```python
from s05_api_key_auth.storage import register_key
from s07_pre_consume_settle.quota import set_balance
from s08_rate_limiting.bucket import configure

register_key("sk-u", "u1")
set_balance("u1", 10_000_000)
configure("u1", capacity=60, refill_per_sec=1.0)  # 默认值
```

```bash
python s08_rate_limiting/code.py
```

```bash
# 突发 3 条 —— 当桶只有 2 枚时，第 3 条会拿到 429
for i in 1 2 3; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8008/v1/chat/completions \
    -H 'authorization: Bearer sk-u' \
    -H 'content-type: application/json' \
    -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
done
```

`PORT`（默认 8008）和 `RATE_PER_TOKEN`（默认 1）都可由环境变量覆盖。

## 测试

```bash
pytest tests/test_s08_rate_limiting.py -v
```

一个测试覆盖契约：

| 测试 | 断言 |
| --- | --- |
| `test_first_two_pass_third_blocked` | 配置 `capacity=2, refill_per_sec=0` 时，前两条返回 200、第三条返回 429。 |

## → new-api 源码

- `middleware/RateLimit.go` —— 用 Redis 计数器做按用户限速的中间件。这里我们把它内联成一个函数调用，契约更直观。

## 取舍

- **进程内桶是单进程的**。每个 worker 有自己的计数器，所以多 worker 部署下用户实际能拿到 `N_workers × capacity` 的突发。真实部署要把桶挪到 Redis，用 `INCR` + `EXPIRE` 让所有 worker 共享状态——但那是后续章节重写的事，本章是单进程 in-memory。
- **默认限制是全局的**。生产从数据库读每用户限制（tier、channel、plan）。本章把 60/1 写死——重点是算法，不是策略。
- **没有 `Retry-After` 头**。真实实现会加这个让礼貌的客户端知道何时退避；我们保持响应 body 极简。
- **按用户，不是按 token**。限速作用于 API key 持有者，而不是按上游模型分。多租户的模型级配额会把桶再按 `(user_id, model)` 拆。