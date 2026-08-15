# s01: 最小的转发中继内核 — 中间站一个程序,所有调用方都走它

> Previous: — · Next: [s02](../s02_openai_protocol/)

> *"把请求转出去"* —— 转发是最朴素的网关。

> **Layer**：L1 协议与转发

## 本章要做什么

加一个 FastAPI 进程,一条 `/relay` 路由把 JSON body 转发到单一上游,再把响应原样吐回。学完你会拿到一个最薄但能跑的中继——之后所有章节都建立在这个内核上。

新增依赖:`fastapi`、`uvicorn`、`httpx`、`pydantic`。

## 在整体中的位置

网关的最内层循环——所有其它功能(鉴权、限速、配额、日志)都挂在它外层。没有它,根本没有"网关"这件事。

## 问题

你的应用要调一家 LLM 厂商,但没有网关。每个调用方都得各自持有厂商 key、把厂商 URL 写死在代码里、再各自复制一份完全相同的请求调度逻辑。换厂商、轮换 key、看一眼流量——三件事任何一件都得改所有调用方。这就是靠人肉复制粘贴的中继,撑不过第一个下午。

解法就是在中间站一个程序。教程里其余所有内容——协议、流式、鉴权、配额、日志——都建立在这一个想法上,所以我们从最小可运行的版本讲起。

## 方案

一个进程:接住请求、转发出去、再把答复送回来。说白了就是一个针对入站 HTTP 请求的 `while True` 循环,循环体里只做一件事——转发。

下图给出一幅全局鸟瞰——图里有 `Client`、本章要写的 `Relay` 进程、以及远处的 `Upstream` 三个角色。请求箭头从 `Client` 走到 `Relay` 再走到 `Upstream`,响应则反向沿两条路回来;中间那一块 `Relay` 是本章要写的进程:

![architecture](images/architecture.svg)

下面这张 ASCII 流程图把同一段流程压成一行,作为上面的对照——图里仍是 `Client / Relay / Upstream` 三角色,箭头方向 = 请求/响应走向(`▶` 是请求,`◀` 是 JSON 响应),中间那一块就是本章要写的 `Relay`:

```
Client  ──POST /relay──▶  Relay  ──POST FORWARD_TARGET──▶  Upstream
        ◀──── JSON ────          ◀──────── JSON ─────────
```

中继在这一阶段只多做了两件事:持有上游 URL,也持有上游 key。调用方既不需要知道 URL,也不需要知道 key。

## 工作原理

整个内核由三块组成。

**1. 一个存活探针路由。** 零依赖,后续每章都会用到(包括 s15 中 Docker 的健康检查——s15 又额外加了一条 `/healthz` 深检路由,这里 `/health` 维持最朴素的"进程在跑"语义):

```python
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
```

**2. 一个请求形态。** Pydantic（数据校验库：用 Python 类型注解定义结构、自动校验入参）在我们花一次网络往返之前先校验请求体。本章只强制要求 `model` 和 `messages`;s02 才会把它变成真正的 OpenAI schema:

```python
class RelayRequest(BaseModel):
    model: str
    messages: list[dict]
```

**3. 转发起。** 这里用的是 `httpx.AsyncClient`(支持异步的 HTTP 客户端库,与同步 requests 对应)——`requests` 的异步版本。异步不是装饰,是硬需求:中继几乎所有时长都花在等待上游上,阻塞式客户端每条在飞的请求都会占一个 OS 线程,吞吐几乎立刻见顶。

```python
@app.post("/relay")
async def relay(req: RelayRequest) -> dict:
    headers = {"Authorization": f"Bearer {UPSTREAM_KEY}"} if UPSTREAM_KEY else {}  # `Bearer`（HTTP 授权头格式：把令牌塞进 Authorization 头）
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

- `headers = ... if UPSTREAM_KEY else {}` —— 中继负责注入厂商 key。调用方永远看不到。这正是网关存在最重要的单一原因。
- `timeout=30.0` —— 别继承一个无限大的默认值。挂住的上游不能反过来挂住中继。
- `except httpx.HTTPError` → **502**。传输层失败是我们上游的锅,不是调用方的;`502 Bad Gateway` 把这件事说得很清楚。
- `if r.status_code >= 400` —— 把上游的状态码原样透传。OpenAI 返回 429,调用方就应该看到 429,而不是被洗成 500。
- 每次请求都用 `async with` 关闭客户端(及其连接池(client 复用的 TCP 连接集合))。简单,但确实是浪费——s10 用一个共享连接池修掉这点。

配置全部走环境变量,所以任何一章都不用动代码就能切换目标:

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

转发一次请求(先 `export UPSTREAM_OPENAI_KEY=...` 才有真实回复;不设 key 的话上游会回 401,而我们正好希望看到原样透传这个状态码):

```sh
curl -X POST http://localhost:8001/relay \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

## 测试

```sh
pytest tests/test_s01_minimal_relay.py -v
```

上游用 `respx`（拦截 httpx 出站请求的 mock 库）mock 拦截,测试用 `tests/conftest.py`（pytest 共享 fixture 的约定文件）里的 `upstream_openai` fixture（`pytest fixture`（pytest 测试装置：在测试前后准备/清理共享资源）），所以测试能离线跑,同时仍然断言真实的线协议形态——中继必须返回上游 `choices[0].message.content`,并且只该调一次上游。

> **测试栈词汇**：本章及后续用到 `pytest` 的几个概念——`fixture`（测试装置,见上）、`autouse`（fixture 的自动应用标志：声明后 pytest 会自动套用到所有测试）、`conftest.py`（pytest 共享 fixture 的约定文件,见上）、`TestClient`（FastAPI 自带的同步测试客户端：不启 HTTP 直接在内存里调用 app）、`respx`（见上）。后文直接复用这些词,不再重复解释。
>
> HTTP（超文本传输协议）状态码速查：`401` 未授权（token 错或缺失）、`402` 支付被拒（余额/配额不足）、`422` 请求格式错（参数校验未通过）、`502` 网关/上游挂了（拿到下游报错）。

## → new-api 源码

| 这里 | new-api |
|---|---|
| `relay()` 路由 | `relay/relay.go` —— 把入站请求派发到上游的入口 |

新版本把这个单一路由泛化成 `Adaptor` 接口(`relay/channel/openai/adaptor.go`——按 channel 的适配器,负责构造出站请求并解析回包),每个厂商一套实现。s04 我们会走到同样的设计。

## 取舍

明确**没有**做的事:

- **没有鉴权**。能访问端口的人就能花你的 key。→ s05。
- **没有流式**。`r.json()` 等整个响应回来,逐 token 输出不可能。→ s03。
- **单上游**。一个 `FORWARD_TARGET`,没有 channel 表、没有权重、没有故障切换。→ s10、s13。
- **没有协议转换**。请求体原样转发,所以调用方必须已经会说上游方言。→ s02、s04。
- **没有配额/日志/指标**。→ s07、s11、s16。
- **每次新建连接池**。正确,但慢;长连接客户端才是生产答案。

## 下章预告

`s01` 的 `/relay` 是我们自创的路径,LLM 生态已经统一讲 OpenAI 的 `chat.completions`。s02 把路由改成 `/v1/chat/completions`,免费拿到所有 OpenAI SDK。
