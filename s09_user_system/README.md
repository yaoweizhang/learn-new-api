# s09: 用户系统（bcrypt + JWT）

## 问题

s05 之前我们用一张“API key → 用户”的内存表来做鉴权。这种做法在演示阶段没问题，
但只要系统对外公开就立刻遇到三个痛点：

1. **没有真正的账号**。用户必须由管理员在服务器上手工签发 key；用户自己
   无法注册、无法改密码、无法找回。
2. **密码无处存放**。`s05_api_key_auth/storage.py` 里 key 是明文的，
   把它当成密码等于把数据库泄露出去。
3. **状态是临时的**。进程一重启，所有用户和配额一起蒸发。

我们需要一个真实的“用户表”：邮箱注册、密码哈希、签发令牌，再用这个令牌
代替原来的明文 key 去访问 `/v1/chat/completions`。

## 方案

引入三个最小但够用的部件：

- **SQLite 用户表**（`s09_user_system/users.py`）—— 标准库 `sqlite3`，
  不引入 ORM。字段：`id / email / password_hash / is_admin / created_at`。
  `email` 唯一约束，保证重复注册会被拒绝。
- **bcrypt 密码哈希**—— 注册时 `bcrypt.hashpw`，登录时 `bcrypt.checkpw`。
  永远不存明文，永远不直接比较。
- **HS256 JWT**（`s09_user_system/jwt_util.py`）—— 登录成功签发
  `access_token`；`/me` 用 `Depends(_current_user)` 解码 claims。
  签名密钥来自环境变量 `JWT_SECRET`，默认 `"change-me-in-production"` 仅
  用于本地开发。

整章的路由形状如下：

```
POST /auth/signup   {email, password}            -> 201 {id, email, access_token}
POST /auth/login    {email, password}            -> 200 {access_token, token_type}
GET  /me            Authorization: Bearer <jwt>   -> 200 {id, email, is_admin}
```

s08 的聊天端点不动，整块 `app` 通过 `app.mount("/v1", s08_app)` 挂载到
s09 之下。所以走通 s09 的鉴权后，原来的 `/v1/chat/completions` 路径
**保持不变**，只是上游入口前面多了注册/登录两道门。

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

- `_conn()` 每次调用都新建连接并执行 `executescript(SCHEMA)`——SQLite 没有
  服务端，连接很轻，这样写最简单。
- `reset_db()` 删文件；Windows 下文件可能被进程持有，所以回退成
  `DELETE FROM users`，测试隔离仍然成立。
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

Payload 三个字段：`sub`（用户 id）、`email`、`is_admin`。`exp` 默认
1 小时，签名用 HS256，对称密钥足够本地学习用；生产环境换成 RS256 +
非对称密钥更合适。

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

def _current_user(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing token")
    try:
        return jwt_util.decode(auth.removeprefix("Bearer ").strip())
    except Exception:
        raise HTTPException(401, "invalid token")

@app.get("/me")
def me(claims: dict = Depends(_current_user)):
    return {"id": int(claims["sub"]),
            "email": claims["email"],
            "is_admin": claims.get("is_admin", False)}
```

注意 `_current_user` 用 `Depends` 注入到 `/me` 的参数 `claims`，
所以 `/me` 函数体本身只是把 claims 翻译成对外的 user 视图。
登录失败统一返回 `401 invalid credentials`——不区分“邮箱不存在”和
“密码错误”，避免被用来探测哪些邮箱已注册。

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
```

`JWT_SECRET` 强烈建议设置；不设置的话所有进程重启后旧 token 仍能用默认
`"change-me-in-production"` 验签——只是别把它带到线上。

## 测试

```bash
pytest tests/test_s09_user_system.py -v
```

四个测试覆盖主要契约：

| 测试 | 断言 |
| --- | --- |
| `test_signup_and_login_roundtrip` | 注册返回 201，登录返回 200，token 是合法 JWT（两点三段）。 |
| `test_login_with_wrong_password_fails` | 错误密码返回 401。 |
| `test_me_requires_token` | 没有 `Authorization` 头时 `/me` 返回 401。 |
| `test_me_returns_user_with_token` | 带合法 token 调用 `/me` 返回对应 email。 |

`reset_db()` 在 fixture 中先调用、后调用，保证测试之间互不污染。

## new-api 源码

- `controller/user.go` —— 注册、登录、注销等 HTTP 处理器；新-api 在
  这里把表单/JSON 绑定到 `model.User`，再交给 service。
- `model/user.go` —— `User` struct、密码哈希字段、`BeforeCreate` 钩子
  里调用 bcrypt；和我们这里的 `users.py` 一一对应。
- `service/user_notify.go` —— 通知/邮件相关扩展；本教程暂不涉及。

## 取舍

- **不做邮箱验证 / 找回密码 / 刷新令牌 / 注册限流** —— YAGNI。这一章
  只解决“能不能让用户登录”，剩下的留给 s10 之后按真实需求加。
- **JWT 存哪里** —— 我们让客户端自己持有 token（典型前端 localStorage
  或移动端 keychain）。新-api 还有 server-side session 路径，复杂度
  更高、可见性更好，但需要一次额外的查表；本教程选无状态的 JWT。
- **HS256 + 对称密钥** —— 学习和本地开发足够；线上推荐 RS256 + JWKS，
  因为前端不能拿私钥验签。
- **SQLite 文件锁** —— 单进程够用；多 worker 时 `find_by_email` 会拿
  不同连接，存在“同一时刻读到两条刚 commit 的 row”的窗口，s12 之后
  切到 Postgres 再讨论并发。
- **默认密钥硬编码** —— 仅用于演示；`JWT_SECRET` 必须设置，至少 32
  字节随机串。PyJWT 会发出 `InsecureKeyLengthWarning`，是预期内的。
