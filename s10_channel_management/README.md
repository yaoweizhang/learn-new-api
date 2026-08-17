# s10: 渠道管理(管理员 CRUD + 优先级/权重选路) — 渠道表 + 优先级 + 加权随机,流量分摊

> Previous: [s09](../s09_user_system/) · Next: [s11](../s11_call_logs/)

> *"priority + weight 排序"* —— 选路只是排序加过滤。

> **Layer**：L4 路由与韧性

## 问题

之前所有章节里,我们的上游都是写死在代码里的:要么 `s04_multi_provider` 里用一个简单的 if/elif 把 `model` 前缀映射到 base_url,要么 `s05` 用一张内存表把 API key 和用户绑死。一旦系统要对外服务,立刻就遇到三个问题:

1. **没法动态加渠道**。接一个新上游(比如一个新的 Azure 部署)必须改代码、重启进程,业务方毫无自主权。
2. **没法做容灾**。一个渠道挂了,所有请求全部失败——既没有备选渠道,也没有"降级到次选"的策略。
3. **没法区分优先级**。生产里同一个模型通常有多个上游(主账号 + 备用账号、Azure + 自建),它们的优先级和配额权重各不相同;写死的代码表达不了。

所以需要一张"渠道表"（每个 `channel`（new-api 里的"上游通道"：一条独立的 LLM 厂商接入配置））:每个渠道记录 provider、base_url、`weight`（权重：同优先级内越大越优先）、`priority`（优先级：数字越小越优先），由管理员通过 HTTP 增删改查,注册后立刻生效;路由层在调用上游前从这张表里"按规则选一个"。

## 本章要做什么

现在场景是:之前所有章节里,我们的上游都是写死在代码里的:要么 `s04_multi_provider` 里用一个简单的 if/elif 把 `model` 前缀映射到 base_url,要么 `s05` 用一张内存表把 API key 和用户绑死。一旦系统要对外服务,立刻就遇到三个问题:没法动态加渠道、没法做容灾、没法区分优先级。要解决这个——**我们把这层配置搬到一张管理员可改的内存渠道表**(**渠道表 / Channel 表**(每个 channel 是 new-api 里"一条独立的上游通道":一份 provider + base_url + weight + priority 配置,管理员通过 HTTP 增删改查,注册后立刻生效;选路时从这张表里按规则挑一条)——多渠道 + 选路,同一客户端就能跨多账号。本章把这张表和选路算法写出来:

1. **写一张内存渠道表 `channels.py` —— 为什么是内存表先于数据库**: `Channel` 是 `@dataclass`,字段 `id / name / provider / base_url / weight / priority / enabled / healthy`;`_channels: dict[int, Channel]` + `threading.Lock` 保护并发读写;公开函数只有 `reset_channels / create_channel / list_channels / get_channel / mark_unhealthy / pick_channel_for`。**为什么不先接 SQLite**:渠道是低频变更的运营数据,先用进程内 dict 把"注册即生效"的契约做出来,等 s12 切到持久化一并迁移——先把"动态配置"这件事讲透,不要让数据库分心。
2. **`pick_channel_for(model_name)` 三步算法 —— 为什么是 priority 优先于 weight**: 先按 `enabled and healthy and provider == _provider_for_model(model_name)` 过滤;然后取最小 `priority`(数字越小越优先,`priority=0` 是最高档);最后在档内按 `weight` 做 `random.choices(..., k=1)[0]` 加权随机。**为什么 priority 先于 weight**: priority 是"主备层级",weight 是"同档内分摊"——主账号全挂之前,备用账号即使 weight=1000 也不该接流量;反过来同档内若按 first-fit,所有请求都会落到最高 weight 那条,其它渠道闲着。
3. **挂两条管理员路由 `/admin/channels` —— 为什么先 CRUD 不接转发**: `POST /admin/channels` 注册渠道、`GET /admin/channels` 列出。**为什么不直接接 `/v1/chat/completions`**:本章要演示的是"动态注册 + 选路算法",把转发层一起拉进来会让 diff 翻倍,选路 bug 和转发 bug 会混在一起排查——`pick_channel_for` 的契约和"用这条渠道去打上游"的契约分开讲更清楚。
4. **鉴权闸门 `_require_admin` —— 为什么用 `dependencies=[...]` 列表形式**: 沿用 s09 的 JWT,自己额外要求 `claims["is_admin"]` 必须为 True,否则 403。**为什么不用 typed parameter**: `_require_admin` 自己完成"读 header → 解码 → is_admin 检查"一整条链路,handler 函数本身只关心业务——闸门用法挂在 `dependencies=[Depends(_require_admin)]` 上更干净,也跟 s08 之前 `request.state.principal` 的模式保持分离。

成品:`POST /admin/channels` 注册 `openai-primary`(weight=100, priority=0)和 `openai-backup`(weight=50, priority=1),后续 `pick_channel_for("gpt-4o-mini")` 会先把 `provider="openai"` 滤出来,再挑最低 priority 档(`openai-primary`),档内按 weight 加权随机(全 weight=0 时回退 round-robin,避免 `random.choices` 全零报错);`mark_unhealthy(cid)` 立即让该渠道被选路跳过。后续 s11 在每次请求时调 `pick_channel_for` 把调用日志落到渠道名;s13 把失败和 `mark_unhealthy` 接成"自动回血"回路。

## 方案

现在的场景是:`## 问题` 提了三件痛——没法动态加渠道 (痛点 #1)、单渠道挂全挂没法做容灾 (痛点 #2)、没法区分主备层级与配额权重 (痛点 #3)——这三件事**任何一件**都没法靠"客户端按厂商分流"或"运维改代码重启"能解决,必须由管理员通过 HTTP 往一张渠道表(**渠道表 / Channel 表**——本章第一次提到这个术语:管理员可动态增删改查的、用于选路的多家上游条目集合)里注册条目、路由层按规则选路。

**要解决这个——我们在网关里引入一个最小可用的渠道注册中心,分两部分**:

- **`s10_channel_management/channels.py`** — 内存表 + 选路算法。`Channel` 是一个 `@dataclass`,字段:`id / name / provider / base_url / weight / priority / enabled / healthy`。`create_channel / list_channels / get_channel / mark_unhealthy / pick_channel_for` 是仅有的几个公开函数。所有读写都在 `threading.Lock` 保护下进行;进程内全局单例。
- **`s10_channel_management/code.py`** — FastAPI 装配。挂载 s09 整块 app,在自己身上新增两条管理员路由:`POST /admin/channels`、`GET /admin/channels`。鉴权沿用 s09 的 JWT;用 `Depends(_require_admin)` 闸门把关,非管理员一律 `403 admin only`。

下面这幅图把上面三件痛各放到一个角色里:

- **`Client` (任意 OpenAI 客户端)** —— 在装上渠道表之前,这是被迫按厂商分流改代码的角色;装上之后,这事被中继隔走——客户端只认 `/v1/chat/completions`,它根本不知道有几条渠道。
- **`Relay` (本章要写的渠道表 + 选路算法)** —— 把痛点 #1 #2 #3 的解决动作集中放在这里:`POST /admin/channels` 注册新渠道、`_channels: dict` 存所有渠道、`pick_channel_for(model)` 按 `(provider 匹配 → 最小 priority 档 → 档内 weight 加权随机)` 三步挑一条。Client 只发请求,Upstream 只接请求,选路细节藏在中继里。
- **`Pool` (内存 `_channels: dict[int, Channel]`)** —— 本章新引入的运行时配置存储。每个 `Channel` 记录 `provider / base_url / weight / priority / enabled / healthy`,管理员通过 HTTP 增删改查,选路时按规则遍历;`mark_unhealthy(cid)` 立即把某条渠道踢出下次选择,挂载链上游不需要知道这件事。
- **`Upstream` (LLM 厂商,可选多家)** —— 服务提供方。它不直接面对客户端,也不直接面对管理员——管理员把渠道条目写进 Pool,选路算法从中继发出请求时挑一家厂商转发过去。Client 看不见选路细节,Upstream 也不知道自己是被挑中的哪一条。

路由形状——下面这张块状路由表把本章要写的 2 条管理员接口压成一览:左是 `method + path`,中间是 header 必带 `Authorization: Bearer <admin jwt>`,右是返回码与返回体;本章要写的核心就是"注册 + 列出"两个动作:

```
POST /admin/channels   Authorization: Bearer <admin jwt>   -> 201 {id, name}
GET  /admin/channels   Authorization: Bearer <admin jwt>   -> 200 [Channel...]
```

注册后立刻可用——`pick_channel_for(model_name)` 从已注册渠道里按 provider 过滤、取最低优先级档、档内按 `weight` 加权随机(同 `weight=0` 时回退 round-robin)。**s10 暂时不接 `/v1/chat/completions`**——把"渠道表注册好"和"用渠道表转发请求"分开是这一章的关键简化,留到后续章节接。理由很简单:本章要演示的是"动态注册 + 选路算法",把转发层一起拉进来会让 diff 翻倍,而且选路 bug 和转发 bug 会混在一起排查。

## 工作原理

**原理**: 一个 chat 请求从客户端进来之前,管理员要先通过 HTTP 往一张渠道表里注册多家厂商;注册后立即生效——一次 `pick_channel_for(model_name)` 选路算法的生命周期是: `_provider_for_model(model_name)` 决定该 model 走哪个 provider → 按 `enabled and healthy and provider == ...` 三条件过滤候选 → 取 `min(c.priority)` 决定档位 → 档内按 `weight` 做 `random.choices(..., k=1)[0]` 加权随机(全 0 权重时回退 round-robin)。注册路径由 `POST /admin/channels` 配 `_require_admin` 闸门把关,闸门后 handler 调 `channels.create_channel(...)` 把 `Channel` 写进 `_channels: dict[int, Channel]`,全程 `threading.Lock` 原子。整章所有部件都为"动态配置 + 三步选路"这条主线服务。

**1. 一个 channel registry (`channels.py`,进程内 `dict[int, Channel]` + `threading.Lock`)** —— `_channels: dict` 存所有渠道,`@dataclass Channel` 字段 `id / name / provider / base_url / weight / priority / enabled / healthy`,`_next_id` 自增。所有读写都在 `_lock` 下原子——单进程多线程够用,s12 切到 Postgres 之后再说。公开函数只有 `reset_channels / create_channel / list_channels / get_channel / mark_unhealthy / pick_channel_for` 六个。

**2. 一个 pick_channel_for selector (`channels.py`,三步算法)** —— 第一步按 `enabled and healthy and provider == _provider_for_model(model_name)` 过滤候选——同 model 只在能服务它的 provider 里挑;第二步取 `min(c.priority)` 决定档位(`priority` 数字越小越优先,`priority=0` 是最高档);第三步档内按 `weight` 做 `random.choices(..., k=1)[0]` 加权随机——主账号全挂之前,备用账号即使 `weight=1000` 也不该接流量。所有渠道 `weight=0` 时回退 `random.randrange(len(tier))` 的 round-robin,避免 `random.choices` 在全 0 权重上报错。`mark_unhealthy(cid)` 把 `ch.healthy` 置 False,下一次自动跳过。

**3. 一个 admin REST surface (`code.py`,两条 `/admin/channels` 路由)** —— `POST /admin/channels`(创建)与 `GET /admin/channels`(列出),两道路由都用 `dependencies=[Depends(_require_admin)]` 把"读 header → 解码 JWT → 校验 `is_admin` 标记"闸门挂在上面。handler 函数只调 `channels.create_channel / list_channels` 把业务跑完,鉴权逻辑不混进业务代码。`app.mount("/", s09_app)` 放在最后——Starlette 按注册顺序匹配,本地路由先注册、mount 后挂,`/admin/channels` 不会落到挂载链。

### `channels.py`:内存注册中心

```python
@dataclass
class Channel:
    id: int
    name: str
    provider: str
    base_url: str
    weight: int
    priority: int
    enabled: bool = True
    healthy: bool = True

_lock = threading.Lock()
_channels: dict[int, Channel] = {}
_next_id = 1

def reset_channels() -> None:
    global _next_id
    with _lock:
        _channels.clear()
        _next_id = 1

def create_channel(name, provider, base_url, weight, priority) -> Channel:
    global _next_id
    with _lock:
        cid = _next_id
        _next_id += 1
        ch = Channel(id=cid, name=name, provider=provider, ...)
        _channels[cid] = ch
        return ch
```

`_channels` 是进程内字典,`_next_id` 自增 ID。所有公开函数都在 `_lock` 保护下读写——单进程多线程够用,s12 切到 Postgres 之后再说。

### `pick_channel_for`:选路算法

```python
def pick_channel_for(model_name: str) -> Channel | None:
    provider = _provider_for_model(model_name)
    if provider is None:
        return None
    with _lock:
        candidates = [
            c for c in _channels.values()
            if c.enabled and c.healthy and c.provider == provider
        ]
    if not candidates:
        return None
    min_priority = min(c.priority for c in candidates)
    tier = [c for c in candidates if c.priority == min_priority]
    weights = [max(c.weight, 0) for c in tier]
    if sum(weights) == 0:
        return tier[random.randrange(len(tier))]   # round-robin fallback
    return random.choices(tier, weights=weights, k=1)[0]
```

三步算法:

1. **过滤**:先按 `enabled and healthy and provider == pick_provider(model_name)` 滤一轮——同一 model 只在能服务它的 provider 里挑。
2. **取最低优先级档**:`min_priority` 决定这一档;`priority` 数字越小越优先(`priority=0` 是最高优先级),档外的不参与选择。
3. **档内加权随机**:档内按 `weight` 做 `random.choices(tier, weights=weights, k=1)[0]` 加权抽样。**所有渠道 `weight=0` 时**回退到 `random.randrange(len(tier))` 的 round-robin,避免 `random.choices` 在全 0 权重上报错。

`mark_unhealthy(cid)` 把某个渠道的 `healthy` 置 False,下一次 `pick_channel_for` 自动跳过——为 s13 的健康检查/重试预留接口,但本章**不实际起循环检测**(见取舍)。

### `code.py`:FastAPI 装配

```python
app = FastAPI(title="learn-new-api s10")

def _require_admin(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    try:
        claims = jwt_util.decode(auth.removeprefix("Bearer ").strip())
    except Exception:
        raise HTTPException(status_code=401, detail="invalid token")
    if not claims.get("is_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    return claims

@app.post("/admin/channels", status_code=201, dependencies=[Depends(_require_admin)])
def create_channel(body: ChannelIn):
    ch = channels.create_channel(...)
    return {"id": ch.id, "name": ch.name}

@app.get("/admin/channels", dependencies=[Depends(_require_admin)])
def list_channels():
    return channels.list_channels()

app.mount("/", s09_app)
```

两个细节:

1. **`dependencies=[Depends(_require_admin)]`** 把鉴权当闸门挂在路由上,处理器函数本身只关心业务。`_require_admin` 自己做完整的"header → 解码 → is_admin 检查"——所以用 `dependencies=` 列表形式而不是 typed parameter,和 brief 一致。
2. **`app.mount("/", s09_app)` 放在最后**。Starlette 按注册顺序匹配路由;如果 mount 先注册,`Mount("/")` 会吸收掉 `/admin/channels` 导致 404。把自己的 APIRoute 先注册、mount 后挂——`/auth/signup`、`/auth/login`、`/me`、`/admin/channels` 都能正确路由。

## 运行

```bash
# 起服务
python s10_channel_management/code.py        # PORT 默认 8010

# 拿一个管理员 token:手工签发一个 is_admin=true 的 JWT
python -c "from s09_user_system.jwt_util import issue; print(issue(0, 'admin@example.com', True))"
# -> eyJhbGciOiJIUzI1NiIs...

ADMIN=eyJhbGciOiJIUzI1NiIs...

# 注册一个渠道
curl -s -X POST http://localhost:8010/admin/channels \
  -H "authorization: Bearer $ADMIN" \
  -H 'content-type: application/json' \
  -d '{"name":"openai-primary","provider":"openai",
       "base_url":"https://api.openai.com","weight":100,"priority":0}'
# -> {"id":1,"name":"openai-primary"}

# 再注册一个低优先级备用
curl -s -X POST http://localhost:8010/admin/channels \
  -H "authorization: Bearer $ADMIN" \
  -H 'content-type: application/json' \
  -d '{"name":"openai-backup","provider":"openai",
       "base_url":"https://api.openai.com","weight":50,"priority":1}'

# 列出所有渠道
curl -s http://localhost:8010/admin/channels -H "authorization: Bearer $ADMIN"
```

确认 channels CRUD 接口都能跑?打上面这套 curl——`POST /admin/channels` 回 `201 + {id, name}` 说明 `_require_admin` 闸门、`channels.create_channel` 写入、`_next_id` 自增 + `_channels` dict 在响应;`GET /admin/channels` 列出两条说明 `channels.list_channels` 也在响应;**注意 Bearer 用的是 s10 手工签发的 admin JWT**,s09 的 `/auth/signup` 默认 `is_admin=False`,管理员权限要单独获取:

1. 直接调 `users.create_user(email, hash, is_admin=True)`——给运维用。
2. 用 `s09_user_system.jwt_util.issue(user_id, email, is_admin=True)` 手工签一个——和测试用例 `_admin_token()` 同源,仅适合开发。

## → new-api 源码

真实部署里同样的"渠道表 + 管理员 CRUD"长这样:

- `controller/channel.go` —— 管理员路由的实现:list/add/update/delete/test 等 handler;负责把 HTTP 请求翻译成 `model.Channel` 上的方法调用,再把数据库行转回 JSON。和我们这里的 `_require_admin` + `ChannelIn` + `channels.create_channel` 完全对应。
- `model/channel.go` —— `Channel` struct 定义 + GORM 映射 + 钩子：字段远多于我们这里的 `Channel`（多了 `key / base_url / group / model / model_mapping / channel_balance / status / ...`），但核心三元 `priority / weight / enabled` 一一对应。

> 上面两个文件名在 GitHub 上是 `controller/channel.go`、`model/channel.go`(小写)。Windows 文件系统不分大小写,本地 IDE 里看着像 `Channel.go` 也常见;Linux/macOS 部署时按小写路径访问。

## 本章不做什么

- **没有健康检查循环 / 重试 / 降级** (定时 ping / 自动回血 / 失败重试回路)——YAGNI。本章只解决"渠道表存在、管理员能增删改查、按规则选一条"这三件事。`mark_unhealthy` 接口已经留好,后续章节(s13)接上后台 goroutine:每次请求前 ping 一遍,连续失败 N 次自动 `mark_unhealthy`,选路自动跳过;连续成功 M 次再恢复。
- **没有 `/admin/channels/{id}` 删除/更新接口** —— brief 只要求 POST + GET。new-api 那边有完整的 update/delete,但管理员工具的最低闭环(创建 + 列出)已经够演示"动态配置"的意义。
- **没有 chat 端点的转发层** —— 本章**不**接 `/v1/chat/completions`,渠道注册与选路算法分开讲更清楚。→ s11 在选路算法外面包日志中间件,s13 把 chat 端点提到本地用 `pick_channel_for` 触发"失败 → 切下一条"回路。
- **没有按 model 的渠道筛选 UI** —— 选路内部按 `_provider_for_model(model_name)` 自动分派,管理员注册时填对 `provider` 字段就行;没有专门的 model ↔ channel 关联表。
- **没有 API key 校验** (channel 自带的 key / 用户 key vs 渠道 key 的拆解)——本章假设渠道对应的厂商 key 已经准备好(`UPSTREAM_*_KEY` 环境变量到位),渠道表里只存 `base_url` / `provider` / 选路元数据,不存 key。生产里 channel 表会带 `key` 字段 + key 池子(`channel_balance`),s_full 才讲到。

## 已知限制

- **没有持久化** (进程内 dict,重启即失)——`_channels` 是进程内 `dict`,进程一重启渠道表清零。和 s09 的 users 表不同,本章不引入 SQLite。生产里渠道是低频变更的运营数据,本来就该走数据库;s12 切 Postgres 时一并接上。
- **`Mount("/")` 注册顺序敏感** (Starlette 按注册顺序匹配路由)——见上文"工作原理"第 1 步,本地路由先于 `app.mount("/", s09_app)` 注册。读者如果自己照着改代码、把 mount 挪到前面,会立刻撞到 404。这一点在 code.py 注释里已标注;在 README 里保留是为了让"修改顺序前先想清楚"成为可见的工程实践。
- **`pick_channel_for` 是教学最小集** —— 实际是 priority asc + 档内 weighted random(`random.choices` 选一条),`weight=0` 时回退 round-robin。new-api 的完整 `GetRandomSatisfiedChannel` 还做按 model 名分桶、按 group 分组、按 status 屏蔽等更细的事;本章只演示"档内加权分发"这一核心思想。
- **`candidates` 顺序是注册顺序** (按渠道注册先后遍历)——`list_channels()` 出来不按 priority 排序,priority 只在 `pick_channel_for` 内部重排。本章 channel 数量小(< 10 条)够用;数量上来后应该在外面先 sort 再遍历。
- **`threading.Lock` 不跨 worker** (单进程锁,多 worker 进程下不互斥)——`asyncio` 单 worker 部署够用,但多 worker 时每个 worker 各持一份 `_channels`,管理员注册的渠道对其他 worker 不可见;上 Redis 共享 + Pub/Sub 广播是后续优化项。

## 设计选择

- **`is_admin` 检查放在 `_require_admin` 里而非 typed parameter** —— 因为 typed parameter 在 FastAPI 里对"非平凡读"的依赖注入不友好;`dependencies=[Depends(_require_admin)]` 是闸门用法,更合适,也跟 s08 之前的 `request.state.principal` 模式保持分离。
- **`priority` asc 而非 desc** (数字越小越优先 / vs 数字越大越优先)——`priority=0` 是最高档,`priority=1` 是次高档;运维直觉是"数字小 = 重要",和 Linux nice 值语义一致;代价是写配置文件时少有人填 0,要靠文档约定。
- **`weight=0` 时回退 round-robin 而不是报错** ——避免 `random.choices` 在全 0 权重上报错;新注册未填 weight 的渠道默认走轮转,不影响可用性。
- **进程内 dict 而非 SQLite 起步** ——渠道是低频变更的运营数据,先用进程内 dict 把"注册即生效"的契约做出来;等 s12 切到持久化时一并迁移,不在本章分心讲数据库。

## 下章预告

s10 解决了"挑通道",但每次调用有没有发生、在哪失败、谁打的——全靠脑补。s11 加调用日志,中间件透明落盘。