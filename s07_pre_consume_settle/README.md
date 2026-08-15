# s07: 预扣与结算

> Previous: [s06](../s06_token_counting/) · Next: [s08](../s08_rate_limiting/)

> *"多扣一点，结账退给你"* —— 预扣保守一点，结算双向找齐。

> **Layer**：L3 计量与扣费

## 问题

一个拥有 100 token 配额的用户，可以同时提交 100 个并行请求，每个 100 token。如果每条请求在调上游之前都按 100 token 做估算，这 100 条全部会通过配额闸门——然后真正打完上游才知道真实花费。结果是：用户可能在请求过程中**变负**，下一条本来余额充足的合法请求却因为配额已经下溢而被拒绝。

这就是"spend-after"模式的天然漏洞：闸门看到的估算和实际账单是两件事，中间这段窗口越长、并发越多，漏洞越大。

## 方案

调一次模型要花多少钱，没法事先知道——上游报多少就是多少。但用户余额不能等调用完才扣，万一扣不下呢？所以套路是：

1. **调之前先预扣一个估计值**：用 s06 算出的 `prompt_tokens + expected_completion`，乘以 `RATE_PER_TOKEN`。余额不够直接 `402 Payment Required`，根本不打上游。
2. **调上游**。
3. **成功的情况下结算**：真实用量比估计少就退差额（`refund`），多了就补差额（再 `deduct`）——`settle(user, pre_deducted, actual)` 把这两步合在一个原子操作里。
4. **上游失败的情况下**：网络错、4xx、5xx 都算失败，把整份预扣原样退回。用户不应该为一笔没成功的请求付费。

扣减在 `threading.Lock` 下原子进行，所以同一用户的并发请求不可能"双花"。注意：配额算式是纳秒级，加锁开销可以忽略；用 `asyncio.Lock` 反而要在每个 await 点让出，划不来。

## 工作原理

配额算式：

```
RATE = 1 quota per token（可配；本章按平价）
estimate = (prompt_tokens + expected_completion) * RATE_PER_TOKEN
```

`s07_pre_consume_settle/quota.py` 暴露存储：

- `reset()` —— 清空所有余额（测试用）
- `set_balance(user_id, amount)` —— 设置一个用户的余额
- `get_balance(user_id) -> int` —— 读余额
- `deduct(user_id, amount) -> bool` —— 原子条件扣减；余额不足返 `False`（不部分扣减）
- `refund(user_id, amount)` —— 把配额加回去（失败补偿用）
- `settle(user_id, pre_deducted, actual) -> int` —— 退还差额，返回实际被扣金额

`s07_pre_consume_settle/code.py` 把这套算式接进 FastAPI handler：

```
estimate = (prompt_tokens + expected_completion) * RATE_PER_TOKEN
if not deduct(principal.user_id, estimate):
    raise HTTPException(402, "insufficient quota")

try:
    r = await client.post(upstream_url, ...)
except httpx.HTTPError:
    refund(principal.user_id, estimate)        # 网络失败
    raise HTTPException(502, "upstream error")

if r.status_code >= 400:
    refund(principal.user_id, estimate)        # 上游返回错误
    raise HTTPException(r.status_code, r.text)

# 成功路径 —— 退还差额
pt = max(usage.prompt_tokens, prompt_tokens)
ct = usage.completion_tokens
actual = (pt + ct) * RATE_PER_TOKEN
settle(principal.user_id, estimate, actual)
```

`max(usage.prompt_tokens, prompt_tokens)` 是为了应对上游 tokenizer 和本地 tokenizer 略有差异的情况——取较大值保证不会因为估算偏小而出现"调用已经花掉 X 配额、但我们只补了 X-1"的账目缺口。

## 运行

```python
from s05_api_key_auth.storage import register_key
from s07_pre_consume_settle.quota import set_balance

register_key("sk-u", "u1")
set_balance("u1", 10_000)
```

```bash
python s07_pre_consume_settle/code.py
```

```bash
curl http://localhost:8007/quota/u1                                  # {"balance": 10000}
curl -X POST http://localhost:8007/v1/chat/completions \
  -H 'authorization: Bearer sk-u' \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
curl http://localhost:8007/quota/u1                                  # {"balance": 9970 左右}
```

`RATE_PER_TOKEN`（默认 1）和 `PORT`（默认 8007）都可以通过环境变量覆盖。

## 测试

```bash
pytest tests/test_s07_pre_consume_settle.py -v
```

三个测试覆盖契约：

| 测试 | 断言 |
| --- | --- |
| `test_pre_consume_deducts_before_call` | 一笔成功的调用会从余额里扣一部分。 |
| `test_insufficient_quota_returns_402` | 一笔余额为 0 的用户会拿到 `402 Payment Required`。 |
| `test_upstream_failure_refunds_pre_consume` | 当上游返回 500 时，整份预扣要退回去。 |

## → new-api 源码

- `service/PreConsumeQuota.go` —— 预扣 / 结算逻辑。
- `model/Quota.go` —— Quota 结构 + 每个用户的计数器。

## 取舍

- **进程内存储**。状态随进程消亡；s09 引入 SQLite，用事务化扣减。
- **没有配额刷新 / 充值**。配额在 dict 里一直活到服务重启；生产走 Redis + 周期补量的 cron。
- **估算偏宽**。当 `max_tokens` 没传时，我们按 `expected_completion = 256` token 预扣。大多回复更短，所以用户会经常收到小笔退款。一旦频繁触顶，后续章节切到按 channel 的 rate card。
- **没有幂等键**。一次超时重试的客户端可能被双扣（扣两次、结算一次）。s10 加 `Idempotency-Key`。