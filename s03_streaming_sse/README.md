# s03: 流式 SSE 直通 — 转发第一个字节就算赢

> Previous: [s02](../s02_openai_protocol/) · Next: [s04](../s04_multi_provider/)

> *"一个字一个字流出去"* —— 客户端先看到第一个字就算赢。

> **Layer**：L1 协议与转发

## 问题

`s02` 用 `r = await client.post(...)` 等整个响应,然后 `r.json()`。对
于一条产出 200 个 token、按 30 tok/s 的聊天补全来说,客户端在将近 7
秒里只能盯着空白屏。逐 token 推送是让聊天产品"看起来活"的方式;没
有它,任何建立在中继上的聊天 UX 都破了。

上游本身说的就是 Server-Sent Events(SSE)——`Content-Type: text/
event-stream`、`data: {...}\n\n` 一帧接着一帧,最后是 `data: [DONE]\n\n`。
中继不能缓存、解析、重塑这些字节;必须把它们一路端出去。

## 本章要做什么

现在场景是:s02 把整个 JSON body 攒齐再回吐——200 个 token、按 30 tok/s 的回复,客户端将近 7 秒只能盯空白屏。生产聊天的 UX 不能接受这个延迟。要解决这个——**我们让 s02 那条路径支持流式(逐 token 推送:一个 token 一小段字节,客户端每收到一段就立刻渲染)**,让聊天"看起来活"。本章就在 s02 那条路径上把流式打开:

1. **按 `req.stream` 分两条路 —— 为什么必须分支**:非流式请求(s02 已实现)是"攒齐再回 JSON",流式请求是"转发第一个字节就开始推"。**为什么不能两路合并**:流式路径用的是 `httpx.AsyncClient.stream(...)` + `StreamingResponse` 的异步生成器,非流式用的是 `await client.post(...)` + `r.json()`,前者不缓存上游 body、后者必须等到上游关闭连接,合并就是同时要两条矛盾策略。
2. **流式走 `httpx.AsyncClient.stream(...)` + `aiter_bytes()` —— 为什么是这套 API**:SSE 是 HTTP 长连接 + 文本帧,**为什么必须用 httpx.stream 的 context manager**:`aiter_bytes()` 只能在 `stream(...)` 返回的响应对象上调,普通 `client.post()` 等到 body 完整才返回对象、等于把流式退化成 s02;**为什么是 `aiter_bytes()` 而不是 `aiter_text()`**:我们不解析、不重塑帧,逐字节原样转出,字节边界错了才会把 `data: {...}\n\n` 撕成两半。
3. **响应头加 `cache-control: no-cache` 和 `x-accel-buffering: no` —— 为什么两都要写**:前者禁止中间节点缓存一份开放式流,**为什么这个头不能省**:中间代理看到长连接默认按"可缓存资源"处理,会一口气攒到阈值再放行;**为什么还要 `x-accel-buffering: no`**:这是 nginx 的专属指令(`x-accel-buffering` 是 nginx 的反向代理缓冲开关),关掉 `proxy_buffering`,nginx 就不会卡住 SSE body,客户端才看得到逐字。

成品:`curl -N -X POST .../v1/chat/completions -d '...,"stream":true}'` 看到一字一字往出冒,首字延迟几百毫秒;不带 `stream` 时仍走 s02 的 JSON 路径。后续 s04 在这条流式通道下挂 Claude/Gemini 适配器,逐厂商的 SSE 帧形态差异由那一章解决。

## 方案

在 `chat_completions` handler 里,看到 `req.stream` 字段就分两条路:

- **stream=false**(s02 已实现):走 `await client.post(...)`,攒齐再回 `JSONResponse`。
- **stream=true**(本章新加):打开 `httpx.AsyncClient.stream(...)`,返回一个 FastAPI
  `StreamingResponse`(FastAPI 的流式响应类型,按 chunk 推送),用 `async for
  chunk in upstream.aiter_bytes()` 产出字节。两个响应头要紧:
  `cache-control: no-cache` 和 `x-accel-buffering: no`(后者告诉 nginx
  不要做缓冲,反向代理常常会一直等到阈值才放行 SSE body)。

`## 问题` 提了一件痛:客户端 7 秒空白屏等攒齐再渲染 (痛点)。这件事**没法靠"客户端轮询"或"客户端 JS 优化"能解决**——必须由网关把响应方式切成边读边推。下面这幅图把这件痛放到三个角色里:

- **`Client` (流式调用方)** —— 在 s02 那条攒齐路径上,这是被 7 秒空白屏困住的角色;切流式之后,这事被网关解了——客户端读一个 chunk 渲染一个 chunk。
- **`Relay` (本章要写的流式分支)** —— 把痛点的解决动作集中放在这里:看到 `stream=true` 就开 `httpx.AsyncClient.stream(...)` 上下文,用 `aiter_bytes()` 拿到字节立刻 `yield` 给 FastAPI 的 `StreamingResponse`。Client 拿到首字就几百毫秒。
- **`Upstream` (SSE)** —— OpenAI 那家按 `data: {...}\n\n` 一帧接一帧推,最后是 `data: [DONE]\n\n`。中继不做解析、不重塑,把字节原样透传。

下面这张 ASCII 时序图把流式响应一口气画出来——和下面那张架构图相对照:上面这张是端到端时序(chunk 随时间一条一条往下走),下面那张是角色拓扑(谁在哪、消息怎么流):

```
Client ──POST /v1/chat/completions {stream:true}──▶  Relay  ──POST FORWARD_TARGET──▶  Upstream
        ◀──── SSE chunk 1 ────                   ◀──── SSE chunk 1 ────
        ◀──── SSE chunk 2 ────                   ◀──── SSE chunk 2 ────
        ◀──── SSE [DONE] ────                    ◀──── SSE [DONE] ────
```

下面这张架构图给读者一幅全局鸟瞰——图里仍是 `Client / Relay / Upstream` 三个角色,箭头方向 = 请求/响应走向(`▶` 是请求,`◀` 是 SSE 流式 chunk),中间那一块就是本章要写的 Relay,拿到一个 chunk 立刻 yield,边流边推:

![architecture](images/architecture.svg)

## 工作原理

**原理**: 一个 HTTP 请求从客户端进来, 它的生命周期是: 路由器按 `/v1/chat/completions` 路径挑出 chat 处理器 → 处理器看 `req.stream` 字段分支 → 流式分支开 `httpx.AsyncClient.stream(...)` 上下文 + 加 `accept: text/event-stream` 头 → 进入 async 迭代器用 `aiter_bytes()` 拿上游字节 → 边读边 `yield` 给 FastAPI `StreamingResponse` → 响应头同时打 `cache-control: no-cache` 和 `x-accel-buffering: no` → 客户端断开时 `async with` 退出自动关上游连接。整章所有部件都为这条主线服务。

**1. 一个 stream branch (FastAPI 处理器里的 `if req.stream`)** —— 同一路由按 `stream` 字段切两条路。非流式走 s02 的 `await client.post(...)` + `r.json()`,流式走下面两个部件;两条路用同一个 OpenAI schema 校验入口。

**2. 一个 httpx streaming context (`httpx.AsyncClient.stream(...)`)** —— `httpx.stream(...)` 返回的响应对象支持 `aiter_bytes()`,普通 `client.post()` 等到 body 完整才返回对象、等于把流式退化成 s02——所以必须用 `stream(...)` 的 context manager。`async for chunk in upstream.aiter_bytes()` (httpx 流响应的字节迭代器) 拿到上游随手缓冲的字节,不假设"一 SSE 帧一个 chunk"。

**3. 一个 async iterator (`async def _relay_stream`,`yield chunk`)** —— 异步生成器 (`AsyncIterator`,Python 异步迭代器,`async for` 协议) 配合 `httpx` 不等上游发送完整个 body,只读上游写出来的内容;`yield chunk` 把这些字节直接推给 FastAPI 的响应,后者再 `flush` (flush:把缓冲立即推给客户端) 到线缆。客户端断开时 `async with` 退出,上游连接也自动关。

```python
async def _relay_stream(req: ChatCompletionRequest) -> AsyncIterator[bytes]:
    headers = {"Authorization": f"Bearer {UPSTREAM_KEY}"} if UPSTREAM_KEY else {}
    body = marshal(req.model_dump(exclude_none=True))
    timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", FORWARD_TARGET, content=body,
            headers={**headers, "content-type": "application/json", "accept": "text/event-stream"},
        ) as upstream:
            async for chunk in upstream.aiter_bytes():
                yield chunk
```

```python
@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    if req.stream:
        return StreamingResponse(
            _relay_stream(req),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )
    # 非流式分支与 s02 一致
    ...
```

`accept: text/event-stream` 头 (accept 头:客户端声明能接受的响应类型,流式场景礼貌性声明) 是个礼貌性的声明:大多数上游都尊重但并不要求它,因为请求体形态本身已经在宣告流式了。

为什么这两个响应头?

- `cache-control: no-cache` (禁止中间节点缓存开放式流) —— 中间节点不能把一份开放式流当成缓存分发。
- `x-accel-buffering: no` (关掉 nginx `proxy_buffering` 的指令) —— 关掉 nginx 的 `proxy_buffering` (x-accel-buffering 概念:nginx 反向代理的缓冲开关),否则 nginx 会一直攒到阈值才放行,客户端看到的 SSE 会"卡住"。

## 运行

```sh
cd s03_streaming_sse
PORT=8003 python code.py
```

确认流式 endpoint 能响应?打这条 curl——能拿到 `{"status":"ok"}` 说明 FastAPI 进程在响应、流式分支加载到内存了;再发一个 `stream:true` 请求,看到 `text/event-stream` 头和 chunk-by-chunk 字节说明 `StreamingResponse` + `aiter_bytes()` 都挂上了:

```sh
curl http://localhost:8003/health
# {"status":"ok"}
```

非流式(与 s02 相同):

```sh
curl -X POST http://localhost:8003/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

流式——`-N` 关闭 curl 的输出缓冲,这样能实时看到 chunk 到来:

```sh
curl -N -X POST http://localhost:8003/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

不设 `UPSTREAM_OPENAI_KEY` 时,上游返回 401——这正好说明中继在向前转发。配上真实 key 就能拿到流式文本。

## → new-api 源码

| 这里 | new-api |
|---|---|
| `_relay_stream` / `StreamingResponse` | `relay/helper/stream_scanner.go` —— chunked SSE 写入器(`StreamScannerHandler` 调 `sendStreamData`、`bufio.Scanner` 按行切片);`relay/channel/openai/relay-openai.go` 里的 `OaiStreamHandler` 是 OpenAI 专属编排 |
| `accept: text/event-stream` | `controller/relay.go` —— 在 `req.Stream` 上的流协商 |
| `x-accel-buffering: no` | `middleware/proxy.go` —— 对 `/v1` 路由关闭 nginx 缓冲 |

new-api 把这件事拆成两阶段:SSE chunker 负责把上游 body 切成事件;
channel adaptor 知道每家厂商的帧形态。我们这里先压成一段直通,s04 加
入 Claude/Gemini 适配器时再拆开。

## 本章不做什么

- **没有帧解析或重组** (按 SSE 帧边界重组字节)——我们转发原始字节;如果哪天上游改了 SSE 形态 (Anthropic 用 `event:` 行), 中继就会破。→ s04 引入按厂商的适配器。
- **没有客户端断连向上游传播** (客户端中途挂断时同步关闭上游请求)——如果调用方中途挂断, 我们就一直从上游读到它关闭, 白花 token 和钱。→ s08 把 `request.is_disconnected()` 接进生成器。
- **没有鉴权、配额、日志、指标** (按用户计费 / 调用历史 / 监控)——任何能访问 8003 端口的人都能花 key, 看不到调用历史。→ s05、s07、s11、s16。

## 已知限制

- **每请求新建连接池** (connection pool, client 复用的 TCP 连接集合)——`async with httpx.AsyncClient()` 每次都重连, 高并发下握手成本可观; 共享 limits 的常驻 `httpx.AsyncClient` 是生产答案。→ s10 修掉这点。
- **`read=None` 读超时无上限** ——长连接上游可能挂死不返回也不关,网关会一直被占着;生产要加心跳或上游空闲超时。→ s10。
- **没有重试 / 退避** ——一次短暂的上游抖动会以 502 暴露给调用方。→ s13。

## 设计选择

- **流式 / 非流式分两路不复用** ——前者靠 `aiter_bytes()` async 生成器,后者靠 `await client.post(...)` + `r.json()`,两条策略正好相反,合并只能两路都做不到位。
- **`aiter_bytes()` 而不是 `aiter_text()`** ——我们不解析、不重塑帧, 逐字节原样转出;按 text 解码会在边界处把 `data: {...}\n\n` 撕成两半。

## 下章预告

s03 接受 / 转发 OpenAI 形态,但上游只有 OpenAI 一家。s04 接 Claude / Gemini,按 model 前缀把请求分到对应适配器。