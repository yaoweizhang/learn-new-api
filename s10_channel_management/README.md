# s10: 渠道管理（管理员 CRUD + 优先级/权重选路）

> Previous: [s09](../s09_user_system/) · Next: [s11](../s11_call_logs/)

## 问题

之前所有章节里，我们的上游都是写死在代码里的：要么 `s04_multi_provider` 里用一个简单的 if/elif 把 `model` 前缀映射到 base_url，要么 `s05` 用一张内存表把 API key 和用户绑死。一旦系统要对外服务，立刻就遇到三个问题：

1. **没法动态加渠道**。接一个新上游（比如一个新的 Azure 部署）必须改代码、重启进程，业务方毫无自主权。
2. **没法做容灾**。一个渠道挂了，所有请求全部失败——既没有备选渠道，也没有"降级到次选"的策略。
3. **没法区分优先级**。生产里同一个模型通常有多个上游（主账号 + 备用账号、Azure + 自建），它们的优先级和配额权重各不相同；写死的代码表达不了。

所以需要一张"渠道表"：每个渠道记录 provider、base_url、weight（权重）、priority（优先级），由管理员通过 HTTP 增删改查，注册后立刻生效；路由层在调用上游前从这张表里"按规则选一个"。

## 方案

引入一个最小可用的渠道注册中心，分两部分：

- **`s10_channel_management/channels.py`** — 内存表 + 选路算法。`Channel` 是一个 `@dataclass`，字段：`id / name / provider / base_url / weight / priority / enabled / healthy`。`create_channel / list_channels / get_channel / mark_unhealthy / pick_channel_for` 是仅有的几个公开函数。所有读写都在 `threading.Lock` 保护下进行；进程内全局单例。
- **`s10_channel_management/code.py`** — FastAPI 装配。挂载 s09 整块 app，在自己身上新增两条管理员路由：`POST /admin/channels`、`GET /admin/channels`。鉴权沿用 s09 的 JWT；用 `Depends(_require_admin)` 闸门把关，非管理员一律 `403 admin only`。

路由形状：

```
POST /admin/channels   Authorization: Bearer <admin jwt>   -> 201 {id, name}
GET  /admin/channels   Authorization: Bearer <admin jwt>   -> 200 [Channel...]
```

注册后立刻可用——`pick_channel_for(provider)` 从已注册渠道里按 `(priority asc, weight desc, healthy=True)` 排序取第一条。**s10 暂时不接 `/v1/chat/completions`**——把"渠道表注册好"和"用渠道表转发请求"分开是这一章的关键简化，留到后续章节接。理由很简单：本章要演示的是"动态注册 + 选路算法"，把转发层一起拉进来会让 diff 翻倍，而且选路 bug 和转发 bug 会混在一起排查。

## 工作原理

### `channels.py`：内存注册中心

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

`_channels` 是进程内字典，`_next_id` 自增 ID。所有公开函数都在 `_lock` 保护下读写——单进程多线程够用，s12 切到 Postgres 之后再说。

### `pick_channel_for`：选路算法

```python
def pick_channel_for(model_prefix: str) -> Channel | None:
    with _lock:
        candidates = [c for c in _channels.values() if c.enabled and c.healthy]
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c.priority, -c.weight))
    return candidates[0]
```

排序键是 `(priority, -weight)`：

- `priority` 小的在前（priority=0 是最高优先级）。
- 同优先级内，`weight` 大的在前（`weight=100` 比 `weight=10` 优先）。

只选 `enabled=True` 且 `healthy=True` 的渠道；没有候选返回 `None`。`mark_unhealthy(cid)` 把某个渠道的 `healthy` 置 False，下一次 `pick_channel_for` 自动跳过——为 s13 的健康检查/重试预留接口，但本章**不实际起循环检测**（见取舍）。

### `code.py`：FastAPI 装配

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

两个细节：

1. **`dependencies=[Depends(_require_admin)]`** 把鉴权当闸门挂在路由上，处理器函数本身只关心业务。`_require_admin` 自己做完整的"header → 解码 → is_admin 检查"——所以用 `dependencies=` 列表形式而不是 typed parameter，和 brief 一致。
2. **`app.mount("/", s09_app)` 放在最后**。Starlette 按注册顺序匹配路由；如果 mount 先注册，`Mount("/")` 会吸收掉 `/admin/channels` 导致 404。把自己的 APIRoute 先注册、mount 后挂——`/auth/signup`、`/auth/login`、`/me`、`/admin/channels` 都能正确路由。

## 运行

```bash
# 起服务
python s10_channel_management/code.py        # PORT 默认 8010

# 拿一个管理员 token：手工签发一个 is_admin=true 的 JWT
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

管理员 token 怎么来？s09 现在的 `/auth/signup` 默认把 `is_admin` 设成 False；拿到 admin 权限的两种方式：

1. 直接调 `users.create_user(email, hash, is_admin=True)`——给运维用。
2. 用 `s09_user_system.jwt_util.issue(user_id, email, is_admin=True)` 手工签一个——和测试用例 `_admin_token()` 同源，仅适合开发。

## 测试

```bash
pytest tests/test_s10_channel_management.py -v
```

九个测试覆盖关键契约，分两组：

**管理员鉴权**（handler 层的 401/403 路径）：

| 测试 | 断言 |
| --- | --- |
| `test_admin_can_create_channel` | 管理员带合法 JWT POST `/admin/channels` 返回 201。 |
| `test_non_admin_cannot_create_channel` | 普通用户带合法 JWT POST `/admin/channels` 返回 403。 |

**`pick_channel_for` 选路算法**（按 `(priority, weight, healthy)` 规则从渠道表里挑一条）：

| 测试 | 断言 |
| --- | --- |
| `test_pick_channel_for_returns_none_when_empty` | 渠道表为空时返回 `None`，不抛异常。 |
| `test_pick_channel_for_returns_none_for_unknown_model` | 渠道表非空但没有任何渠道能服务该模型时返回 `None`。 |
| `test_pick_channel_for_filters_by_provider` | 按 `model` 前缀筛掉 provider 不匹配的渠道（`gpt-*` 不会落到 claude 渠道，反之亦然）。 |
| `test_pick_channel_for_skips_unhealthy_and_disabled` | `mark_unhealthy` 标记的渠道被跳过，即使 `weight` 更高。 |
| `test_pick_channel_for_picks_lowest_priority_first` | `priority` 比 `weight` 优先——低优先级小权重要胜过高优先级大权重。 |
| `test_pick_channel_for_distributes_load_by_weight` | 同优先级内按 `weight` 加权随机分配（用 monkey patch `random.choices` 让结果可重现），跑 200 次两个渠道至少各被选中一次。 |
| `test_pick_channel_for_handles_zero_weights` | 同优先级内所有渠道 `weight=0` 时回退到 round-robin，不抛 `random.choices` 异常。 |

`_clean` fixture 在每个测试前后调用 `reset_channels()`，保证渠道表不串。

## → new-api 源码

真实部署里同样的"渠道表 + 管理员 CRUD"长这样：

- `controller/channel.go` —— 管理员路由的实现：list/add/update/delete/test 等 handler；负责把 HTTP 请求翻译成 `model.Channel` 上的方法调用，再把数据库行转回 JSON。和我们这里的 `_require_admin` + `ChannelIn` + `channels.create_channel` 完全对应。
- `model/channel.go` —— `Channel` struct 定义 + GORM 映射 + 钩子：字段远多于我们这里的 `Channel`（多了 `key / base_url / group / model / model_mapping / channel_balance / status / ...`），但核心三元 `priority / weight / enabled` 一一对应。

> 上面两个文件名在 GitHub 上是 `controller/channel.go`、`model/channel.go`（小写）。Windows 文件系统不分大小写，本地 IDE 里看着像 `Channel.go` 也常见；Linux/macOS 部署时按小写路径访问。

## 取舍

- **没有健康检查循环 / 重试 / 降级** —— YAGNI。这一章只解决"渠道表存在、管理员能增删改查、按规则选一条"这三件事。`mark_unhealthy` 接口已经留好，s13 会接上后台 goroutine：每次请求前 ping 一遍，连续失败 N 次自动 `mark_unhealthy`，选路自动跳过；连续成功 M 次再恢复。
- **没有持久化** —— 进程一重启渠道表清零；和 s09 的 users 表不同，本章不引入 SQLite。生产里渠道是低频变更的运营数据，本来就该走数据库；s12 切 Postgres 时一并接上。
- **没有 `/admin/channels/{id}` 删除/更新接口** —— brief 只要求 POST + GET。新-api 那边有完整的 update/delete，但管理员工具的最低闭环（创建 + 列出）已经够演示"动态配置"的意义。
- **`pick_channel_for` 只用 weight 排序，没有按权重加权随机** —— 同优先级内 `weight=100` 永远压过 `weight=50`，所以 `weight` 在这里实际上扮演"次级优先级"的角色。真正的"加权随机轮询"留到 s11/s13 再做。
- **`Mount("/")` 注册顺序** —— 见上文"工作原理"。读者如果自己照着改代码、把 mount 挪到前面，会立刻撞到 404；这一点在注释里已经标注。
- **`is_admin` 检查放在 `_require_admin` 里而非 typed parameter** —— 因为 typed parameter 在 FastAPI 里对"非平凡读"的依赖注入不友好；`dependencies=[Depends(...)]` 是闸门用法，更合适。s08 之前的 `request.state.principal` 模式已被替代。