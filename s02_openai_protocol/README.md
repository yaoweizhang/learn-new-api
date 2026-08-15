# s02: OpenAI 协议

> Previous: [s01](../s01_minimal_relay/) · Next: [s03](../s03_streaming_sse/)

> *"对外说 OpenAI"* —— 一个 wire format 兼容所有 SDK。

> **Layer**：L1 协议与转发

**本章新增**:入站面采用 OpenAI 的 `/v1/chat/completions` 路径和 JSON
schema。任何 OpenAI 客户端(官方 SDK、LangChain、LlamaIndex、终端里
的 `curl`)都能原样对我们发起调用。

## 问题

`s01` 在自定义路径(`/relay`)上回答自定义 JSON 形态——每个客户端都得学习我们的方言。但 LLM 生态已经讲 OpenAI 的 `chat.completions` 契约,我们再花精力教世界我们这套私有格式,根本起不来。

一个网关只要对接 OpenAI 的面,就能免费拿到所有现有的客户端。这不叫妥协,这叫搭便车。

## 方案

两处调整,没有新基础设施:

1. **重命名路由**: `/relay` → `/v1/chat/completions`。这就是 OpenAI 暴露的路径,所有客户端都已经认识它。
2. **收紧请求 schema**,对齐 OpenAI 的负载:`model`、`messages: [{role, content}, ...]`(带 `min_length=1`),外加可选的 `temperature`、`max_tokens`、`stream`。其它字段留给上游去拒绝——中继不去发明字段。

转发循环本身逐字节不变。唯一改的是我们"在外头叫什么"。

```
Client ──POST /v1/chat/completions──▶  OpenAI 形态的 API  ──POST FORWARD_TARGET──▶  Upstream
        ◀────── JSON ────────────                         ◀──────── JSON ───────────
```

![architecture](images/architecture.svg)

## 工作原理

Pydantic 模型承担 schema;处理器通过 `common/json` 工具做编解码,保证
JSON 来回的规矩和别的章节完全一致:

```python
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
```

路由就是把 `s01` 的 relay 换了个 URL,加了个类型化的 body。`model_dump(exclude_none=True)` 在序列化前剥掉可空字段,所以不传 `temperature` 的调用方不会在线上传出一个空的 JSON 键:

```python
@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(req: ChatCompletionRequest) -> dict:
    headers = {"Authorization": f"Bearer {UPSTREAM_KEY}}" if UPSTREAM_KEY else {}
    body = marshal(req.model_dump(exclude_none=True))
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(
                FORWARD_TARGET, content=body, headers={**headers, "content-type": "application/json"}
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return unmarshal_str(r.text, ChatCompletionResponse).model_dump()
```

`common/json.py` 的 `marshal` 和 `unmarshal_str` 是业务代码唯一允许使用的 JSON 入口:`marshal` 输出紧凑 UTF-8 字节(无空格、`ensure_ascii=False`);`unmarshal_str` 通过 Pydantic 模型解析线包,边界处的响应校验就完成了。把它们集中到一个模块,正好对齐 new-api 的 `common/json.go` 规则。

为什么要 `exclude_none=True`?OpenAI 的 API 把"省略的可选字段"理解为"服务端用默认"。`temperature: null` 是另一种请求——它强制传 `null`,从而绕过上游默认。剥掉字段才能保住调用方的本意。

## 运行

```sh
cd s02_openai_protocol
PORT=8002 python code.py
```

确认活着:

```sh
curl http://localhost:8002/health
# {"status":"ok"}
```

转发一次(先 `export UPSTREAM_OPENAI_KEY` 才有真实回复;不设的话上游会返回 401,我们正好希望看到原样透传这个状态码):

```sh
curl -X POST http://localhost:8002/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

## 测试

```sh
pytest tests/test_s02_openai_protocol.py -v
```

上游用 `respx` mock(`tests/conftest.py` 的 `upstream_openai` 固定器),所以套件能离线跑,同时仍断言真实的线协议形态。两个测试覆盖如下:

- `test_openai_route_exists` —— 合法负载被转发,返回带 `choices` 键的 body。
- `test_request_validation_rejects_missing_messages` —— 缺 `messages` 时,Pydantic 边界返回 `422`,根本不会发出任何网络请求。

## → new-api 源码

| 这里 | new-api |
|---|---|
| `ChatCompletionRequest` 模型 | `relay/channel/openai/adaptor.go` —— OpenAI 线协议和内部 `relay` 结构之间的入参/响应 DTO 转换 |
| `chat_completions` 路由 | `relay/relay.go` —— 把入站请求派发到 OpenAI 的 `Adaptor` |
| `model_dump(exclude_none=True)` | `relay/constant.go` —— 转发前由它按 channel 做归一化,丢掉空字段 |

new-api 把这套模式抽象成 `Adaptor` 接口(`relay/channel/openai/adaptor.go`),每个厂商一个实现。s04 我们会走到同样的设计。

## 取舍

明确**没有**做的事:

- **没有 Claude / Gemini 的协议转换**。请求体还是 OpenAI 形态,所以 Claude 风格的 `system` 块、或 Gemini 的 `contents` 数组都会被原样转发、再被上游拒绝。→ s04。
- **没有流式**。`r.json()` 等完整 body,逐 token 输出不可能。→ s03。
- **没有鉴权、配额、日志、指标**。→ s05、s07、s11、s16。
- **每请求新建一个连接池**。正确,但慢;长连接客户端才是生产答案。→ s10。
- **没有重试和退避**。一次短暂的上游抖动会以 502 暴露给调用方。→ s13。
