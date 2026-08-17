# s02: OpenAI 协议 — 改两个字节,免费拿到所有 SDK

> Previous: [s01](../s01_minimal_relay/) · Next: [s03](../s03_streaming_sse/)

> *"对外说 OpenAI"* —— 一个 wire format 兼容所有 SDK。

> **Layer**：L1 协议与转发

## 问题

`s01` 在自定义路径(`/relay`)上回答自定义 JSON 形态——每个客户端都得学习我们的方言。但 LLM 生态已经讲 OpenAI 的 `chat.completions` 契约,我们再花精力教世界我们这套私有格式,根本起不来。

一个网关只要对接 OpenAI 的面,就能免费拿到所有现有的客户端。这不叫妥协,这叫搭便车。

## 本章要做什么

现在场景是:s01 在自定义路径(`/relay`)上回答自定义 JSON 形态,每个客户端都得学习我们的方言。要解决这个——**我们把网关的入站面改成 OpenAI 已经统一的 `/v1/chat/completions` 路径和 JSON schema(线协议:网关与客户端约定的 JSON / HTTP 形态)**,对外讲 OpenAI 那一套话。**为什么改的是"外头叫什么"而不是"里头怎么转"**:网关的转发逻辑没动,动的是对外暴露的契约,生态早已统一。本章就做这一件事:

1. **路由改名 `/relay` → `/v1/chat/completions` —— 为什么换路径**:OpenAI 生态统一讲这条路径,所有 SDK 默认就朝这里发。**为什么不留个 `/relay` 兼容旧调用方**:留两条路径等于让中继长期维护两套契约,SDK 默认配置过来还是撞到 OpenAI 形态;统一走一条,所有客户端零修改。
2. **请求 JSON 收窄到 OpenAI 的 schema —— 为什么收窄**:只强制 `model` 和 `messages` 必填,可选的 `temperature` / `max_tokens` / `stream` 接受但不主动发明;`model_dump(exclude_none=True)`(序列化时剥掉 None 字段,避免空字段落到线上)剥掉 None,**为什么不直接 `temperature: null` 转发**:OpenAI 把"省略"理解为"用服务端默认",把 `null` 理解为"强制传 null 覆盖默认";剥掉才能保住调用方本意。
3. **响应走 `response_model=ChatCompletionResponse` —— 为什么响应也要校验**:FastAPI 用 `response_model`(声明响应类型做自动校验)把上行回包按 OpenAI schema 再过一遍,任何字段缺失/形态错都会在网关边界就拦住,而不是被原样吐回、进了客户端才报错。
4. **转发循环本身逐字节不变 —— 为什么这点要明说**:Bearer 头、HTTPError→502、状态码透传这些 s01 已经验证过的内核,这一章完全复用。**只换外面、不动里面**就是本章的全部技术动作。

成品:任何 OpenAI 客户端(官方 SDK、LangChain、`curl`)能直连 `http://localhost:8002/v1/chat/completions`,客户端零修改。后续 s03 在这条路径上加 `stream=true`、s04 在这条路径下挂多厂商适配器,都基于这一章打下的形态。

## 方案

两处调整,没有新基础设施:

1. **重命名路由**: `/relay` → `/v1/chat/completions`。这就是 OpenAI 暴露的路径,所有客户端都已经认识它。
2. **收紧请求 schema**,对齐 OpenAI 的负载:`model`、`messages: [{role, content}, ...]`(带 `min_length=1`),外加可选的 `temperature`、`max_tokens`、`stream`。其它字段留给上游去拒绝——中继不去发明字段。

转发循环本身逐字节不变。唯一改的是我们"在外头叫什么"。

`## 问题` 提了两件痛:每个客户端都得学一遍自创方言 (痛点 #1)、LLM 生态没人认这套私有格式 (痛点 #2)。这两件事**任何一件**都不是客户端自己改一下能解决——必须由网关换对外形态。下面这幅图把这两件事各放到一个角色里:

- **`Client` (任何 OpenAI 客户端)** —— 装上 OpenAI 路径之前,这是客户端要被迫改代码的角色;装上之后,这事就被网关解了——SDK 默认就朝 `/v1/chat/completions` 发,零修改。
- **`OpenAI 形态 API` (本章要写的网关入站面)** —— 把痛点 #1 #2 的解决动作集中放在这里:把路径换 OpenAI 的、用 OpenAI schema 收紧 body、按 OpenAI schema 校验响应。Client 一行不改,Upstream 一行不动。
- **`Upstream` (LLM 服务)** —— 服务提供方。它看到的还是 s01 那条 `FORWARD_TARGET`,请求体形态没变——只是中间那层网关对外换了名字。

下面这张 ASCII 流程图把本章的形态压成一行,作为上面的对照——图里仍是 `Client / OpenAI 形态 API / Upstream` 三角色,箭头方向 = 请求/响应走向(`▶` 是请求,`◀` 是 JSON 响应),中间那一块就是本章要写的 OpenAI 形态 API(重命名路由 + 收窄 schema):

```
Client ──POST /v1/chat/completions──▶  OpenAI 形态的 API  ──POST FORWARD_TARGET──▶  Upstream
        ◀────── JSON ────────────                         ◀──────── JSON ───────────
```

下面这张架构图给读者一幅全局鸟瞰——图里仍是 `Client / OpenAI 形态 API / Upstream` 三个角色,请求自左向右、响应自右向左折返;中间那一块就是本章要写的 OpenAI 形态 API(重命名路由 + 收窄 schema):

![architecture](images/architecture.svg)

## 工作原理

**原理**: 一个 HTTP 请求从客户端进来, 它的生命周期是: 路由器按 `/v1/chat/completions` 路径挑出 chat 处理器 → 处理器用 OpenAI schema 校验请求体 (model + messages) → 处理器用 `exclude_none=True` 剥掉可选空字段 → 转发给上游 FORWARD_TARGET → 等待上游回包 → 用 `response_model=ChatCompletionResponse` 把上游回包过一遍 OpenAI schema → 把校验后的 JSON 吐回客户端。整章所有部件都为这条主线服务。

**1. 一个 chat handler (`POST /v1/chat/completions`, `response_model=ChatCompletionResponse`)** —— 把 s01 的 `/relay` 路径换成 OpenAI 生态统一认的 `/v1/chat/completions`,所有 SDK 默认就朝这里发。`response_model`(FastAPI 装饰器参数,声明响应类型做自动校验)让回包也走 schema 校验,任何字段缺失/形态错都会在网关边界就拦住。

**2. 一个 OpenAI request schema (Pydantic `ChatCompletionRequest` + `ChatMessage`)** —— 在花一次网络往返之前先按 OpenAI 的字段集校验 body 格式。`Field(min_length=1)` (Pydantic 字段约束,列表至少 1 条) 强制 `messages` 不能传空数组;`exclude_none=True` (序列化时剥掉 None 字段,避免空字段落到线上) 在序列化前剥掉可空字段,所以不传 `temperature` 的调用方不会在线上传出一个空的 JSON 键——OpenAI 把"省略"理解为"用服务端默认",`null` 是"强制传 null 覆盖默认",两种语义不能混。

**3. 一个 marshal/unmarshal 入口 (`common/json.py`)** —— 业务代码唯一允许使用的 JSON 入口:`marshal` 输出紧凑 UTF-8 字节(无空格、`ensure_ascii=False`);`unmarshal_str` 通过 Pydantic 模型解析线包,边界处的响应校验就完成了。对齐 new-api 的 `common/json.go` 规则。

```python
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)  # Field(min_length=...):Pydantic 字段约束,列表至少 1 条
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
```

```python
@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(req: ChatCompletionRequest) -> dict:
    headers = {"Authorization": f"Bearer {UPSTREAM_KEY}"} if UPSTREAM_KEY else {}
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

逐行看:

- `headers = ... if UPSTREAM_KEY else {}` —— 中继负责注入厂商 key。调用方永远看不到。这正是网关存在最重要的单一原因。
- `timeout=30.0` —— 别继承一个无限大的默认值。挂住的上游不能反过来挂住中继。
- `except httpx.HTTPError` → **502**。传输层失败是我们上游的锅,不是调用方的;`502 Bad Gateway` 把这件事说得很清楚。
- `if r.status_code >= 400` —— 把上游的状态码原样透传。OpenAI 返回 429,调用方就应该看到 429,而不是被洗成 500。
- `response_model=ChatCompletionResponse` —— 上行回包按 OpenAI schema 再过一遍。任何字段缺失/形态错都会在网关边界就拦住,而不是被原样吐回、进了客户端才报错。
- 每次请求都用 `async with` 关闭客户端(及其连接池(client 复用的 TCP 连接集合))。简单,但确实是浪费——s10 用一个共享连接池修掉这点。

配置全部走环境变量,所以任何一章都不用动代码就能切换目标:

```python
PORT           = int(os.getenv("PORT", "8002"))
FORWARD_TARGET = os.getenv("FORWARD_TARGET", "https://api.openai.com/v1/chat/completions")
UPSTREAM_KEY   = os.getenv("UPSTREAM_OPENAI_KEY", "")
```

## 运行

```sh
cd s02_openai_protocol
PORT=8002 python code.py
```

确认 OpenAI schema 端点能不能响应?打这条 curl——能拿到 `{"status":"ok"}` 说明 FastAPI 进程在响应、`ChatCompletionRequest`/`ChatCompletionResponse` 两个 schema 都加载到内存里了:

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

## → new-api 源码

| 这里 | new-api |
|---|---|
| `ChatCompletionRequest` 模型 | `relay/channel/openai/adaptor.go` —— OpenAI 线协议和内部 `relay` 结构之间的入参/响应 DTO 转换 |
| `chat_completions` 路由 | `controller/relay.go` —— 把入站请求派发到 OpenAI 的 `Adaptor`(new-api 术语:厂商适配器接口) |
| `model_dump(exclude_none=True)` | `relay/channel/openai/adaptor.go` 的 `ConvertOpenAIRequest` + `dto` 各包 —— 转发前每个 channel adaptor 把自己家厂商的请求转成内部 `dto.GeneralOpenAIRequest`,Go 端用 `omitempty` JSON tag 实现"丢空字段";`relay/constant/relay_mode.go` 里只放 `RelayModeChatCompletions` 之类的模式枚举 |

new-api 把这套模式抽象成 `Adaptor` 接口(`relay/channel/openai/adaptor.go`),每个厂商一个实现。s04 我们会走到同样的设计。

## 本章不做什么

- **没有 Claude / Gemini 的协议转换** (把 OpenAI 之外的厂商方言翻成 OpenAI 形态给客户端)——请求体还是 OpenAI 形态, Claude 风格的 `system` 块、或 Gemini 的 `contents` 数组都会被原样转发、再被上游拒绝。→ s04。
- **没有流式** (逐 token 输出)——`r.json()` 等完整 body, 逐 token 输出不可能。→ s03。
- **没有鉴权** (任何能访问端口的人就能调网关)——任何能访问 8002 端口的人都能打中继。→ s05 在 chat 路由前加闸门。
- **没有配额 / 日志 / 指标** (按用户计费 / 调用历史 / 监控)——没法按用户计费、看不到调用历史、没有监控。→ s07、s11、s16。

## 已知限制

- **每请求新建连接池** (connection pool, client 复用的 TCP 连接集合)——`async with httpx.AsyncClient()` 每次都重连, 高并发下握手成本可观; 长连接客户端是生产答案。→ s10 修掉这点。
- **单上游** (单一上游厂商, 没有 channel 表 / 权重 / 故障切换)——`FORWARD_TARGET` 写死, 厂商挂了整套就挂。→ s10、s13。
- **没有重试和退避** ——一次短暂的上游抖动会以 502 暴露给调用方。→ s13。

## 设计选择

- **路径改名不留 `/relay` 兼容口** ——留两条等于让中继长期维护两套契约;SDK 默认配置过来还是撞到 OpenAI 形态,统一走一条,所有客户端零修改。
- **`response_model=ChatCompletionResponse` 在边界过 schema** ——让回包也在网关边界被校验一次, 字段缺失/形态错不会绕过网关才到客户端才报错。

## 下章预告

s02 客户端拿到完整 JSON 才开始渲染,7 秒延迟。s03 把协议升级到逐 token 流式,客户端首字延迟降到几百毫秒。