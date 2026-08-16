# s14: 最简服务端渲染的管理后台(Jinja2 + 会话 Cookie) — Jinja2 + Cookie 认证,只读看板上线

> Previous: [s13](../s13_retry_fallback/) · Next: [s15](../s15_docker_deployment/)

> *"把 in-memory 表露出来"* —— admin UI 把内存状态暴露给运维。

> **Layer**：L5 运维与可观测

## 问题

到 s13,网关已经能跑——用户签到、配额扣减、渠道选路 + 重试 + 回退、缓存、日志都接好了。但**所有管理动作全靠 curl**:

- 想看现在跑了多少条渠道?`curl /admin/channels`
- 想看刚才哪条请求失败了?`curl /admin/logs`
- 想加一条新渠道?`curl -X POST /admin/channels -d '{...}'`
- 想改自己的密码?不好意思,得改库

admin 后台是**人用的界面**,不是给脚本用的。每次让人去拼 curl、看 JSON、再 `jq` 一下才能知道"系统现在怎样",运营成本就上来了。学习项目也希望有一个"看得见"的入口——点开浏览器就能看到系统状态。

new-api 自己有 React 写的完整 Web 后台(`web/` 目录),那是个正经的 SPA,跟 Go 后端走 REST。照搬那个,体量比后端还大——Vue/React 构建工具链、状态管理、路由、组件库、TypeScript 类型定义,光搭起来就够写三章。本章不碰那个体量。

## 本章要做什么

要解决这个,在网关内挂一个**最薄的服务端渲染后台**:浏览器 GET `/dashboard/login` 拿登录表单、POST 凭证拿 Cookie,GET `/dashboard/` 渲染"用户/渠道/日志数"三个数字。学完运营用浏览器就能看到系统状态,不用 curl + jq。本章把这套最小看板写出来:

1. **挂一个 Jinja2 仪表盘 —— 为什么服务端渲染不写 React SPA**: new-api 自带完整 React SPA (`web/` 目录,Vite + TypeScript + Zustand + Tailwind),那个体量比后端还大。**为什么不抄**: 教程目的是演示"网关能渲染 HTML"这件事的最小形态——Vue/React 构建工具链、状态管理、路由、组件库、TypeScript 类型定义,光搭起来就够写三章;Jinja2 + 3 个数字足够。**为什么用 Jinja2**: FastAPI 官方 `Jinja2Templates` 内置,F-string 模板拼字符串容易 XSS,服务端渲染对运维读看板这种只读场景最自然。

2. **表单登录 + 明文 Cookie session —— 为什么不上 JWT**: `@app.post("/dashboard/login")` 接 `Form(email, password)`,校验成功就 `RedirectResponse("/dashboard/")` + `set_cookie("admin", "1", httponly=True)`。Cookie 只是 `admin=1` 的明文标记——**没有签名、没有加密**。**为什么不直接用 s09 的 JWT**: admin JWT 复用 s09 那一套能跑,但 admin 是 `is_admin=1` 的特殊用户、要签发 + 校验,5-10 行额外书架代码;教学范围内明文 Cookie 让两段测试足够短;**生产里要么 `itsdangerous` 签名、要么直接复用 s09 的 JWT**——取舍里展开。

3. **`_require_admin` 守卫 + 401 而不是 302 —— 为什么手动调而不是 `Depends`**: 仪表盘 handler 第一行 `gate = _require_admin(request); if gate: return gate`,`_require_admin` 没 Cookie 时返回 `HTMLResponse("unauthorized", status_code=401)`。**为什么不是 302**: 重定向到 `/dashboard/login` 在生产里体验更好(浏览器自动跳登录页),但 Starlette `TestClient` 默认跟随重定向,302 + 跟随 → 200 会让断言 `status_code in (302, 401)` 失败。直接返 401 让 TestClient 停在原响应上,简化测试。**为什么手动调而不是 `Depends(_require_admin)`**: 401 响应不是 HTTPException、是手写的 HTMLResponse,`Depends` 配合自定义 Response 容易写绕;手动 5 行,更好读。

4. **数据复用直接 import 内存单例 —— 为什么是"看得到"不是"可编辑"**: 仪表盘三个数字从已有模块读:`channels = len(ch_mod.list_channels())`(s10 内存 dict)、`logs = len(log_store.list_logs())`(s11 异步 flush 后的 list),`users` 硬编码 0 因为 s09 没 `list_all()`。**为什么不写 CRUD UI**: 不展示用户列表、不支持改渠道、不支持分页筛选;那要列表分页 + 搜索 + 批量操作 + 暗色模式 + 表单校验——YAGNI;本章只展示"能看到数字"。**为什么不接数据库**: 进程重启回到初始状态是有意为之,本章是"看得到数字"的最小后台,不是"可编辑的 CRUD 后台"。

成品: 浏览器打开 `localhost:8014/dashboard/login` → 输入 `admin@example.com / admin`(默认本地凭证) → 登录后看到三个数字:Users: 0、Channels: N、Logs: M;老的 `/v1/chat/completions` 仍可达;`curl -i` 看 302 + `Set-Cookie: admin=1; HttpOnly`。后续 s15 把整套 Docker 化,s16 给后台看板加实时指标。

## 方案

`## 问题` 提了一件事:想看系统状态只能拼 `curl + jq`(痛点 #1 现状)。这件事**没法靠"s10 给 channels 加个 REST"或"s11 给 logs 加个 REST"能解决**——REST 端点还得用脚本去拼,只是把"运营不想学 curl"翻译成了"运营学 jq"——必须有一个浏览器开就能看到数字的页面,加上一个"我是管理员"的会话,只读露三条数据。下面这幅图把这件痛各放到四个角色里:

- **`Browser` (运营用的浏览器)** —— 在装上 dashboard 之前,这是被迫拼 `curl + jq` 的角色;装上之后,这事被 dashboard 隔走——浏览器只管 GET `/dashboard/`,`admin` Cookie 跟着走,看到三个数字就完事。
- **`Relay` (本章要写的 FastAPI + Jinja2)** —— 把痛点 #1 的解决动作集中放在这里:`@app.get("/dashboard/login")` 渲染 form、`@app.post("/dashboard/login")` 校验凭证 + 下发 `admin` Cookie、`@app.get("/dashboard/")` 验 Cookie + 渲染 `dashboard.html`。所有 dashboard 路由注册在 `app.mount("/", s13_app)` **之前**——Starlette 按注册顺序匹配,本地路由挡 mount,`/v1/chat/completions` 仍可达。
- **`Cookies` (浏览器会话侧, `admin=1` httponly 明文)** —— 鉴权载体。HTTPOnly 防止 JS 读(缓解 XSS),但**没有签名**(没有用 `itsdangerous` 之类签名 Cookie 的密钥验证机制),浏览器 DevTools 改值就能伪造管理员。YAGNI:教学范围内只要能区分"已登录 / 未登录"两种状态就够;生产里必须上签名或者直接复用 s09 的 JWT。
- **`Storage` (从 s10/s11 import 来的内存单例)** —— 数据来源。渠道数 `len(ch_mod.list_channels())`(s10 的 `_channels: dict`)、日志数 `len(log_store.list_logs())`(s11 的 `_flushed: list`)。**用户数硬编码 0**——s09 没 `list_all()` 接口,本章不为这一个数字去给 s09 加 SQL count;Browser 不直接读存储,Read 路径必须经 Relay 的 handler。

引入四个最小部件:

- **`s14_admin_dashboard/code.py`** —— 一个新的 FastAPI 实例,挂
  上 `/dashboard/login`(GET 渲染表单、POST 校验凭证并下发 Cookie)
  和 `/dashboard/`(GET 校验 Cookie 后渲染 Jinja2 模板)。最后
  `app.mount("/", s13_app)`,让 `/v1/chat/completions` 等老路由仍
  可达。
- **`templates/base.html` + `templates/dashboard.html`** —— Jinja2
  模板。`base.html` 是页面骨架(标题 + 导航条),`dashboard.html`
  继承它,渲染三个数字:用户数、渠道数、日志数。
- **Cookie session** —— 一个 httponly 的 `admin=1` Cookie。这个
  Cookie **没有签名、没有加密**——明文标记"我是管理员"。生产环境
  请用 `itsdangerous` 签名或 JWT;本章 YAGNI。
- **数据复用** —— 渠道数从 `s10_channel_management.channels
  .list_channels()` 读,活跃日志数从 `s11_call_logs.log_store
  .list_logs()` 读。**用户数保持 0**——s09 没有 `list_all()`,本章
  不为这一个数字去给 s09 加 SQL count(YAGNI)。

路由形状——下面这张块状路由表把本章要写的 4 条接口压成一览:左是 `method + path`,中间是入参(登录是表单 body),右是返回码与返回体;本章要写的核心就是"登录 + 仪表盘"两个 dashboard 路由 + 仍然挂着从 s13 来的一条转发接口:

```
GET  /dashboard/login              -> 200 HTML form
POST /dashboard/login              -> 302 (-> /dashboard/) + Set-Cookie, 失败 401
GET  /dashboard/                   -> 200 rendered dashboard, 未登录 401
GET  /v1/chat/completions          -> 仍可达(来自挂载的 s13)
```

## 工作原理

**原理**: 浏览器打开 dashboard 时,整个流程是: GET `/dashboard/login` → Relay 渲染登录表单 (服务端用 Jinja2Templates 把 `login.html` 拼成 HTML 字符串返回) → 浏览器填表 POST `/dashboard/login` → Relay 校验 `ADMIN_EMAIL` / `ADMIN_PASSWORD` 环境变量 → 通过则 `RedirectResponse("/dashboard/", status_code=302)` + `set_cookie("admin", "1", httponly=True)` → 浏览器带 Cookie GET `/dashboard/` → Relay 调 `_require_admin` 查 Cookie → 通过则 `TemplateResponse(request, "dashboard.html", {"stats": ...})` 渲染数字,失败则直接 `HTMLResponse("unauthorized", status_code=401)`。整章所有部件都为"服务端渲染 + 最小 cookie session"这条主线服务。

**1. 一个 Jinja2 templates 服务端渲染器 (`templates/base.html` + `templates/dashboard.html` + `Jinja2Templates(directory=...)`)** —— `dashboard.html` 继承 `base.html`(页面骨架),渲染三个数字(users / channels / logs)。**为什么用 Jinja2 不用 React SPA**: FastAPI 官方内置 `Jinja2Templates`,服务端渲染对运维读看板这种只读场景最自然;new-api 的 `web/` React SPA (new-api 自带的前端,Vite + TypeScript + Zustand + Tailwind) 体积比后端还大,本教程不抄。

**2. 一个明文 cookie session (`admin=1` httponly)** —— 表单 POST `/dashboard/login` 时由 `set_cookie("admin", "1", httponly=True)` 下发;dashboard handler 第一行调 `_require_admin(request)` 校验 `cookies.get("admin") == "1"`。**为什么明文**: 教学范围内"区分登录 / 未登录两种状态"够用;签名 / 加密是它的反向——浏览器不可伪造,代价是 5-10 行额外书架代码 (用 `itsdangerous` 之类签名 Cookie 的库),超出"最小可运行"。

**3. 一个 admin CRUD handlers (`@app.get("/dashboard/")` + `_require_admin` 守卫 + 401)** —— 守卫没 Cookie 时直接 `HTMLResponse("unauthorized", status_code=401)`,**返 401 而非 302**: Starlette `TestClient` 默认跟随重定向,302 + 跟随 → 200 让断言失败;直接返 401 让 TestClient 停在原响应上,生产里应该 302(浏览器自动跳登录页)。**为什么不走 `Depends(_require_admin)`**: 401 响应不是 HTTPException、是手写的 HTMLResponse,`Depends` 配合自定义 Response 容易写绕。

**4. 一个数据复用 import (`from s10_channel_management import channels as ch_mod` + `from s11_call_logs import log_store`)** —— dashboard handler 直接调 `len(ch_mod.list_channels())` 和 `len(log_store.list_logs())` 读内存单例;**没有数据库查询**——重启进程回到初始状态,这是一个"看得到数字"的最小后台,不是"可编辑的 CRUD 后台"。用户数硬编码 0——s09 没 `list_all()`,不为一个数字加 SQL count。

### 登录：POST + Cookie

```python
@app.post("/dashboard/login")
def login_post(email: str = Form(...), password: str = Form(...)):
    if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
        resp = RedirectResponse("/dashboard/", status_code=302)
        resp.set_cookie("admin", "1", httponly=True)
        return resp
    return HTMLResponse("invalid", status_code=401)
```

凭证用环境变量注入（`ADMIN_EMAIL` / `ADMIN_PASSWORD`），默认
`admin@example.com` / `admin`——**仅供本地测试**。生产环境必须
从 secrets manager 读，并且密码要 bcrypt + 查表（s09 那一套），
不能放环境变量。

Cookie 只是 `admin=1` 的明文标记：**没有签名**。浏览器改个
`admin=1` 就能进去。生产里要么用 `itsdangerous.URLSafeTimed
Serializer` 签名 Cookie，要么直接用 s09 的 JWT——我们已经为用户
写过 JWT 了，admin JWT 复用同一套就行。

### 守卫：`_require_admin` + 401

```python
def _require_admin(request: Request):
    if request.cookies.get("admin") != "1":
        return HTMLResponse("unauthorized", status_code=401)
```

注意：返回的是 **401 而不是 302**。理论上应该 302 重定向到
`/dashboard/login`，但 Starlette 的 `TestClient` 默认会跟随重定
向——重定向到 `/dashboard/login`（200）后最终状态码是 200，断言
`status_code in (302, 401)` 会失败。直接返回 401 让 TestClient 停在
原响应上是简化测试。**生产里应该 302**——浏览器体验更好（自动跳
登录页）。

### 仪表盘：Jinja2 模板

```python
HERE = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))

@app.get("/dashboard/", response_class=HTMLResponse)
def dashboard(request: Request):
    gate = _require_admin(request)
    if gate:
        return gate
    stats = {
        "users": 0,
        "channels": len(ch_mod.list_channels()),
        "logs": len(log_store.list_logs()),
    }
    return templates.TemplateResponse(request, "dashboard.html", {"stats": stats})
```

`os.path.dirname(__file__)` 在 Windows 下返回 `D:\study\...\s14_admin
_dashboard`（带反斜杠），`os.path.join` 会正确处理——传给
`Jinja2Templates(directory=...)` 后内部用 `jinja2.FileSystemLoader`
打开，相对路径解析对正反斜杠都兼容。

`TemplateResponse` 的新签名（Starlette ≥ 0.30）是
`TemplateResponse(request, name, context)`——request 作为第一个位
置参数，name 和 context 跟上。旧签名
`TemplateResponse(name, {"request": request, ...})` 在新 Starlette
下会把字符串当 request、把 dict 当 name，导致 Jinja2 缓存键是
`dict`（不可哈希）而报错 `TypeError: unhashable type: 'dict'`。

### 路由顺序：本地路由挡挂载

```python
app = FastAPI(title="learn-new-api s14")

@app.get("/dashboard/login")        # 本地
@app.post("/dashboard/login")        # 本地
@app.get("/dashboard/")              # 本地

app.mount("/", s13_app)              # 挂载最后
```

Starlette 按注册顺序迭代路由。客户端打 `/dashboard/login`，本地
路由先匹配，根本不进 s13 → s12 → ... 那条挂载链。`/v1/chat/
completions` 反过来：本地没这条，落到挂载链，s13 自己 match 上。
这是 `s04_multi_provider` / `s05_api_key_auth` / `s13_retry_fallback` 都在踩的同一个 Starlette 坑——
**本地路由必须先注册，挂载必须最后**。

### 数据复用：直接 import 内存单例

```python
from s10_channel_management import channels as ch_mod
from s11_call_logs import log_store

stats = {
    "users": 0,                                # s09 没 list_all，硬编码 0
    "channels": len(ch_mod.list_channels()),    # s10 内存 dict
    "logs": len(log_store.list_logs()),         # s11 异步 flush 后的内存 list
}
```

`channels` 和 `log_store` 都是模块级单例（线程锁保护），dashboard
handler 直接调函数读当前状态。**没有数据库查询**——重启进程回到
初始状态。这是一个"看得到数字"的最小后台，不是"可编辑的 CRUD 后
台"。

## 运行

```bash
# 起服务（端口 8014）
ADMIN_PASSWORD=foo python s14_admin_dashboard/code.py

# 验证 dashboard 链路通了:登录拿 302 + Set-Cookie、带 Cookie GET /dashboard/ 拿 200 (渲染的 HTML 含 "Channels: 0" / "Logs: 0")——这能验证 cookie 下发 + 服务端渲染 + 数据 import 三段都到位:

# 浏览器打开
open http://127.0.0.1:8014/dashboard/login

# 输入 admin@example.com / foo → 登录
# 看到三个数字：Users: 0, Channels: <当前渠道数>, Logs: <已 flush 的日志数>

# 也可用 curl 验证
curl -i -c cookies.txt -X POST http://127.0.0.1:8014/dashboard/login \
  -d 'email=admin@example.com&password=foo'
# -> 302 + Set-Cookie: admin=1; HttpOnly

curl -b cookies.txt http://127.0.0.1:8014/dashboard/
# -> 200 HTML，body 里含 "Channels: 0" / "Logs: 0"

# 老的 chat 端点也仍可达
curl -X POST http://127.0.0.1:8014/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

## → new-api 源码

真实部署里同样的"管理后台"在 new-api 里长得完全不一样：

- **`web/`** —— 一个完整的 React SPA。Vite + TypeScript + React
  Router + Zustand + Tailwind，跟 Go 后端走 REST API（`/api/*
  路径）。页面包括 login、dashboard、channels、users、logs、
  settings、redeem 等十几个模块。体积比 Go 后端本体还大。
  本章的 Jinja2 + 3 个数字是它的**最小替身**——证明"网关自己能渲
  染 HTML"这件事可行，但生产里没人这么干。
- **`router/api-router.go`** —— 后端 REST 入口。`/api/channel/*`
  `/api/user/*`、`/api/log/*` 等都是真接口；前端从这些接口拉数
  据再 React 渲染。
- **`middleware/auth.go`** —— 真正的 admin 鉴权走 JWT 中间件
  + `IsAdmin` 标志位校验。本章的明文 Cookie 是 YAGNI；new-api
  这边 admin 和普通用户共用同一套 JWT，只是在 user 表里多一个
  `is_admin` 字段。

> Windows 文件系统不分大小写，本地 IDE 里看着像 `Web/` 不少见；
> 部署到 Linux/macOS 时按实际的小写路径 `web/` 访问。

## 本章不做什么

- **没有用户管理 UI (增删改查的浏览器 form 页面)** —— 不展示用户列表、不支持改密码、不支持
  封号。s09 已经实现了 `create_user` / `find_by_email`，但要
  展示成界面还有列表分页、搜索、批量操作、暗色模式……YAGNI。
  本章的 dashboard 只显示"用户数"（且硬编码 0，因为 s09 没
  `list_all`）。要展示真实用户数，给 s09 加一个
  `count_users() -> int` 即可。
- **没有渠道 CRUD UI** —— 不能在网页上加 / 删 / 改渠道。s10
  提供了 `/admin/channels` REST 端点，curl 仍可达；浏览器界面
  是 YAGNI。如果要，加一个 `templates/channels.html` + 三个
  handler（list / create / delete）就够。
- **没有日志筛选 UI** —— 日志列表不分页、不按用户过滤、不按
  状态码过滤。s11 `log_store.list_logs()` 直接返回所有已 flush
  的日志，超长 JSON 在浏览器里渲染很慢。生产里要加分页 + 过滤
  条件；本章 YAGNI。→ s_full 接 DB 后一并加。

## 已知限制

- **Cookie session 是明文、未签名** (没有密钥验证机制) —— 浏览器改 `admin=1`
  就能进。**不能上生产**。生产里要么用 `itsdangerous
  .URLSafeTimedSerializer` 签名 Cookie，要么直接复用 s09 的
  JWT（admin 是 is_admin=1 的特殊用户）。本章用明文 Cookie 是
  为了让两段测试足够短；加密 / 签名会引入 5-10 行额外的图书
  架代码，超出"最小可运行"的范畴。→ s_full 接真鉴权时一并修。
- **没有 CSRF 保护** (跨站请求伪造 token——防止已登录用户被诱导提交表单) —— POST `/dashboard/login` 不带 CSRF token。
  攻击者构造一个表单让已登录 admin 浏览器自动提交，配合社工
  能改 admin 密码——但本章还没"改密码"功能，CSRF 没东西可
  偷。生产里加 `csrf_protect` 中间件（fastapi-csrf-protect 等
  库），这是 s_full 加 CRUD form 后必须做的。
- **没用 FastAPI 的 `Depends` 注入守卫** —— `_require_admin` 是
  手动调一次再 `if gate: return gate`，5 行。换成 `Depends
  (_require_admin)` + `response.status_code` 之类的写法更
  FastAPI-native，但本场景里 401 响应不是 HTTPException，是手
  写的 HTMLResponse，`Depends` 配合自定义 Response 容易写
  绕。手动写法 5 行，更好读。
- **测试里 401 而不是 302** —— 浏览器场景下应该 302 重定向到
  登录页，但 `TestClient` 默认跟随重定向，302 + 跟随 → 200，
  断言 `status_code in (302, 401)` 会失败。**直接返 401** 让
  TestClient 不跟随，重定向到登录页留给浏览器自己去体验。
  这是一个为测试妥协的实现偏差——见 `_require_admin` 注释。
- **stats 字典每次现算** —— 每次 GET `/dashboard/` 都重新调
  `list_channels()` 和 `list_logs()`。渠道 / 日志量大了之后这
  会慢（O(n) 拷贝）。生产里要么缓存 60 秒、要么从 Prometheus
  拉 metrics。YAGNI。
- **没有日志登出** —— `admin` Cookie 没有过期时间（默认
  session cookie，关浏览器即失效）。按钮 log out 没做。
  YAGNI。
- **挂载顺序的隐式依赖** —— `app.mount("/", s13_app)` 必须在
  所有 `/dashboard/*` 路由**之后**。如果有人把 mount 提到
  前面，Starlette 会把 `/dashboard/*` 全部转给 s13 处理——s13
  没这些路由，会 404。代码里用注释 + 章节末尾的"挂载必须最后"
  强调，下一章起新挂载时务必检查。

## 设计选择

- **`os.path.dirname(__file__)` 取 templates 路径而非 `pathlib`** —— `os.path.join` 正反斜杠都兼容;`pathlib.PurePath.as_posix()` 在 Windows 拿到 `D:/...` 传进 `Jinja2Templates` 在某些版本下会触发 `FileNotFound` 错误。Windows 上正反斜杠兼容性这一条是 s14 在 Windows 上自测发现的小坑,笔记留下来。
- **`TemplateResponse(request, name, context)` 是 Starlette ≥ 0.30 新签名** —— request 作为第一个位置参数,name 和 context 跟上。旧签名 `TemplateResponse(name, {"request": request, ...})` 在新 Starlette 下会把字符串当 request、把 dict 当 name,导致 Jinja2 缓存键是 `dict`(不可哈希)而报错 `TypeError: unhashable type: 'dict'`。s14 直接用新签名,不踩这个坑。

## 下章预告

s14 看板有了,部署还是手动。s15 加 Docker + compose,一条命令起完整网关。
