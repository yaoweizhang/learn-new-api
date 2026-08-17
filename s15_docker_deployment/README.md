# s15: Docker 化部署(单容器 + HEALTHCHECK + docker-compose) — 一个容器 + 一个 compose,跑起来就能用

> Previous: [s14](../s14_admin_dashboard/) · Next: [s16](../s16_observability/)

> *"compose 一行起服务"* —— 本地 compose 等于生产。

> **Layer**：L5 运维与可观测

## 问题

到 s14,我们的 FastAPI 应用在开发机上跑得通:`python s14_admin_dashboard/code.py` 一开浏览器就能看到仪表盘。但"在我机器上能跑"不等于"上生产能跑"。

具体差在四件事:

- 不同机器上 Python 版本可能不一样(3.10 / 3.11 / 3.12 行为略有差异)。
- 不同机器上 `requirements.txt` 里某个包版本对不上,装出来不一致。
- 进程会不会死?挂了以后谁拉起?没人知道。
- 上游 OpenAI 突然改了协议,我们的连接是不是还活着?同样没人知道。

光把代码 `scp` 到服务器不够。部署需要的是:

1. 一个**可复现**的运行时(Python 版本 + 系统库 + Python 依赖 + 我们的代码)。
2. 一个**进程级**的存活机制(死了就拉起)。
3. 一个**业务级**的健康探针(进程在跑,但 DB 连不上 / 上游挂掉,也应该报警)。

## 本章要做什么

现在场景是:到 s14,我们的 FastAPI 应用在开发机上跑得通:`python s14_admin_dashboard/code.py` 一开浏览器就能看到仪表盘。但"在我机器上能跑"不等于"上生产能跑"——Python 版本不一致、依赖装出来不一致、进程死了没人拉、上游改了协议没人知道。要解决这个——**我们把整个 s01-s14 链路打包进 `python:3.11-slim` 单容器**(**Docker 容器**(一个跑在宿主上的隔离进程,自带 Python 运行时 + 我们的代码 + 依赖;用 `docker compose up` 一条命令拉起);**HEALTHCHECK**(Docker 自带的进程级探活:每 30s 调一次 `/healthz`,失败累计 3 次把容器标 unhealthy 触发重启)),用 `docker compose up` 一条命令拉起完整网关(单进程 in-memory,没有 redis / mysql 依赖);`HEALTHCHECK` 探活,`/healthz` 路由做业务级深检——深检现在是个桩(永远 `ok=True`),生产里换 DB 读 + upstream HEAD。本章把这套部署形态写出来:

1. **写一个 `Dockerfile` —— 为什么单阶段不写多阶段**: `FROM python:3.11-slim` + `WORKDIR /app` + `COPY requirements.txt .` + `RUN pip install --no-cache-dir` + `COPY . .`。**为什么不写多阶段**: 多阶段出 ~20MB 镜像,单阶段 ~150MB;镜像大小对教学无所谓,多阶段需要 Go 工具链那一套思路(我们纯 Python 不需要);**为什么 `requirements.txt` 在 `COPY . .` 之前**: 镜像分层缓存——代码改了只重 build 最后一层,依赖层不动。

2. **加一条 `HEALTHCHECK` + `/healthz` 深检路由 —— 为什么不是简单 `curl /health`**: `HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import httpx; print(httpx.get('http://localhost:8015/healthz').status_code)"`。`/healthz` 在 s15 这层 app 上先注册、`app.mount("/", s14_app)` 之后挂载,Starlette 按注册顺序匹配,所以 `/healthz` 命中 s15 本地路由不落到 s14。**为什么是深检不是存活探针**: s01 的 `/health` 是零依赖存活探针;`/healthz` 是业务级深检,生产里要真去 (1) 调 `find_by_email('healthcheck@example.com')` 验证 SQLite 可读 (2) `httpx.get('https://api.openai.com', timeout=2.0)` 验证外网可达。任一异常 `checks[...] = False`,`all(...)` 失败整个 `ok` 才 `False`。**为什么当前是桩**: 测试环境不能真去戳 DB 或访问 OpenAI,这里只搭骨架。

3. **写一份 `docker-compose.yml` —— 为什么单容器不拆 gateway + worker**: `services.gateway.build: .`、`ports: 8015:8015`、`env_file: ../.env`、`healthcheck:` 跟 Dockerfile 同步。**为什么不拆**: new-api 在生产里通常把 HTTP 网关和后台任务(对账、清理、统计)拆成两个进程;**故意没拆**——s15 是教学的最后一章,我们的代码量还小,把全部 s01-s14 链路放在一个进程里才便于读懂;**本章也不依赖 redis / mysql**: 全部状态都是进程内 in-memory,生产化(≥ 万 QPS 或有重后台任务)时再拆 + 加外部依赖。**为什么 `env_file: ../.env`**: docker-compose 的 env_file 解析以 compose 文件所在目录为相对基准,本章 compose 用 `env_file: ../.env`,所以需要先把 `.env.example` 复制到仓库根的 `.env`。

4. **`/healthz` 路由先注册,`app.mount("/", s14_app)` 最后 —— 为什么这个顺序不能错**: Starlette 按注册顺序迭代路由,本地路由先匹配、挂在最后面的 `Mount("/")` 是兜底。`/healthz` 在 s15 本地命中,根本不会落进 s14。**为什么测试不用 Docker**: `code.py` 里的 `app` 就是一个普通 FastAPI 实例,直接用 `TestClient` 跑——同时验证关键约束:`/healthz` 必须出现在 s14 mount 之前,否则返回的是 s14 的 404。

成品: `docker compose -f s15_docker_deployment/docker-compose.yml up -d --build` 拉起容器,`docker compose ps` 看到 `gateway 状态 Up (healthy)`(/healthz 在 30s 内返回 200 后才 healthy);`curl localhost:8015/dashboard/login` 浏览器仍可达。后续 s16 加 Prometheus + trace_id,s_full 真接 Postgres + Redis + 反向代理。

## 方案

现在的场景是:`## 问题` 提了四件痛——Python 版本不一致 (痛点 #1)、依赖装出来不一致 (痛点 #2)、进程死了没人拉 (痛点 #3)、上游改了协议没人知道 (痛点 #4)——这四件事**没法靠"代码 scp 到服务器"或"运维写 systemd unit"能统一解决**,必须有一个可复现的 runtime + 进程级拉起 + 业务级深检。

**要解决这个——我们在网关外引入三个最小部署文件**,把所有运行时锁在一处镜像里:

- `Dockerfile`:基于 `python:3.11-slim`,按 `requirements.txt` 锁版本,把全部源码 COPY 进去,暴露 8015 端口,设置 `HEALTHCHECK`(Docker 内置健康检查机制,容器自起后定时探活,失败累计触发重启)。
- `docker-compose.yml`:单服务 —— `gateway`(我们的应用),把整个 s01-s15 链路打包成一个 service,无外部依赖。
- `code.py`:在 s14 之上挂一个 `/healthz` 路由,**深检** DB 连接和上游可达性,直接由 Docker 的 `HEALTHCHECK CMD` 调用。

**首次引入**:**Docker 容器**(Docker 容器——把应用 + 运行时 + 依赖 + 配置打包成一个可移植、可复现的镜像,在任何 Docker host 上 `docker compose up` 就能跑起来的部署单元——本章首次提到这个术语,这里给出定义 + 角色)。它在本章里承担的是"一处构建、到处运行、失败自愈"的全套职责。

下面这幅图把上面四件痛各放到三个角色里:

- **`Host` (Docker host,开发机/服务器)** —— 在装上 docker 之前,这是被迫拼 `python s14/code.py` + 写 systemd unit + 手敲 supervisor.conf 的角色;装上之后,这事被 docker-compose 隔走——只发一条 `docker compose up` 命令就完事。
- **`Container` (本章要打的镜像 `gateway`, `python:3.11-slim` 单容器)** —— 把痛点 #1 #2 #3 #4 的解决动作集中放在这里:`Dockerfile` 锁 Python 版本 + pip 依赖、`HEALTHCHECK` 30s 探一次 `/healthz`、失败重启。一处镜像构建,所有环境(开发机/CI/生产)拿到的运行时都一致。
- **`Upstream` (LLM 厂商)** —— 服务提供方。容器只通过 `:8015` 对外暴露 `/healthz` 深检 + chat 端点;`/healthz` 当前是桩 `{ok: True, checks: {db, upstream}}`(测试环境不能真戳 DB / 访问 OpenAI,生产里需要替换),`checks[*]` 任一项 False 整个 `ok` 才 False,生产里可以据此触发告警。

- `Dockerfile`:基于 `python:3.11-slim`,按 `requirements.txt` 锁版本,把全部源码 COPY 进去,暴露 8015 端口,设置 `HEALTHCHECK`。
- `docker-compose.yml`:单服务 —— `gateway`(我们的应用),把整个 s01-s15 链路打包成一个 service,无外部依赖。
- `code.py`:在 s14 之上挂一个 `/healthz` 路由,**深检** DB 连接和上游可达性,直接由 Docker 的 `HEALTHCHECK CMD` 调用。

## 工作原理

**原理**: 运维打 `docker compose -f s15_docker_deployment/docker-compose.yml up -d --build` 时,整个流程是: compose 读 `docker-compose.yml` 拿到 `build: .` + `env_file: ../.env` → 调用 `docker build` 把 `Dockerfile` 跑出一层 `python:3.11-slim` 基础镜像 → 装 `requirements.txt` → COPY 源码 → `CMD ["python","s15_docker_deployment/code.py"]` 起 uvicorn:8015 → `HEALTHCHECK` (Docker 内置健康检查) 每 30s 调一次 `python -c "...httpx.get('http://localhost:8015/healthz')..."` → 容器自身在 s15 这层 app **先** 注册 `@app.get("/healthz")` 深检路由(把 `app.mount("/", s14_app)` 放最后,Starlette 按注册顺序匹配) → 探活返回 200 三次连续则容器标 `healthy`、否则 `unhealthy` (Docker 据此决定是否重启)。整章所有部件都为"一次构建、到处运行、自动探活"这条主线服务。

**1. 一个 Dockerfile (`FROM python:3.11-slim` + 单阶段 COPY + requirements 缓存层)** —— 镜像锁 Python 版本 + `pip install --no-cache-dir -r requirements.txt` + COPY 全部源码;**为什么 `requirements.txt` 在 `COPY . .` 之前**: 镜像分层缓存 (Docker 构建出的每一层单独缓存)——代码改了只重 build 最后一层,依赖层不动,镜像迭代更快。

**2. 一个 HEALTHCHECK + `/healthz` 深检路由 (Docker 自带的进程级探活 + 业务级深检桩)** —— `HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import httpx; print(httpx.get('http://localhost:8015/healthz').status_code)"`,Docker 每 30s 跑一次,失败累计 3 次标 `unhealthy` 触发重启。`/healthz` 在 s15 这层 app 先注册、本地路由挡 mount,深检桩返回 `{ok, checks{db, upstream}}`;**生产里**这里的 checks 要真去 find_by_email / 测外网。

**3. 一个 docker-compose.yml 单服务 (单容器 `gateway`,含 build + env_file + 端口映射)** —— `services.gateway.build: .`、`ports: 8015:8015`、`env_file: ../.env`、`healthcheck:` 同步镜像的 `HEALTHCHECK`;**为什么是单容器**: 教学范围内一个小进程便于读懂,生产化(≥ 万 QPS 或有重后台任务)再拆 gateway + worker。

**4. 一个 `/healthz` 本地路由挡 mount (Starlette 注册顺序约束)** —— `@app.get("/healthz")` 在 s15 app 上**先**注册,`app.mount("/", s14_app)` 放最后;**为什么这个顺序不能错**: Starlette 按注册顺序匹配路由,本地 `/healthz` 命中,根本不会落到 s14,s14 没这条路由会 404。

### 部署形态

**`Dockerfile` 顺序** —— 下面这张块状说明把 Dockerfile 每条指令的目的压成一览:每一行就是一层(layer),目的就是让常用层(基础镜像、依赖装包)单独缓存,代码改了不重装。下面这块伪代码表示每行做一件独立的事:

```
python:3.11-slim     # 基础镜像,系统层不变
WORKDIR /app         # 全部操作在 /app 内,避免污染根目录
COPY requirements.txt .
RUN pip install ...  # 单独一层,代码变了不重装
COPY . .             # 最后才拷代码
ENV PORT=8015
EXPOSE 8015          # 文档性,run 时仍需 -p
HEALTHCHECK ...      # Docker 每 30s 跑一次
CMD [...]            # 容器启动命令
```

**HEALTHCHECK**:`--interval=30s --timeout=5s --start-period=10s --retries=3` —— 启动 10s 后开始探测,30s 一次,单次超时 5s,连续 3 次失败才把容器标记成 `unhealthy`。`CMD` 用 `python -c "import httpx; print(...)"`,打印的就是 `httpx.get('/healthz').status_code`:是 200 就 0 退出码,非 200(或连接被拒绝)就非 0,Docker 据此更新容器健康状态。

**`/healthz` 深检**(`s15_docker_deployment/code.py`):

- 在 s15 的 app 上**先**注册 `@app.get("/healthz")`,再 `app.mount("/", s14_app)`。
- Starlette 按注册顺序匹配路由,所以 `/healthz` 命中 s15 本地路由,不会落到 s14。
- 当前实现返回硬编码 `{"ok": True, "checks": {"db": True, "upstream": True}}` —— 测试环境不能真去戳 DB 或者访问 OpenAI,这里只搭骨架。
- 生产里这里真的去:(1) 调一下 `s09_user_system.users.find_by_email('healthcheck@example.com')` 验证 SQLite 可读;(2) 用 `httpx.get('https://api.openai.com', timeout=2.0)` 验证外网可达。任一异常就把 `checks[...]` 标 `False`,全部 `all(...)` 失败整个 `ok` 才 `False`。

**docker-compose**:`gateway` 服务 build 当前目录(就是 `Dockerfile`),`ports: 8015:8015` 把容器 8015 暴露到宿主 8015。本章 compose **不含 redis 之类外部依赖**——`依赖什么 / 单容器多容器`是部署策略选择,留给后续按部署形态调整。本章聚焦"应用层打包 + 健康检查"这一层。

## 运行

```bash
# 1. 构建并启动(后台)
docker compose -f s15_docker_deployment/docker-compose.yml up -d --build

# 验证部署到位: 容器状态是 Up (healthy)(说明 /healthz 在 30s 内返回 200,Docker 据此把容器标 healthy)——这能验证 Dockerfile 构建、compose 启动、uvicorn 加载、/healthz 深检路由挡 mount 全部到位:

# 注:docker-compose 的 env_file 解析以 compose 文件所在目录为相对基准,
#   本章 compose 用 `env_file: ../.env`,所以需要先把 `.env.example` 复制到仓库根的 `.env`。
cp .env.example .env  # 仅首次需要;之后改 .env 不用再 cp

# 2. 查健康状态(Docker 视角)
docker compose -f s15_docker_deployment/docker-compose.yml ps
# 预期:gateway 状态是 "Up (healthy)" —— /healthz 在 30s 内返回 200 之后才是 healthy
```

## → new-api 源码

- 兄弟目录 `new-api/Dockerfile`(和本仓库同级的 sibling 仓库)—— new-api 的多阶段 Dockerfile:`builder` 镜像编译 Go 二进制 → `运行时` 镜像只放 alpine + 二进制 + 配置。比 s15 的单阶段更省空间,但需要 Go 工具链(我们这一章是纯 Python 不需要)。
- 兄弟目录 `new-api/docker-compose.yml` —— new-api 的 compose 有 `one-api`(`oneapi-network`)、`mysql`、`redis` 三个 service,并且加了 `network_mode` 和 secrets。比 s15 的版本丰富得多,但思路一致:`healthcheck` → `depends_on` → 端口映射。

## 本章不做什么

- **没有多阶段构建** (multi-stage——用 builder 阶段编译再拷到 runtime 阶段,减小最终镜像体积) —— `FROM python:3.11-slim` 出来直接跑,镜像 ~150MB;new-api 的多阶段出 ~20MB。镜像大小对教学无所谓,多阶段留给 s_full 做。
- **没有 prod 反向代理** (TLS 终止、限流、静态文件缓存由反向代理负责,如 nginx / caddy / 阿里云 SLB) —— `docker-compose.yml` 直接暴露 8015 到宿主。生产里前面会有 nginx / caddy / 阿里云 SLB;这一章不加 —— 想聚焦在"应用层打包 + 健康检查"这一层,不掺入基础设施。→ s_full 加 nginx / caddy 时一并做。
- **没有 `.dockerignore`** (镜像构建时排除 `tests/`、`.git/` 等非运行所需目录) —— 没有 `.dockerignore` 过滤 `tests/`、`.git/`、`.superpowers/` 等,镜像直接 COPY 全部源码。生产镜像应该只含运行需要的文件;这里保留是为了让镜像能做 `python -m pytest ...` 自检 —— 留给 s_full 做最末一步。
- **没有 K8s/Helm/cluster 化** —— `docker compose` 是单机编排,生产的多副本 / 滚动升级 / 跨节点调度不在本章范围。→ s_full 如果上线再做 K8s 化。
- **没有 secrets manager 注入** —— `env_file: ../.env` 把环境变量直接 mount 进容器,密钥存在仓库根的 `.env` 文件。生产用 Vault / AWS Secrets Manager / k8s Secret,本地开发场景直接 `.env` 够用。

## 已知限制

- **`/healthz` 目前是"永远为真"的桩** —— 当前实现无论 DB / 网络怎样都返回 `ok: true`,只是为了: (1) 保证 `HEALTHCHECK` 容器级不报警;(2) 让 `tests/test_s15_docker_deployment.py` 在没装 SQLite / 没外网的 CI 上也能跑。生产化时这里要换成真探测:DB 用 `find_by_email('healthcheck@example.com')` 验证 SQLite/Postgres 可读;upstream 用 `httpx.get('https://api.openai.com', timeout=2.0)` 验证外网可达;任一异常把 `checks[*]` 标 False,`all(...)` 失败整个 `ok` 才 False。→ s_full 接 Postgres 后一并换实现。
- **`HEALTHCHECK CMD` 仍是外层调用 `/healthz`,不查 DB / 网络** —— 同上原因(`tests/` 不依赖外部服务)。生产里要在进程内直接探测而不是 HTTP 跳回自己(避免"自己探自己永远 ok"的死循环)。
- **`start-period=10s` 在慢机器上可能不够** —— 启动 10s 后开始探测,慢 CI / 慢拉镜像可能还没起来就被判 unhealthy。可以提到 30s。YAGNI:14 章测试在 10s 内启动完毕够用。
- **`env_file: ../.env` 强依赖 `.env` 在仓库根** —— 首次跑必须 `cp .env.example .env`;忘了 cp compose 会以空环境变量启动,OpenAI key / admin password 全是 default。代码里 docker-compose.yml 用相对路径 `../.env` 是**有意为之**:compose 解析时相对于 compose 文件所在目录,而 `.env` 是仓库根的单一份 secrets,mount 进容器统一管理。

## 设计选择

- **单容器,不拆 gateway + worker** —— new-api 在生产里通常把 HTTP 网关和后台任务(对账、清理、统计)拆成两个进程;这里**故意没拆**: s15 是教学的最后一章,代码量小,全部 s01-s15 链路放在一个进程里便于读懂。**反方**: 生产化(≥ 万 QPS 或有重后台任务)再拆,但拆就要引入 Redis 共享状态 + worker supervisor,工作量再 +1 章。YAGNI:这一章聚焦"能跑起来 + 能探活",不掺入"怎么分布式"。
- **`/healthz` 路由先注册,`app.mount("/", s14_app)` 最后** —— Starlette 按注册顺序匹配,本地 `/healthz` 命中,根本不会落进 s14。这跟 `s04_multi_provider` / `s05_api_key_auth` / `s13_retry_fallback` 都在踩的同一个 Starlette 坑一致:本地路由必须先注册,挂载必须最后。本章的 `code.py` 把这条作为约定俗成的硬约束,在挂载顺序注释里强调一遍。**替换而不是叠加**: s15 是 chat + healthz 端点的"打包版",挂载 s14 仍然存在,只是为了 `/v1/chat/completions` 这种老路由可达。

## 下章预告

s15 部署对了,出问题靠肉眼 grep 日志。s16 加 trace-id + Prometheus + structlog,跨调用拉一条线。
