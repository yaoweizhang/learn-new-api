# s15：Docker 化部署（单容器 + HEALTHCHECK + docker-compose）

> Previous: [s14](../s14_admin_dashboard/) · Next: [s16](../s16_observability/)

> *"compose 一行起服务"* —— 本地 compose 等于生产。

> **Layer**：L5 运维与可观测

## 问题

到 s14，我们的 FastAPI 应用在开发机上跑得通：`python s14_admin_dashboard/code.py` 一开浏览器就能看到仪表盘。但"在我机器上能跑"不等于"上生产能跑"。

具体差在四件事：

- 不同机器上 Python 版本可能不一样（3.10 / 3.11 / 3.12 行为略有差异）。
- 不同机器上 `requirements.txt` 里某个包版本对不上，装出来不一致。
- 进程会不会死？挂了以后谁拉起？没人知道。
- 上游 OpenAI 突然改了协议，我们的连接是不是还活着？同样没人知道。

光把代码 `scp` 到服务器不够。部署需要的是：

1. 一个**可复现**的运行时（Python 版本 + 系统库 + Python 依赖 + 我们的代码）。
2. 一个**进程级**的存活机制（死了就拉起）。
3. 一个**业务级**的健康探针（进程在跑，但 DB 连不上 / 上游挂掉，也应该报警）。

## 方案

把整个 s01-s15 链路打包成一个**单容器**，再加一个独立的 Redis 容器：

- `Dockerfile`：基于 `python:3.11-slim`，按 `requirements.txt` 锁版本，把全部源码 COPY 进去，暴露 8015 端口，设置 `HEALTHCHECK`。
- `docker-compose.yml`：两服务 —— `gateway`（我们的应用）和 `redis`，`gateway` `depends_on` redis。
- `code.py`：在 s14 之上挂一个 `/healthz` 路由，**深检** DB 连接和上游可达性，直接由 Docker 的 `HEALTHCHECK CMD` 调用。

## 工作原理

**`Dockerfile` 顺序** —— 每条指令做一件事，缓存命中最好：

```
python:3.11-slim     # 基础镜像，系统层不变
WORKDIR /app         # 全部操作在 /app 内，避免污染根目录
COPY requirements.txt .
RUN pip install ...  # 单独一层，代码变了不重装
COPY . .             # 最后才拷代码
ENV PORT=8015
EXPOSE 8015          # 文档性，run 时仍需 -p
HEALTHCHECK ...      # Docker 每 30s 跑一次
CMD [...]            # 容器启动命令
```

**HEALTHCHECK**：`--interval=30s --timeout=5s --start-period=10s --retries=3` —— 启动 10s 后开始探测，30s 一次，单次超时 5s，连续 3 次失败才把容器标记成 `unhealthy`。`CMD` 用 `python -c "import httpx; print(...)"`，打印的就是 `httpx.get('/healthz').status_code`：是 200 就 0 退出码，非 200（或连接被拒绝）就非 0，Docker 据此更新容器健康状态。

**`/healthz` 深检**（`s15_docker_deployment/code.py`）：

- 在 s15 的 app 上**先**注册 `@app.get("/healthz")`，再 `app.mount("/", s14_app)`。
- Starlette 按注册顺序匹配路由，所以 `/healthz` 命中 s15 本地路由，不会落到 s14。
- 当前实现返回硬编码 `{"ok": True, "checks": {"db": True, "upstream": True}}` —— 测试环境不能真去戳 DB 或者访问 OpenAI，这里只搭骨架。
- 生产里这里真的去：(1) 调一下 `s09_user_system.users.find_by_email('healthcheck@example.com')` 验证 SQLite 可读；(2) 用 `httpx.get('https://api.openai.com', timeout=2.0)` 验证外网可达。任一异常就把 `checks[...]` 标 `False`，全部 `all(...)` 失败整个 `ok` 才 `False`。

**docker-compose**：`gateway` 服务 build 当前目录（就是 `Dockerfile`），`ports: 8015:8015` 把容器 8015 暴露到宿主 8015。`redis` 是给未来的 rate limit / cache 预留的位置 —— 这一章不真用，但 compose 文件先到位，后面章节可以无缝接。

## 运行

```bash
# 1. 构建并启动（后台）
docker compose -f s15_docker_deployment/docker-compose.yml up -d --build

# 2. 查健康状态（Docker 视角）
docker compose -f s15_docker_deployment/docker-compose.yml ps
# 预期：gateway 状态是 "Up (healthy)" —— /healthz 在 30s 内返回 200 之后才是 healthy
```

## 测试

`tests/test_s15_docker_deployment.py` 一条用例：

```python
def test_healthz_deep_check():
    with TestClient(app) as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True
```

跑：

```bash
python -m pytest tests/test_s15_docker_deployment.py -v
```

预期 `1 passed`。注意：这条用例**不需要 Docker** —— `code.py` 里的 `app` 就是一个普通的 FastAPI 实例，直接用 `TestClient` 跑。同时它验证了关键约束：`/healthz` 必须出现在 s14 mount 之前，否则返回的是 s14 的 404。

## → new-api 源码

- 兄弟目录 `new-api/Dockerfile`（和本仓库同级的 sibling 仓库）—— new-api 的多阶段 Dockerfile：`builder` 镜像编译 Go 二进制 → `运行时` 镜像只放 alpine + 二进制 + 配置。比 s15 的单阶段更省空间，但需要 Go 工具链（我们这一章是纯 Python 不需要）。
- 兄弟目录 `new-api/docker-compose.yml` —— new-api 的 compose 有 `one-api`（`oneapi-network`）、`mysql`、`redis` 三个 service，并且加了 `network_mode` 和 secrets。比 s15 的版本丰富得多，但思路一致：`healthcheck` → `depends_on` → 端口映射。

## 取舍

**1. 单容器，不拆 gateway + worker**

new-api 在生产里通常把 HTTP 网关和后台任务（对账、清理、统计）拆成两个进程；这里**故意没拆** —— s15 是教学的最后一章，我们的代码量还小，把全部 s01-s15 链路放在一个进程里才便于读懂。生产化（≥ 万 QPS 或者有重后台任务）时再拆。

**2. `/healthz` 目前是"永远为真"的桩**

当前实现无论 DB / 网络怎样都返回 `ok: true`，只是为了：

- 保证 `HEALTHCHECK` 容器级不报警；
- 让 `tests/test_s15_docker_deployment.py` 在没装 SQLite / 没外网的 CI 上也能跑。

生产化时这里要换成真探测（DB read + upstream HEAD），并把 `checks` 里 fail 的项标 `False`。

**3. 没有多阶段构建**

`FROM python:3.11-slim` 出来直接跑，镜像 ~150MB；new-api 的多阶段出 ~20MB。镜像大小对教学无所谓，多阶段留给 s_full（如果需要）做。

**4. 没有 prod 反向代理**

`docker-compose.yml` 直接暴露 8015 到宿主。生产里前面会有 nginx / caddy / 阿里云 SLB 做 TLS 终止、限流、静态文件缓存；这一章不加 —— 想聚焦在"应用层打包 + 健康检查"这一层，不掺入基础设施。

**5. Docker 镜像里直接 COPY 全部源码**

没有 `.dockerignore` 过滤 `tests/`、`.git/`、`.superpowers/` 等。生产镜像应该只含运行需要的文件；这里保留是为了让镜像能做 `python -m pytest ...` 自检 —— 同样留给 s_full 做最末一步。
