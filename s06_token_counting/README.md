# s06: Token 计数

> Previous: [s05](../s05_api_key_auth/) · Next: [s07](../s07_pre_consume_settle/)

> *"tiktoken 数明白"* —— token 不是字符，也不是 word。

> **Layer**：L3 计量与扣费

**本章新增**:在请求飞行前就数 prompt token(OpenAI 走 tiktoken,
其它都按字符/4),并把结果挂到响应的 `usage` 上。现在我们在账单到达
用户之前,就知道每条请求花了多少 token。

> **tiktoken**（OpenAI 开源的 tokenizer 库，按 BPE 规则把文本切成 token 并计费）—— 后续章节直接复用，不再重复解释。

## 问题

`s05` 把请求转出去、原样把上游给回来的 `usage` 透传——这只有在模型"已经做完活"之后才正确。我们想要的是:

1. **在调用离开我们边缘之前就报个价**。按 token 计费要求的是请求前(或者同一份响应里)就拿到 token 数,而不是下次对账时拿。
2. **统一 usage 形态**,即便上游不返回也撑得住(老版本 Claude/Gemini 响应、被 mock 的上游、部分失败)。
3. **估算要够准,可以合理计费**,同时又不必为了一次分词再付一次远端调用。

没有它的话,我们要么多扣(按最坏情况估)、要么少扣(干脆忘了数)——响应一旦发出,就都救不回来了。

## 方案

一个 `tokenizer` 模块,按模型名前缀选估算器:

| 模型名前缀 | 策略 | 出处 |
|---|---|---|
| `gpt-`/`o` | `tiktoken`(`cl100k_base`) | `s06_token_counting/tokenizer.py:count_openai` (`cl100k_base` 是 OpenAI gpt-4*/gpt-3.5-turbo 用的 BPE 编码) |
| 其它 | `len(content) // 4` | `s06_token_counting/tokenizer.py:count_estimate` |

`s06_token_counting/code.py:chat_completions` 在转发前先数 prompt
token,等到上游回复时:

- 如果上游给了完整的 `usage`(`total_tokens > 0`),就保留它给的
  `prompt_tokens`,但用 `max(上游值, 我们的预计数)` 做兜底。这一
  步专门挡住"静默少算"的边界情况。
- 否则就用 `prompt_tokens` 估算值 + `len(reply) // 4`(对完成
  token)合成一份 `usage`。

下面这张 ASCII 流程图把"先计数再转发"画出来——图里有 `Client`、本章要写的 `s06` 中继、以及远端 `Upstream` 三个角色，箭头方向 = 请求/响应走向（`▶` 是请求，`◀` 是 JSON 响应），中间那一块 `s06` 是本章要写的：先 count prompt 再转发，并 merge upstream 的 usage 回给客户端。

```
Client ──POST──▶ s06 ──count prompt──▶ Upstream ──reply──▶ merge usage ──▶ Client
                                  └── 非 OpenAI 走 char/4 兜底
```

下面这张架构图给读者一幅全局鸟瞰：图里仍是 `Client / s06 / Upstream` 三个角色，箭头方向 = 请求/响应走向（`▶` 是请求，`◀` 是 JSON 响应），中间那一块 `s06` 中继就是本章要写的——预热计数 + 合并上游 usage。

![architecture](images/architecture.svg)

## 工作原理

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

## 测试

```bash
python -m pytest tests/test_s06_token_counting.py -v
```

两条覆盖:

1. `test_usage_field_populated` —— OpenAI 路径:响应里 `usage.prompt_
   tokens >= 1` 且 `total_tokens >= prompt_tokens`。
2. `test_non_openai_falls_back_to_char_estimator` —— Claude 路径:
   `usage.prompt_tokens >= 1`(证明 `count_estimate` 分支跑了)。

两条测试都用 `tests/conftest.py` 里共享的 `upstream_openai` / `upstream_claude` respx 固定器。

## → new-api 源码

- `service/TokenCalculate.go` —— 真实实现。按厂商分派(OpenAI 分词
  器、Claude 启发式、Gemini 启发式),缓存每条消息的计数,然后把结
  果交给计费层。

## 取舍

- **还没有流式 token 计数**。计数在请求阶段算;流式响应(`s03_streaming_sse`)要把 token 数到 SSE chunk 落地的那一刻再算,目前还是 pre-consume 估算 + settle 校正的组合。
- **char/4 比较粗糙**。在英文上对 Claude/Gemini 准确率大约 ±20%——给软配额提示够用,精确计费则不行。生产路径应该在上游提供 `/count_tokens` 时调它。
- **overhead 是硬编码的**。每条消息 4 token 这条规则来自 OpenAI cookbook;真实 overhead 随 role 和工具定义而变。一章内我们接受这点漂移,后面再按 model-specific 规则读。
