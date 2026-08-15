# s03: 流式 SSE 直通

> Previous: [s02](../s02_openai_protocol/) · Next: [s04](../s04_multi_provider/)

> *"一个字一个字流出去"* —— 客户端先看到第一个字就算赢。

> **Layer**：L1 协议与转发

**本章新增**:当 `stream=true` 时,中继打开一个 `httpx` 流式客户端、
原样转发 SSE chunk,使客户端能看到首 token 延迟。非流式请求仍按 s02
的方式返回 JSON。

## 问题

`s02` 用 `r = await client.post(...)` 等整个响应,然后 `r.json()`。对
于一条产出 200 个 token、按 30 tok/s 的聊天补全来说,客户端在将近 7
秒里只能盯着空白屏。逐 token 推送是让聊天产品"看起来活"的方式;没
有它,任何建立在中继上的聊天 UX 都破了。

上游本身说的就是 Server-Sent Events(SSE)——`Content-Type: text/
event-stream`、`data: {...}\n\n` 一帧接着一帧,最后是 `data: [DONE]\n\n`。
中继不能缓存、解析、重塑这些字节;必须把它们一路端出去。

## 方案

按 `req.stream` 做分支:

- **stream=false**:走和 s02 一样的 `await client.post(...)`,返回
  `JSONResponse`。
- **stream=true**:打开 `httpx.AsyncClient.stream(...)`,返回一个 FastAPI
  `StreamingResponse(media_type="text/event-stream")`,用 `async for
  chunk in upstream.aiter_bytes()` 产出字节。两个响应头要紧:
  `cache-control: no-cache` 和 `x-accel-buffering: no`(后者告诉 nginx
  不要做缓冲,反向代理常常会一直等到阈值才放行 SSE body)。

```
Client ──POST /v1/chat/completions {stream:true}──▶  Relay  ──POST FORWARD_TARGET──▶  Upstream
        ◀──── SSE chunk 1 ────                   ◀──── SSE chunk 1 ────
        ◀──── SSE chunk 2 ────                   ◀──── SSE chunk 2 ────
        ◀──── SSE [DONE] ────                    ◀──── SSE [DONE] ────
```

![architecture](images/architecture.svg)

## 工作原理

中继复用了 `s02` 的请求 schema 和 `marshal` 工具做对外请求体;新加
的只有一段流式转发生成器,以及一个分支响应:

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

`httpx` 不等上游发送完整个 body,只读上游写出来的内容;`yield chunk`
把这些字节直接推给 FastAPI 的响应,后者再 flush 到线缆。`aiter_bytes()`
返回的是上游随手缓冲出的内容——并不假设"一 SSE 帧一个 chunk"。

`accept: text/event-stream` 头是个礼貌性的声明:大多数上游都尊重但并不要求它,因为请求体形态本身已经在宣告流式了。

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

为什么这两个响应头?

- `cache-control: no-cache` —— 中间节点不能把一份开放式流当成缓存分发。
- `x-accel-buffering: no` —— 关掉 nginx 的 `proxy_buffering`,否则 nginx 会一直攒到阈值才放行,客户端看到的 SSE 会"卡住"。

## 运行

```sh
cd s03_streaming_sse
PORT=8003 python code.py
```

健康检查:

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

## 测试

```sh
pytest tests/test_s03_streaming_sse.py -v
```

`tests/conftest.py` 的 `upstream_openai` 固定器提供 respx mock;流式
测试用一份 SSE 负载覆盖默认 JSON 响应,并断言字节原样到达客户端。
覆盖范围:

- `test_streaming_returns_sse_chunks` —— `stream=true` 请求把上游的
  SSE chunk 原样返回(`hello `、`world`、`[DONE]`)。
- `test_non_streaming_still_works` —— 不带 `stream` 时仍走 JSON 响应
  路径。

## → new-api 源码

| 这里 | new-api |
|---|---|
| `_relay_stream` / `StreamingResponse` | `relay/sse.go` —— chunked SSE 写入器(`w.Write` 对应 `aiter_bytes`)以及流生命周期 |
| `accept: text/event-stream` | `relay/relay.go` —— 在 `req.Stream` 上的流协商 |
| `x-accel-buffering: no` | `middleware/proxy.go` —— 对 `/v1` 路由关闭 nginx 缓冲 |

new-api 把这件事拆成两阶段:SSE chunker 负责把上游 body 切成事件;
channel adaptor 知道每家厂商的帧形态。我们这里先压成一段直通,s04 加
入 Claude/Gemini 适配器时再拆开。

## 取舍

明确**没有**做的事:

- **没有帧解析或重组**。我们转发原始字节;如果哪天上游改了 SSE 形
  态(Anthropic 用 `event:` 行),中继就会破。→ s04 引入按厂商的适
  配器。
- **没有客户端断连向上游传播**。如果调用方中途挂断,我们就一直从
  上游读到它关闭,白花 token 和钱。→ s08 把 `request.is_disconnected()`
  接进生成器。
- **不能取消上游请求**。同样的问题换个形式。→ s08。
- **每请求新建连接池**。共享 limits 的常驻 `httpx.AsyncClient` 是
  生产答案。→ s10。
- **没有重试 / 退避 / 鉴权 / 配额 / 日志 / 指标**。→ s05、s07、
  s11、s13、s16。
