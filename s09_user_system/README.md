# s09: 用户系统（bcrypt + JWT）

> Previous: [s08](../s08_rate_limiting/) · Next: [s10](../s10_channel_management/)

> *"注册即有 JWT"* —— 用户系统 = 发证 + 验证。

> **Layer**：L2 鉴权与身份

> **术语速读**：本章用 `bcrypt`（专为密码哈希设计的慢哈希算法）做密码存储，登录成功后用 `HS256`（JWT 的一种签名算法，对称密钥）签发 `JWT`（JSON Web Token：把用户信息签名后塞进字符串）—— 三者后续章节直接复用，不再重复解释。

## 问题

s05 之前我们用一张"API key → 用户"的内存表来做鉴权。这种做法在演示阶段没问题，但只要系统对外公开就立刻遇到三个痛点：

1. **没有真正的账号**。用户必须由管理员在服务器上手工签发 key；用户自己无法注册、无法改密码、无法找回。
2. **密码无处存放**。`s05_api_key_auth/storage.py` 里 key 是明文的，把它当成密码等于把数据库泄露出去。
3. **状态是临时的**。进程一重启，所有用户和配额一起蒸发。

说白了，我们现在缺的不是一个新的鉴权机制，而是一张真正的"用户表"——邮箱注册、密码哈希、签发令牌，再用这个令牌代替原来的明文 key 去访问 `/v1/chat/completions`。

## 方案

引入四个最小但够用的部件：

- **SQLite 用户表**（`s09_user_system/users.py`）—— 标准库 `sqlite3`，不引入 ORM。字段：`id / email / password_hash / is_admin / created_at`。`email` 唯一约束，保证重复注册会被拒绝。
- **bcrypt 密码哈希**—— 注册时 `bcrypt.hashpw`，登录时 `bcrypt.checkpw`。永远不存明文，永远不直接比较。
- **HS256 JWT**（`s09_user_system/jwt_util.py`）—— 登录成功签发 `access_token`；`/me` 用 `Depends(_current_user)` 解码 claims。签名密钥来自环境变量 `JWT_SECRET`，默认 `"change-me-in-production"` 仅用于本地开发。
- **Token 黑名单**（`s09_user_system/token_blacklist.py`）—— 进程内集合，键为 `sha256(token).hexdigest()`。`_current_user` 在解码 JWT 之前先检查黑名单——JWT 是无状态的，所以"注销一个尚未到期的 token"必须靠显式 deny-list。

整章的路由形状如下——下面这张块状路由表把本章要写的 4 条接口压成一览：表左是 `method + path`，中间是入参，右是返回码与返回体；本章要写的核心就是这套"注册 → 登录 → 注销 → 读自己"的接口。

```
POST /auth/signup   {email, password}            -> 201 {id, email, access_token}
POST /auth/login    {email, password}            -> 200 {access_token, token_type}
POST /auth/logout   Authorization: Bearer <jwt>  -> 204   (把这个 token 加进黑名单)
GET  /me            Authorization: Bearer <jwt>  -> 200 {id, email, is_admin}
```

注销是**尽力而为**——已经被攻击者截获的旧 token 在被注销前仍然有效（JWT 验签只看签名 + exp，不查黑名单）。需要"立刻全量撤销"的话得上 refresh-token + 黑名单的组合，那是 v2 的事。

## 工作原理

### `users.py`：SQLite 存储

```python
DB_PATH = Path("/tmp/learn-new-api-users.db")
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""
```

- `_conn()` 每次调用都新建连接并执行 `executescript(SCHEMA)`——SQLite 没有服务端，连接很轻，这样写最简单。
- `reset_db()` 删文件；Windows 下文件可能被进程持有，所以回退成 `DELETE FROM users`，测试隔离仍然成立。
- `create_user` / `find_by_email` 是仅有的两个公开函数。

### `jwt_util.py`：最小 HS256 实现

```python
SECRET = os.getenv("JWT_SECRET", "change-me-in-production")

def issue(user_id, email, is_admin, ttl_seconds=3600):
    now = int(time.time())
    payload = {"sub": str(user_id), "email": email,
               "is_admin": is_admin, "iat": now, "exp": now + ttl_seconds}
    return jwt.encode(payload, SECRET, algorithm="HS256")
```

Payload 三个字段：`sub`（用户 id）、`email`、`is_admin`。`exp` 默认 1 小时，签名用 HS256，对称密钥足够本地学习用；生产环境换成 RS256 + 非对称密钥更合适。

### `code.py`：FastAPI 装配

```python
app = FastAPI(title="learn-new-api s09")
app.mount("/v1", s08_app)              # s08 的 /v1/chat/completions 原样保留

@app.post("/auth/signup", status_code=201)
def signup(creds: Credentials):
    if users.find_by_email(creds.email):
        raise HTTPException(409, "email already registered")
    pw_hash = bcrypt.hashpw(creds.password.encode(), bcrypt.gensalt()).decode()
    uid = users.create_user(creds.email, pw_hash)
    return {"id": uid, "email": creds.email,
            "access_token": jwt_util.issue(uid, creds.email, is_admin=False)}

def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing token")
    return auth.removeprefix("Bearer ").strip()

def _current_user(request: Request) -> dict:
    token = _bearer(request)
    if token_blacklist.get_default().is_revoked(token):
        raise HTTPException(401, "token revoked")
    try:
        return jwt_util.decode(token)
    except Exception:
        raise HTTPException(401, "invalid token")

@app.post("/auth/logout", status_code=204)
def logout(request: Request):
    token = _bearer(request)        # 401 if missing
    token_blacklist.get_default().revoke(token)
    return None

@app.get("/me")
def me(claims: dict = Depends(_current_user)):
    return {"id": int(claims["sub"]),
            "email": claims["email"],
            "is_admin": claims.get("is_admin", False)}
```

三条设计要点：`_bearer` 把"提取 token"抽出来一处，`_current_user` 在解码前先查黑名单（用 SHA-256 算出的摘要做 key，不是原始 token），`/auth/logout` 走 `_bearer` 复用同一份解析逻辑。共用 `_bearer` 看起来是小事，但少了它，"什么算合法 Bearer 头"就会在每个 handler 里被重复实现一遍，迟早某个分支写错。

登录失败统一返回 `401 invalid credentials`——不区分"邮箱不存在"和"密码错误"，避免被用来探测哪些邮箱已注册。注销后再访问 `/me` 返回 `401 token revoked`，跟"密码错"区分开——给客户端一个明确的"你的 token 被显式作废了"信号，而不是让它以为只是密码又错了。

## 运行

```bash
# 第一次：先 s08 准备好环境
export UPSTREAM_OPENAI_KEY=sk-...

# 起服务
python s09_user_system/code.py        # PORT 默认 8009
```

```bash
# 注册
curl -s -X POST http://localhost:8009/auth/signup \
  -H 'content-type: application/json' \
  -d '{"email":"a@b.com","password":"secret123"}'
# -> {"id":1,"email":"a@b.com","access_token":"eyJ..."}

# 登录
TOKEN=$(curl -s -X POST http://localhost:8009/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"a@b.com","password":"secret123"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# /me
curl -s http://localhost:8009/me -H "authorization: Bearer $TOKEN"
# -> {"id":1,"email":"a@b.com","is_admin":false}

# 注销（撤销这个 token）
curl -s -X POST http://localhost:8009/auth/logout \
  -H "authorization: Bearer $TOKEN"
# -> 204 No Content

# 之后再访问 /me 用同一个 token：
curl -i http://localhost:8009/me -H "authorization: Bearer $TOKEN"
# -> 401 {"detail":"token revoked"}
```

`JWT_SECRET` 强烈建议设置；不设置的话所有进程重启后旧 token 仍能用默认 `"change-me-in-production"` 验签——只是别把它带到线上。

## 测试

```bash
pytest tests/test_s09_user_system.py -v
```

七个测试覆盖主要契约：

| 测试 | 断言 |
| --- | --- |
| `test_signup_and_login_roundtrip` | 注册返回 201，登录返回 200，token 是合法 JWT（两点三段）。 |
| `test_login_with_wrong_password_fails` | 错误密码返回 401。 |
| `test_me_requires_token` | 没有 `Authorization` 头时 `/me` 返回 401。 |
| `test_me_returns_user_with_token` | 带合法 token 调用 `/me` 返回对应 email。 |
| `test_logout_revokes_token` | `/auth/logout` 后同一个 token 再访问 `/me` 返回 401 `token revoked`。 |
| `test_logout_without_token_returns_401` | 不带 Bearer 调用 `/auth/logout` 返回 401。 |
| `test_blacklist_check_isolated_per_token` | 撤销 token-A 不影响 token-B——验证 SHA-256 键隔离。 |

`reset_db()` 在 fixture 中先调用、后调用；token 黑名单由平行 fixture 重置，保证测试之间互不污染。

## → new-api 源码

- `controller/user.go` —— 注册、登录、注销等 HTTP 处理器；新-api 在这里把表单/JSON 绑定到 `model.User`，再交给 service。
- `model/user.go` —— `User` struct、密码哈希字段、`BeforeCreate` 钩子里调用 bcrypt；和我们这里的 `users.py` 一一对应。
- `service/user_notify.go` —— 通知/邮件相关扩展；本教程暂不涉及。

## 取舍

- **不做邮箱验证 / 找回密码 / 刷新令牌 / 注册限流** —— YAGNI。这一章只解决"能不能让用户登录"，剩下的留给 s10 之后按真实需求加。
- **JWT 存哪里** —— 我们让客户端自己持有 token（典型前端 localStorage 或移动端 keychain）。新-api 还有 server-side session 路径，复杂度更高、可见性更好，但需要一次额外的查表；本教程选无状态的 JWT。
- **HS256 + 对称密钥** —— 学习和本地开发足够；线上推荐 RS256 + JWKS，因为前端不能拿私钥验签。
- **SQLite 文件锁** —— 单进程够用；多 worker 时 `find_by_email` 会拿不同连接，存在"同一时刻读到两条刚 commit 的 row"的窗口，s12 之后切到 Postgres 再讨论并发。
- **默认密钥硬编码** —— 仅用于演示；`JWT_SECRET` 必须设置，至少 32 字节随机串。PyJWT 会发出 `InsecureKeyLengthWarning`，是预期内的。