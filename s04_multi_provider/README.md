# s04: 多厂商适配器分派 — 按 model 前缀挑适配器,请求体各转各的

> Previous: [s03](../s03_streaming_sse/) · Next: [s05](../s05_api_key_auth/)

> *"前缀选 provider"* —— model 名就是路由键。

> **Layer**：L1 协议与转发

## 本章要做什么

引入 `Provider` 抽象基类,按模型名前缀(`gpt-`/`o` → OpenAI,`claude-` → Claude,`gemini-` → Gemini)挑适配器。每个 provider 把 OpenAI 请求翻译成自家线协议（线协议：网关与客户端约定的 JSON / HTTP 形态）,再把响应翻回 OpenAI 形态。学完你能用一个客户端对接三家上游。

## 上一章复盘

s03 把协议窄到 OpenAI 一种 vendor。现在要加 Claude / Gemini,但客户端不应该改。

## 在整体中的位置

网关唯一的"协议多元"出口——前面 3 章只接 OpenAI 形态,从此往后客户端始终用 OpenAI 形态说话,网关按 model 决定用哪家上游。

## 问题

`s02` 和 `s03` 把一份 OpenAI 形态的 JSON body 原样转发。上游就是
OpenAI 时一切相安无事——但 OpenAI 期望的 body(`model`、`messages`、
`temperature`、可选的 `stream`)并不是 Anthropic 或 Google 期望的样子。Claude 要 `x-api-key`、`anthropic-version` 请求头,以及每个请求都要的 `max_tokens`。Gemini 要一个 `contents: [{role, parts: [{text}]}]` 数组,以及放在 URL 查询串里的 API key。

如果把 OpenAI 的 JSON 直接透传到 Claude,上游就回 `400 invalid request`;透传到 Gemini 也一样。一个客户端、一套线协议、三个互不兼容的上游——这就是 s04 要解决的问题。

## 方案

引入一个 `Provider` 抽象基类,每个上游一个具体实现。每个 provider 只做两件事:

1. **把 OpenAI 请求翻译成自家线协议** (`to_upstream`)。
2. **把自家响应翻回 OpenAI 形态** (`from_upstream`)。

路由处理器通过 `pick_provider(model)` 按模型名前缀挑出对应适配器（`Adaptor`，new-api 术语：厂商适配器接口），然后沿着这个适配器转发请求。客户端看到的 `/v1/chat/completions` 入口和 JSON 形态完全一样,无论最后答的是哪家上游。

下面这张 ASCII 流程图把分派路径压成一行——和下面那张架构图相对照:上面这张是单跳时序,下面那张是角色拓扑,中间那块都是"按模型名前缀选":

```
Client ──POST /v1/chat/completions──▶  Relay(按模型名前缀选)  ──POST upstream──▶  Provider
        ◀────── OpenAI JSON ─────────                                    ◀──── JSON ────
```

下面这张架构图给读者一幅全局鸟瞰——图里有 `Client`、`Relay`、以及根据模型名前缀动态选出的 `Provider` 三个角色,箭头方向 = 请求/响应走向(`▶` 是请求,`◀` 是 JSON 响应),中间那一块就是本章要写的 Relay,按模型名挑 adapter:

![architecture](images/architecture.svg)

## 工作原理

适配器表是本章的核心:

| 模型名前缀 | Provider | 上游 URL |
|---|---|---|
| `gpt-` 或 `o` | `OpenAIProvider` | `https://api.openai.com/v1/chat/completions` |
| `claude-` | `ClaudeProvider` | `https://api.anthropic.com/v1/messages` |
| `gemini-` | `GeminiProvider` | `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=…` |
| 其它 | — | `400 unknown model` |

每个 provider 只翻译"真正不一致的部分"——共有字段(`model`、
`messages`)原样透传;厂商专属字段(Claude 的 `system`、`max_tokens`;
Gemini 的 `contents` 形态)显式构造。响应被折叠回 OpenAI 的
`chat.completion` 形态,所以客户端不需要知道答的是哪家上游:

```python
class Provider(ABC):
    name: str

    @abstractmethod
    def to_upstream(self, req: dict) -> tuple[str, dict, dict]: ...

    @abstractmethod
    def from_upstream(self, payload: dict) -> dict: ...


def pick_provider(model: str) -> Provider:
    if model.startswith("gpt-") or model.startswith("o"):
        return OpenAIProvider()
    if model.startswith("claude-"):
        return ClaudeProvider()
    if model.startswith("gemini-"):
        return GeminiProvider()
    raise ValueError(f"unknown model: {model}")
```

路由处理器几乎和 s02 一模一样——新增的只有 `pick_provider(req.model)`
和夹在原 `httpx.AsyncClient.post` 前后的 `provider.to_upstream` /
`provider.from_upstream`:

```python
@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    try:
        provider = pick_provider(req.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    payload = req.model_dump(exclude_none=True)
    payload["_api_key"] = _key_for(provider.name)
    url, headers, upstream_body = provider.to_upstream(payload)
    body_bytes = marshal(upstream_body)
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(url, content=body_bytes, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    translated = provider.from_upstream(json.loads(r.text))
    return JSONResponse(translated)
```

API key 来自各家专属的环境变量(`UPSTREAM_OPENAI_KEY`、
`UPSTREAM_CLAUDE_KEY`、`UPSTREAM_GEMINI_KEY`),通过 `_key_for
(provider.name)` 解析、在适配器看到之前塞进 payload 的 `_api_key` 槽
里。下划线前缀保证这个字段不会出现在序列化后的线协议里。

## 运行

```sh
cd s04_multi_provider
PORT=8004 python code.py
```

健康:

```sh
curl http://localhost:8004/health
# {"status":"ok"}
```

三家厂商,一份请求形态(把对应的 `UPSTREAM_*_KEY` 设上才有真实回复;不设的话上游会回 401,我们正好希望中继把它原样透传):

```sh
# OpenAI
curl -X POST http://localhost:8004/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'

# Claude
curl -X POST http://localhost:8004/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"claude-3-5-sonnet-20241022","messages":[{"role":"user","content":"hi"}]}'

# Gemini
curl -X POST http://localhost:8004/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gemini-1.5-flash","messages":[{"role":"user","content":"hi"}]}'
```

## 测试

```sh
pytest tests/test_s04_multi_provider.py -v
```

三家厂商都通过 `respx` mock(每条测试都拿同一份 `three_upstreams`
固定器,它同时挂上三家上游的 mock,所以一次跑就能遍历整张分派表):

- `test_routes_openai` —— `model: gpt-4o-mini` 被路由到 OpenAI,中继
  返回 `openai-ok`。
- `test_routes_claude` —— `model: claude-3-5-sonnet-20241022` 被路由
  到 Anthropic,响应被翻回 OpenAI 形态,`choices[0].message.content`
  里是 `claude-ok`。
- `test_routes_gemini` —— `model: gemini-1.5-flash` 被路由到 Google
  端点,返回 `gemini-ok`。
- `test_unknown_model_rejected` —— `model: mystery-7` 在
  `pick_provider` 阶段失败,返回 `400`。

## → new-api 源码

| 这里 | new-api |
|---|---|
| `Provider` ABC | `relay/channel/adaptor.go` —— 每个 channel 都实现的 `Adaptor` 接口 |
| `OpenAIProvider` | `relay/channel/openai/adaptor.go` —— OpenAI 专属的请求/响应转换 |
| `ClaudeProvider` | `relay/channel/claude/adaptor.go` —— Anthropic Messages 的转换 |
| `GeminiProvider` | `relay/channel/gemini/adaptor.go` —— Google `generateContent` 的转换 |
| `pick_provider(model)` | `relay/relay.go` —— 通过检查模型名把入站请求派发到对应 channel |

new-api 走得更远:它有一个 `GetAdaptor(meta)` 工厂,把 `(channel,
model)` 元组映射到适配器实例;另外每 channel 都有 `Key` 模式(我们这
里硬编码的 `_*_KEY` 环境变量变成运行时可配置)。Go 端每家厂商都有流
式适配器——见下面的取舍。

## 取舍

明确**没有**做的事:

- **没有流式翻译**。当 `stream: true` 时,我们仍然等整个响应再返
  回 JSON。三家厂商的 SSE 线协议在流中段不同(OpenAI 推 `data:
  {...}\n\n`,Claude 推 `event: …` 行,Gemini 推 `data: [array,…]`),
  真正的流式翻译是另一道独立的设计题。→ s05+。
- **OpenAI 路径没有真正的 `system` 翻译**。OpenAI 客户端可以把
  `system` 放在 `messages` 里(`{"role": "system", "content": "…"}`);
  Claude 想要的是顶层 `system` 字段。适配器做了顶层 `system` 的提
  取,但 `messages` 里的 `system` 这条分支还没有处理。
- **按前缀路由很脆**。一个叫 `open-mistral-7b`(真实的 Mistral 模型
  名)的模型会被 `o` 匹配到 OpenAI provider——然后 401 或 400。new-api
  的解法是按 `channel` 路由,而不是按 `model`,所以运维在配置阶段就
  声明"这个模型走 Anthropic"。
- **每请求新建连接池**。正确,但慢;长连接客户端才是生产答
  案。→ s10。
- **没有重试 / 退避**。→ s13。

## 下章预告

s04 任何能访问的客户端都能打网关,只要有路径就行。s05 加 API key 鉴权,把"匿名"打掉。
