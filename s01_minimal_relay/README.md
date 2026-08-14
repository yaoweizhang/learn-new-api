# s01: 最小的转发中继内核

> Previous: — · Next: [s02](../s02_openai_protocol/)

**本章新增**:一个 HTTP 转发器——一条路由,把 JSON 请求体传给单一上游,再把回复原样返回。

新增依赖:`fastapi`、`uvicorn`、`httpx`、`pydantic`。

## 问题

假设你的应用要调用一家 LLM 厂商。没有任何网关时,每个调用方都得各自
持有厂商 key、把厂商 URL 写死在代码里、再各自复制一份完全相同的请求
调度逻辑。换厂商、轮换 key、看一眼流量,都得修改所有调用方。这就是
靠人肉复制粘贴的中继,撑不过第一个下午。

解法就是在中间站一个程序。教程里其余所有内容——协议、流式、鉴权、配
额、日志——都建立在这一个想法之上,所以我们从最小可运行的版本讲起。

## 方案

一个进程,接住请求、转发出去、再把答复送回来。概念上就是一个针对
入站 HTTP 请求的 `while True` 循环,循环体里只做一件事:转发。

![architecture](images/architecture.svg)

```
Client  ──POST /relay──▶  Relay  ──POST FORWARD_TARGET──▶  Upstream
        ◀──── JSON ────          ◀──────── JSON ─────────
```

中继在这一阶段只多做了两件事:它持有上游 URL,也持有上游 key。调用
方既不需要知道 URL,也不需要知道 key。

## 工作原理

整个内核由三块组成。

**1. 一个存活探针路由。** 零依赖,后续每章都会用到(以及 s15 中 Docker
的健康检查):

```python
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
```

**2. 一个请求形态。** Pydantic 在我们花一次网络往返之前先校验请求
体。本章只强制要求 `model` 和 `messages`;s02 才会把它变成真正的
OpenAI schema:

```python
class RelayRequest(BaseModel):
    model: str
    messages: list[dict]
```

**3. 转发起。** `httpx.AsyncClient` 是 `requests` 的异步版本。异步在这
里很关键——中继几乎所有时长都花在等待上游上,阻塞式客户端每条在飞
的请求都会占用一个 OS 线程,吞吐几乎立刻见顶。

```python
@app.post("/relay")
async def relay(req: RelayRequest) -> dict:
    headers = {"Authorization": f"Bearer {UPSTREAM_KEY}"} if UPSTREAM_KEY else {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(FORWARD_TARGET, json=req.model_dump(), headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()
```

逐行看:

- `headers = ... if UPSTREAM_KEY else {}` —— 中继负责注入厂商 key。调用
  方永远看不到。这正是网关存在最重要的单一原因。
- `timeout=30.0` —— 绝不继承一个无限大的默认值。挂住的上游不能反过来
  挂住中继。
- `except httpx.HTTPError` → **502**。传输层失败是我们上游的锅,不是
  调用方的,`502 Bad Gateway` 把这件事说得很清楚。
- `if r.status_code >= 400` —— 把上游的状态码原样透传。OpenAI 返回 429,
  调用方就应该看到 429,而不是洗成 500。
- 每次请求都用 `async with` 关闭客户端(及其连接池)。简单,但确实是浪
  费——s10 用一个共享连接池来修掉这一点。

配置全部走环境变量,所以任何一章都不用改代码就能切换目标:

```python
PORT           = int(os.getenv("PORT", "8001"))
FORWARD_TARGET = os.getenv("FORWARD_TARGET", "https://api.openai.com/v1/chat/completions")
UPSTREAM_KEY   = os.getenv("UPSTREAM_OPENAI_KEY", "")
```

## 运行

```sh
cd s01_minimal_relay
PORT=8001 python code.py
```

确认活着:

```sh
curl http://localhost:8001/health
# {"status":"ok"}
```

转发一次请求(先 `export UPSTREAM_OPENAI_KEY=...` 才能拿到真实回复;
不设 key 的话上游会返回 401,而我们正好希望看到原样透传这个状态码,这正
是我们想要的行为):

```sh
curl -X POST http://localhost:8001/relay \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

## 测试

```sh
pytest tests/test_s01_minimal_relay.py -v
```

上游用 `respx` 进行 mock(`tests/conftest.py` 里的 `upstream_openai` 固
定器),所以测试可以离线跑、同时仍断言真实的线协议形态——中继必须返回
上游 `choices[0].message.content`,并且只能调用上游一次。

## → new-api 源码

| 这里 | new-api |
|---|---|
| `relay()` 路由 | `relay/relay.go` —— 把入站请求派发到上游的入口 |

新版本把这个单一路由泛化成一个 `Adaptor` 接口(`relay/channel/openai/
adaptor.go`——按 channel 的适配器,负责构造出站请求并解析回包),每个
厂商一套实现。我们在 s04 会走到同样的设计。

## 取舍

明确**没有**做的事:

- **没有鉴权**。能访问端口的人就能花你的 key。→ s05。
- **没有流式**。`r.json()` 等整个响应回来,所以逐 token 输出不可
  能。→ s03。
- **单上游**。一个 `FORWARD_TARGET`,没有 channel 表、没有权重、
  没有故障切换。→ s10、s13。
- **没有协议转换**。请求体原样转发,所以调用方必须已经会说上游方
  言。→ s02、s04。
- **没有配额/日志/指标**。→ s07、s11、s16。
- **每次新建一个连接池**。正确,但慢;长连接客户端才是生产答案。
