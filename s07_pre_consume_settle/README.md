# s07: 预扣与结算 — 多扣一点,结账退给你

> Previous: [s06](../s06_token_counting/) · Next: [s08](../s08_rate_limiting/)

> *"预扣保守一点,结算双向找齐"* —— 多扣一点不怕,真账目按上游报的来。

> **Layer**：L3 计量与扣费

## 问题

一个拥有 100 token 配额的用户,可以同时提交 100 个并行请求,每个 100 token。如果每条请求在调上游之前都按 100 token 做估算,这 100 条全部会通过配额闸门——然后真正打完上游才知道真实花费。结果是:用户可能在请求过程中**变负**,下一条本来余额充足的合法请求却因为配额已经下溢而被拒绝。

这就是 `spend-after`(调完再扣的反模式,对比"调前预扣")模式的天然漏洞:闸门看到的估算和实际账单是两件事,中间这段窗口越长、并发越多,漏洞越大。

## 本章要做什么

现在场景是:一个拥有 100 token 配额的用户,可以同时提交 100 个并行请求,每个 100 token。如果每条请求在调上游之前都按 100 token 做估算,这 100 条全部会通过配额闸门——然后真正打完上游才知道真实花费。这就是 `spend-after`(调完再扣的反模式,对比"调前预扣")模式的天然漏洞。要解决这个——**我们改用预扣+结算两阶段**:调上游前按 s06 的估算值乘单价先预扣一个偏宽的额度(不够直接 `402 Payment Required` 不打上游),上游回包后用真实 `usage` 跟预扣值做差额结算——少用退、超用补、整笔失败原样退。本章就把这两刀写出来:

1. **写一个 `quota` 模块 —— 为什么要有自己的模块而不是直接扣字典**:`reset()` / `set_balance(uid, n)` / `get_balance(uid)` / `deduct(uid, n) -> bool`(原子条件扣减,余额不足返 `False` 不部分扣)/ `refund(uid, n)` / `settle(uid, pre, actual)`(退还或补差额,原子返回实扣)。**为什么扣减必须在 `threading.Lock` 下原子**:同用户并发请求不能"双花",先读再写会被两个 in-flight 请求同时穿过;**为什么用 `threading.Lock` 而不是 `asyncio.Lock`**:配额算式是纳秒级,加锁开销可忽略,`asyncio.Lock` 在每个 `await` 点要让出,划不来。
2. **handler 调前算 estimate + 预扣 —— 为什么用偏宽的估算**:`estimate = (prompt_tokens + expected_completion) * RATE_PER_TOKEN`,`expected_completion` 走 `req.max_tokens or 256`。**为什么没 `max_tokens` 时按 256**:大多数回复比 256 短,所以用户常常收到小笔退款——偏宽是为了闸门能在请求飞行前拦住明显不够余额的用户,宁多勿少;**为什么不调用上游真实 `/count_tokens` 接口**:本章要的是估算不是精确,多一次远端调用就把"预扣"这件事本身变成昂贵的——等到 s_full 走精确路径。
3. **handler 调后算 actual + 结算 —— 为什么差额要双向找齐**:成功路径 `actual = (max(上游 pt, 本地 pt) + ct) * RATE`,`settle(uid, estimate, actual)` 内部 `diff = actual - estimate`,`diff>0` 补差额(余额不够 deduct 静默失败——生产是 billing 异常)、`diff<0` 退差额。**为什么 `max(上游 pt, 本地 pt)`**:上游 tokenizer 和本地略有差异,取较大值保证本地账目不被悄悄少扣;**为什么 `ct` 缺失时回退到 `max(1, len(reply)//4)` 而不是 0**:`pt` 是输入我们没法本地算,`ct` 是输出我们已经拿到内容,本地估一下总比 0 准。
4. **失败路径整笔 refund —— 为什么不能"扣就扣了"**:网络错(`httpx.HTTPError`)、`r.status_code >= 400`、模型名错(`pick_provider` 抛 `ValueError`)——任一失败都把整份预扣 `refund` 回去。**为什么失败也走 refund**:用户没有拿到任何有效回复,按 token 收钱没道理;**为什么不是事后异步对账**:对账窗口越大,并发漏洞越大——同步 refund 让用户余额状态始终和"成功收到的回复数"对得上。

成品:`curl -X POST .../v1/chat/completions` 成功时响应里带 `"usage"` + `"quota_charged": N`;调一次 `GET /quota/u1` 看到余额减少了一笔实际花费(可能比预扣少,差额已退);余额为 0 时直接 `402 insufficient quota`,上游一次都不打。后续 s08 在闸门后接按用户的限速,s09 把 `_balances` 持久化进 SQLite 走事务化扣减。

## 方案

调一次模型要花多少钱,没法事先知道——上游报多少就是多少。但用户余额不能等调用完才扣,万一扣不下呢?所以本章引入的**预扣 / 结算**(**预扣**(pre-consume,调上游前先按估算扣一个偏宽额度)、**结算**(settle,上游回包后用真实 usage 跟预扣值做差额——少用退、超用补))就是这条"调前估 + 调后对账"两步路线的统称。本章套路是:

1. **调之前先预扣一个估计值**:用 s06 算出的 `prompt_tokens + expected_completion`,乘以 `RATE_PER_TOKEN`。余额不够直接 `402 Payment Required`,根本不打上游。
2. **调上游**。
3. **成功的情况下结算**:真实用量比估计少就退差额(`refund`),多了就补差额(再 `deduct`)——`settle(user, pre_deducted, actual)` 把这两步合在一个原子操作里。
4. **上游失败的情况下**:网络错、4xx、5xx 都算失败,把整份预扣原样退回。用户不应该为一笔没成功的请求付费。

扣减在 `threading.Lock` 下原子进行,所以同一用户的并发请求不可能"双花"。注意:配额算式是纳秒级,加锁开销可以忽略;用 `asyncio.Lock` 反而要在每个 await 点让出,划不来。

`## 问题` 提了 1 件痛:`spend-after` 模式闸门看到的是估算、真实账单要等调用完,这中间窗口里并发请求可能让用户变负、失败调用也可能被错误扣费。这件事**没法靠"客户端自觉"或"事后对账"能解决**——必须由网关在转发前先扣一个偏宽的额度、转发后再算差额。下面这幅图把这件事各放到四个角色里:

- **`Client` (调用方)** —— 在装 s07 之前,这是"打完了才扣钱、扣不下时一脸懵"的角色;装上之后,这事被中继解了——Client 只管发请求,余额不够直接 `402` 打回来,失败调用自动原样退回。
- **`Relay` (本章要写的预扣 + 调后结算)** —— 把痛的解决动作集中放在这里:handler 调 `quota.deduct(uid, estimate)` 预扣 → 调上游 → 成功路径用 `quota.settle(uid, pre, actual)` 退/补差额(少用退、超用补),失败路径用 `quota.refund(uid, estimate)` 整笔退回。整段在 `threading.Lock` 下原子。
- **`Quota` (内存 dict + Lock,`quota.py`)** —— 本章新引入的进程内存储。`_balances: dict[uid → int]`,所有 `deduct` / `refund` / `settle` 操作在 `threading.Lock` 下原子进行——同用户并发请求不能"双花"。进程一重启配额清零,s09 接 SQLite 持久化。
- **`Upstream` (LLM 厂商)** —— 服务提供方。它在响应里带 `usage` 就如实回,中继按 `max(上游 pt, 本地 pt) + ct` 算出真实花费做结算;网络错或 4xx/5xx 中继视为失败、整笔退回。

## 工作原理

**原理**: 一个 chat 请求从客户端进来, 它的生命周期是: handler 调 `s06.count_prompt` 拿到 `prompt_tokens` → 算 `estimate = (pt + expected_completion) × RATE_PER_TOKEN` → 调 `quota.deduct(uid, estimate)` 预扣(余额不够直接 `402`)→ 转发到上游 → 拿到响应时用 `quota.settle(uid, pre, actual)` 退/补差额(成功)或 `quota.refund(uid, estimate)` 整笔退回(失败)。整章所有部件都为这条主线服务。

**1. 一个 pre-deduct 估算 (`code.py` handler 内的 `estimate = (pt + expected_completion) × RATE`)** —— `expected_completion` 走 `req.max_tokens or 256`(没传 `max_tokens` 时按 256 兜底,大多回复更短所以用户常收到小笔退款)。偏宽是为了闸门能在请求飞行前拦住明显不够余额的用户,宁多勿少。

**2. 一个 quota storage (`quota.py`,进程内 `dict` + `threading.Lock`)** —— `_balances: dict[uid → int]` 存余额;`deduct(uid, n) → bool`(原子条件扣减,余额不足返 `False` 不部分扣)/ `refund(uid, n)` / `settle(uid, pre, actual) → int`(退/补差额,返回实扣)。所有操作在 `threading.Lock` 下原子——同用户并发请求不能"双花"。

**3. 一个 forwarder (`code.py` 内的 `httpx.AsyncClient.post(...)`)** —— 跟 s04 一样的转发循环;预扣不通过则直接 `raise HTTPException(402)` 不打上游,成功回包时取 `usage` 字段做结算。

**4. 一个 settle / refund 结算 (`code.py` handler 调后逻辑)** —— 成功路径 `actual = (max(上游 pt, 本地 pt) + ct) × RATE`,`settle(uid, pre, actual)` 在 Lock 下算 `diff = actual - pre`——`diff > 0` 补差额,`diff < 0` 退差额;失败路径(网络错 / 4xx / 5xx / `pick_provider` 抛错)整笔 `refund(uid, estimate)`——用户不为失败调用付费。

下面这块算式把"预扣金额"算出来——给读者一个一眼能看懂的公式:`prompt + 估计的 completion` 乘以 `token 单价` 就是预扣值;下单与结算都围绕这个 `estimate` 转。

配额算式:

```
RATE = 1 quota per token(可配;本章按平价)
estimate = (prompt_tokens + expected_completion) * RATE_PER_TOKEN
```

`s07_pre_consume_settle/quota.py` 暴露存储:

- `reset()` —— 清空所有余额(测试用)
- `set_balance(user_id, amount)` —— 设置一个用户的余额
- `get_balance(user_id) -> int` —— 读余额
- `deduct(user_id, amount) -> bool` —— 原子条件扣减;余额不足返 `False`(不部分扣减)
- `refund(user_id, amount)` —— 把配额加回去(失败补偿用)
- `settle(user_id, pre_deducted, actual) -> int` —— 退还差额,返回实际被扣金额

`s07_pre_consume_settle/code.py` 把这套算式接进 FastAPI handler:

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
pt = max(usage.get("prompt_tokens", 0), prompt_tokens)
ct = usage.get("completion_tokens", max(1, len(translated["choices"][0]["message"]["content"]) // 4))
actual = (pt + ct) * RATE_PER_TOKEN
settle(principal.user_id, estimate, actual)
```

`max(usage.prompt_tokens, prompt_tokens)` 是为了应对上游 tokenizer 和本地 tokenizer 略有差异的情况——取较大值保证不会因为估算偏小而出现"调用已经花掉 X 配额、但我们只补了 X-1"的账目缺口。

Asymmetry note: `pt` falls back to 0 if upstream omits it, but `ct` falls back to a char/4 estimate (`max(1, len(content) // 4)`). Reason: tokenizers differ; `ct` is the *output* we already have, so we can estimate locally; `pt` is the *input* which we cannot recover locally if upstream omits it.

**注意**:s07 这里的 `max(...)` 在 s_full 的 `services/billing.py` 替换为"pt/ct 任一缺失则保留 pre_deducted"。原因是 pre-consume 已经 floor 在 estimate 上,再 max 会让用户永远按 estimate 付费,掩盖超额路径;s_full 选择显式承担"pt/ct 缺失 → 不退款"的语义。

## 运行

```python
from s05_api_key_auth.storage import register_key
from s07_pre_consume_settle.quota import set_balance

register_key("u1", "sk-u")
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

确认预扣接口能响应 + 余额查得到?打这套 curl——`/v1/chat/completions` 回 `200 + usage + quota_charged` 说明 `deduct` 在闸门后跑了、`settle` 在调后跑了;紧接着 `GET /quota/u1` 余额从 `10000` 掉到约 `9970`(差额已退)说明 `quota.py` 的 `_balances` dict 和 `threading.Lock` 都在响应,预扣+结算整条链在跑:

`RATE_PER_TOKEN`(默认 1)和 `PORT`(默认 8007)都可以通过环境变量覆盖。

## → new-api 源码

- `service/billing.go` —— 预扣 / 结算逻辑(`PreConsumeQuota`、`SettleQuota`、`RefundQuota` 等函数)。
- `service/quota.go` —— 配额计算 + quota Lua 脚本封装(单笔扣减用 Redis 原子操作保证一致性)。

## 本章不做什么

- **没有按 channel 的 rate card** (不同上游单价不同:OpenAI / Claude / Gemini 单 token 价格)——本章统一 `RATE_PER_TOKEN = 1`,所有模型按 token 算 1 quota。→ s_full 接上游定价表,按 `(uid, model, channel)` 查单价。
- **没有按用户的速率限速** (限制用户每秒能发几次,而非能花多少钱)——本章只算"花多少",不算"打多快"。一个余额充足的用户仍可 1 秒打 100 次。→ s08 接令牌桶按用户限速。
- **没有幂等键** (重复请求去重:客户端带一个唯一 key,服务端对同一 key 不重复扣费)——一次超时重试的客户端可能被双扣(扣两次、结算一次)。→ s10 加 `Idempotency-Key`。
- **没有失败调用的告警 / metric** (Prometheus 计数器 / 失败率告警)——失败 `refund` 是同步的、可见的,但不暴露任何指标。→ s11 加日志 + s16 加指标。

## 已知限制

- **进程内存储** (`_balances` 是进程内 `dict`,内存对象)——状态随进程消亡;s09 引入 SQLite,用事务化扣减。
- **没有配额刷新 / 充值** (定时按量补配额 / 用户主动充值)——配额在 dict 里一直活到服务重启;生产走 Redis + 周期补量的 cron。
- **`threading.Lock` 不跨 worker** (单进程锁,多 worker 进程下不互斥)——`asyncio` 单 worker 部署够用,但多 worker 时每个 worker 各持一份 `_balances`,同 user 并发请求可能"双花";上 Redis 共享余额 + 原子 Lua 脚本是后续优化项。
- **`expected_completion = 256` 偏宽** (回复 token 数估算固定 256 个)——当 `max_tokens` 没传时按 256 token 预扣。大多回复更短,所以用户会经常收到小笔退款。一旦频繁触顶,后续章节切到按 channel 的 rate card。
- **`settle` 时余额可能不够补差额** (`diff > 0` 时 deduct 静默失败)——`max(上游, 本地)` 兜底或上游 `usage` 异常时,真实花费可能超过 estimate,余额补不上时 `deduct` 静默返 `False`,用户少付一笔——生产是 billing 异常,需告警。

## 设计选择

- **`threading.Lock` 而不是 `asyncio.Lock`** (同步锁 / vs 异步锁)——配额算式是纳秒级,加锁开销可忽略,`asyncio.Lock` 在每个 `await` 点要让出,划不来。代价是多 worker 时锁不共享,生产上 Redis + Lua 原子脚本更合适。
- **预扣偏宽 (256 默认)而不是精确估算** (调上游 `/count_tokens`)——预扣的目的是"闸门在请求飞行前能拦住明显不够余额的用户",多扣一点不怕、用户会收到小笔退款;不调 `/count_tokens` 是为了不把预扣本身变成昂贵的远端调用。
- **失败整笔 refund 而不是部分退** (网络错 / 4xx / 5xx / `pick_provider` 抛错全部退 estimate)——用户没拿到有效回复,按 token 收钱没道理;同步 refund 让用户余额状态始终和"成功收到的回复数"对得上,不留并发漏洞窗口。
- **`max(上游 pt, 本地 pt)` 兜底 prompt_tokens** ——上游 tokenizer 和本地略有差异,取较大值保证本地账目不被悄悄少扣;代价是用户偶尔多付一点 token,但账单永远不漏。

## 下章预告

s07 防住"算钱",但用户一秒可以打 100 次,公共资源被一个人吃完。s08 套令牌桶,一个用户用超就 429。