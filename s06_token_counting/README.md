# s06: Token 计数 — 数清每个请求的 token,才知道这一单该收多少钱

> Previous: [s05](../s05_api_key_auth/) · Next: [s07](../s07_pre_consume_settle/)

> *"token 不是字符,也不是 word"* —— 用 token 算账,按 token 计费。

> **Layer**：L3 计量与扣费

## 问题

`s05` 把请求转出去、原样把上游给回来的 `usage` 透传——这只有在模型"已经做完活"之后才正确。我们想要的是:

1. **在调用离开我们边缘之前就报个价**。按 token 计费要求的是请求前(或者同一份响应里)就拿到 token 数,而不是下次对账时拿。
2. **统一 usage 形态**,即便上游不返回也撑得住(老版本 Claude/Gemini 响应、被 mock 的上游、部分失败)。
3. **估算要够准,可以合理计费**,同时又不必为了一次分词再付一次远端调用。

没有它的话,我们要么多扣(按最坏情况估)、要么少扣(干脆忘了数)——响应一旦发出,就都救不回来了。

## 本章要做什么

现在场景是:`s05` 把请求转出去、原样把上游给回来的 `usage` 透传——这只有在模型"已经做完活"之后才正确。我们想要的是:在调用离开我们边缘之前就报个价;统一 usage 形态;估算要够准,可以合理计费。要解决这个——**我们在转发前先把 prompt 的 token 数清楚**:OpenAI 模型走 `tiktoken`(`cl100k_base` 编码)按 BPE 数,其它厂商没有官方分词器就用 `字符数 / 4` 的经验估算兜底;上游回包时如果带了完整 `usage` 就用它,没带就用本地估算 + 回复长度合成。本章就把这条数 token 的链路写出来:

1. **写一个 `tokenizer` 模块 —— 为什么要在转发前数 prompt token**:`count_prompt(messages, model)` 按模型名前缀分派:OpenAI 走 `count_openai`(每条消息加 4 token overhead + `cl100k_base` 编码 `content`,再给回复预热 2),非 OpenAI 走 `count_estimate`(`sum(len(content)) // 4`,至少 1)。**为什么必须在转发前就数清楚**:后续 s07 要按"预估 token 数 × 单价"预扣,没这个数字根本没法预扣;**为什么不等到上游回报再算**:那时候已经花了上游配额,本地的账和上游的账对不齐,账单/限额逻辑无法在请求飞行前做出决策。
2. **在 `chat_completions` handler 里数 token —— 为什么 handler 自己调不算中间件**:每条进来的请求 `count_prompt` 一次,把 `prompt_tokens` 留下来给响应阶段用;**为什么不用全局中间件**:token 数和 `model` 字段绑定,要从 `messages` 里读,而 Pydantic 校验完的请求体才是干净形态——全局中间件在 Pydantic 之前跑,要么复读 `body` 一遍,要么拿不到 `model` 字段。
3. **合并上游 `usage` —— 为什么两条路径都要保底**:上游给了完整 `usage` 就用,但 `prompt_tokens` 取 `max(上游值, 本地预计)`——**为什么取较大值**:`max(...)` 挡住"上游 tokenizer 估得比本地少"的边界情况,保证本地账目不被悄悄少扣;**为什么非 OpenAI 路径要走 fallback**:老版本 Claude/Gemini、被 mock 的上游、部分失败都会让 `usage` 缺失,这种时候用 `prompt_tokens`(本地估算)+ `max(1, len(reply) // 4)`(回复长度估)合成一份 `usage`,让客户端永远能读到 `usage.total_tokens`。
4. **挂回响应 + 报 prompt+completion 字段 —— 为什么补齐 `usage` 是契约的一部分**:OpenAI 客户端拿到响应后会读 `usage` 字段做自己的统计/限速,缺失就要客户端自己想办法——**为什么坚持三字段都给**:`prompt_tokens` / `completion_tokens` / `total_tokens` 是 OpenAI SDK 期待的形态;补不齐就破坏客户端零修改的承诺(s02 立的)。

成品:`curl -X POST .../v1/chat/completions` 收到响应里有 `"usage": {"prompt_tokens": N, "completion_tokens": M, "total_tokens": N+M}`,OpenAI 路径走 `tiktoken` 准数,Claude/Gemini 走 `char/4` 兜底。后续 s07 在这条数 token 的链路上接预扣+结算,s08 在闸门后接按用户限速,这一层定型后整条链路就知道"每一笔该花多少钱"。

> **tiktoken**(OpenAI 开源的 tokenizer 库,按 BPE 规则把文本切成 token 并计费)—— 后续章节直接复用,不再重复解释。

## 方案

现在的场景是:`## 问题` 提了三件痛——调用前不知道这一笔要花多少(痛点 #1)、上游 `usage` 缺失就拿不到账单(痛点 #2)、估算不够准就不能合理计费(痛点 #3)——这三件事**任何一件**客户端自己估都搞不定、运维后对账也搞不定,必须由网关在转发前先数 token、转发后再合并 upstream usage。

**要解决这个——我们在网关里引入一个 `tokenizer` 模块,按模型名前缀选估算器**——OpenAI 模型走 `tiktoken`(`cl100k_base` 是 OpenAI gpt-4*/gpt-3.5-turbo 用的 BPE 编码),其它模型走 `len(content) // 4` 的经验估算(故意粗糙——对账单估算够用,精确计费等上游 `/count_tokens`):

| 模型名前缀 | 策略 | 出处 |
|---|---|---|
| `gpt-`/`o` | `tiktoken`(`cl100k_base`) | `s06_token_counting/tokenizer.py:count_openai` |
| 其它 | `len(content) // 4` | `s06_token_counting/tokenizer.py:count_estimate` |

`s06_token_counting/code.py:chat_completions` 在转发前先数 prompt token,等到上游回复时按上游给没给完整 `usage` 分两条路:

- 如果上游给了完整的 `usage`(`total_tokens > 0`),就保留它给的
  `prompt_tokens`,但用 `max(上游值, 我们的预计数)` 做兜底。这一
  步专门挡住"静默少算"的边界情况。
- 否则就用 `prompt_tokens` 估算值 + `len(reply) // 4`(对完成
  token)合成一份 `usage`。

下面这幅图把上面三件痛点各放到一个角色里:

- **`Client` (调用方)** —— 在装 s06 之前,这是"只管发请求、账单等回包再说"的角色;装上之后,这事被中继解了——Client 只管发 `prompt`,token 数和 `usage` 都由中继填好回吐。
- **`Relay` (本章要写的数 token + 合并 usage)** —— 把痛点 #1 #2 #3 的解决动作集中放在这里:handler 调 `tokenizer.count_prompt(messages, model)` 拿到 `prompt_tokens`(OpenAI 走 `tiktoken`,其它走 `char/4`);转发上游后,如果回了完整 `usage` 就用它,缺失就用本地估算值 + 回复长度合成一份。Client 看不见上游报了多少 token,Upstream 看不见本地估了多少。
- **`Upstream` (LLM 厂商)** —— 服务提供方。它在响应里带 `usage` 字段就如实回,缺失也无所谓——中继会用 `max(上游, 本地)` 兜底,账单永远算得清。

下面这张 ASCII 流程图把"先计数再转发"画出来,和下面那张架构图相对照——上面这张是单跳时序,下面那张是角色拓扑,中间那块都是 s06 中继(预热计数 + 合并 upstream usage):

```
Client ──POST──▶ s06 ──count prompt──▶ Upstream ──reply──▶ merge usage ──▶ Client
                                  └── 非 OpenAI 走 char/4 兜底
```

下面这张架构图给读者一幅全局鸟瞰——图里仍是 `Client / s06 / Upstream` 三个角色,箭头方向 = 请求/响应走向(`▶` 是请求,`◀` 是 JSON 响应),中间那一块 `s06` 中继就是本章要写的——预热计数 + 合并上游 usage:

![architecture](images/architecture.svg)

## 工作原理

**原理**: 一个 chat 请求从客户端进来, 它的生命周期是: 处理器调 `tokenizer.count_prompt(messages, model)` 在转发前先算 prompt token 数 → 用 Pydantic 校验过的 `messages` 算, 不读原始 body → 转发到上游 → 拿到响应里的 `usage` 字段 → 用 `max(上游, 本地)` 做兜底补齐 `prompt_tokens` → 把完整 `usage` 回吐客户端。所有部件都围着这条主线展开。

**1. 一个 tiktoken encoder (`tokenizer.count_openai`,`cl100k_base` 编码)** —— OpenAI 官方分词器,按 BPE 把文本切成 token。`count_openai` 对每条消息加 4 token overhead(角色 + 分隔符),再给回复预热加 2。`gpt-*` / `o*` 前缀的模型走这条路径。

**2. 一个 count_prompt dispatcher (`tokenizer.count_prompt`)** —— 按模型名前缀分派:OpenAI 走 `count_openai`,其它走 `count_estimate`(`sum(len(content)) // 4`,至少 1)。handler 在转发前调它,把 `prompt_tokens` 留给响应阶段合并用。

**3. 一个 upstream usage merger (handler 内的合并逻辑)** —— 上游回了完整 `usage`(`total_tokens > 0`)就用它,缺失就用本地估算值 + `len(reply) // 4`(对完成 token)合成一份。`prompt_tokens` 取 `max(上游, 本地)` 兜底——挡住"上游 tokenizer 估得比本地少"的边界。

**4. 一个 char/4 fallback (`tokenizer.count_estimate`)** —— 非 OpenAI 模型没有官方分词器,业内通用 `1 token ≈ 4 chars` 经验估算,故意粗糙——够给后续章节(s07 预扣)做软配额,精确计费等上游 `/count_tokens`。

`tiktoken.get_encoding("cl100k_base")` 给我们的是 OpenAI 对 `gpt-4*`
和 `gpt-3.5-turbo` 用的那套 BPE 编码器。根据 OpenAI cookbook,每条
聊天消息带 ~4 个 token 的 overhead(角色标识 + 分隔符),我们再给回复
预热加 2 个。

```python
# s06_token_counting/tokenizer.py
def count_openai(messages, model):
    n = 0
    for m in messages:
        n += 4
        content = m.get("content") or ""
        n += len(_OPENAI_ENCODER.encode(content))
    n += 2  # 回复预热
    return n
```

非 OpenAI 模型没有官方分词器,所以我们退回业内常用的 1 token ~ 4 字符的经验估算。故意粗糙——对账单估算已经够用,等拿到准确计数再替换掉(后续 `s07_pre_consume_settle` 会用真实计数)。

调度器:

```python
def count_prompt(messages, model):
    if model.startswith(("gpt-", "o")):
        return count_openai(messages, model)
    return count_estimate(messages)
```

## 运行

```bash
cd s06_token_counting
python -c "from s05_api_key_auth.storage import register_key; register_key('u1','sk-tok')"
PORT=8006 python code.py
```

在另一个 shell:

```bash
curl -X POST http://localhost:8006/v1/chat/completions \
  -H 'authorization: Bearer sk-tok' \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

确认 tiktoken 装好了 + 数 token 接口能响应?打上面这条 curl——回包里看到 `"usage": {"prompt_tokens": N, "completion_tokens": M, "total_tokens": N+M}` 三字段齐全,说明 `cl100k_base` 编码器已加载到内存、handler 的 `count_prompt` + 合并 upstream usage 整条链都在跑:

响应里现在带着:

```json
{
  "usage": {
    "prompt_tokens": 6,
    "completion_tokens": 3,
    "total_tokens": 9
  }
}
```

## → new-api 源码

- `service/token_counter.go` —— 真实实现。按厂商分派(OpenAI 分词
  器、Claude 启发式、Gemini 启发式),缓存每条消息的计数,然后把结
  果交给计费层(`service/billing.go`)。

## 本章不做什么

- **没有流式 token 计数** (逐 token 推送响应 `s03_streaming_sse`)——计数在请求阶段算;流式响应要把 token 数到 SSE chunk 落地的那一刻再算。→ s_full 接上游 usage 流式回调,在 SSE 末尾对齐总数。
- **没有按 model 的精确分词器** (除 OpenAI 之外的官方 tokenizer)——Claude/Gemini 没有官方开源分词器,统一走 `char/4` 经验估算。→ 等上游提供 `/count_tokens` 端点再切。
- **没有按 role / tool overhead 的细分** (聊天消息格式开销:role 标签、工具定义等每条消息占的 token)——`count_openai` 写死每条消息 4 token,不分 role、不算 tool 定义。→ s_full 接 model-specific 的 `count_message_with_overhead`。

## 已知限制

- **char/4 比较粗糙** (经验估算公式,1 token ≈ 4 字符)——在英文上对 Claude/Gemini 准确率大约 ±20%——给软配额提示够用,精确计费则不行。生产路径应该在上游提供 `/count_tokens` 时调它。
- **overhead 是硬编码的** (每条聊天消息的固定 token 开销)——每条消息 4 token 这条规则来自 OpenAI cookbook;真实 overhead 随 role 和工具定义而变。一章内我们接受这点漂移,后面再按 model-specific 规则读。
- **`tiktoken` 首次加载慢** (下载 + 缓存 BPE 词表)——首次 `import tiktoken` 会触发 `cl100k_base` 词表下载,冷启动约 1-2s;生产路径通常把词表预打包进镜像。

## 设计选择

- **`max(上游 pt, 本地 pt)` 兜底** (取两边 prompt token 估算的较大值 / vs 取上游值)——上游 tokenizer 和本地略有差异,取较大值保证本地账目不被悄悄少扣;代价是用户偶尔会被多扣一点点 token,但账单永远不漏。
- **非 OpenAI 走 `char/4` 而非不数** ——给 s07 预扣提供一个"虽然粗糙但总比 0 强"的数字;精确计费交给上游 `/count_tokens` 兜底。
- **三字段 usage 始终填齐** (`prompt_tokens` / `completion_tokens` / `total_tokens` / vs 仅回上游给的)——OpenAI SDK 期待三字段都到位,缺一字段客户端就会自己再算。补齐是契约的一部分。

## 下章预告

s06 能算账,但还没人记账。s07 在调用前预扣,调用后退补,让用户不为失败调用付费。