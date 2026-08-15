# learn-new-api

**new-api** 是一类 AI API 网关：把多家上游（OpenAI、Anthropic、Gemini、本地推理服务）合并成一个 OpenAI 兼容端点。客户端拿自己的账号余额/配额，发任意上游的请求——对外说 OpenAI 的话，任何 OpenAI SDK 都能直接连。

按用户视角看，它主要解决四件事：

- 多人共用一个网关，按调用扣费（quota 预扣 + 结算）
- 一个模型名背后挂多个渠道，按优先级/权重选，失败自动 fallback
- 管理员后台：加渠道、看用量、看错误、查 p99
- 调用日志、缓存、限流、可观测性

原始实现是 Go（[songquanpeng/new-api](https://github.com/songquanpeng/new-api)），代码量大、模块耦合深。光看代码很难理解"渠道级 fallback + quota 预扣结算 + token 计数"这些概念是怎么咬合的——设计动机埋在 if 后面。

这个仓库把那套核心骨架拆成 16 个递进的 FastAPI 章节，每章只解决一个具体问题，最后在 `s_full/` 把它们拼回一个生产形态的应用。读完全部约等于用 Python 把 new-api 的核心架构重新实现了一遍，但每一步都能看到**前因后果**——前一步为什么这么写、后一步在哪里接管。

## 阅读路径

| # | 章节 | 这一步解决什么 |
|---|---|---|
| [s01](s01_minimal_relay/) | 最小转发 | HTTP 转发 |
| [s02](s02_openai_protocol/) | OpenAI 协议 | 对外说 OpenAI 的话 |
| [s03](s03_streaming_sse/) | 流式响应 | SSE |
| [s04](s04_multi_provider/) | 多 provider | Claude / Gemini 也能转 |
| [s05](s05_api_key_auth/) | API key 鉴权 | Bearer + 黑名单 |
| [s06](s06_token_counting/) | Token 计数 | tiktoken |
| [s07](s07_pre_consume_settle/) | 预扣 + 结算 | 调一次扣多少 |
| [s08](s08_rate_limiting/) | 限流 | Token bucket |
| [s09](s09_user_system/) | 用户系统 | 注册 / 登录 / JWT |
| [s10](s10_channel_management/) | 渠道管理 | 多上游 + 优先级/权重 |
| [s11](s11_call_logs/) | 调用日志 | 异步落 + 统计 |
| [s12](s12_caching/) | 响应缓存 | 完全相同 prompt 命中 |
| [s13](s13_retry_fallback/) | 失败回落 | 切到下一条渠道 |
| [s14](s14_admin_dashboard/) | 管理后台 | Jinja2 CRUD |
| [s15](s15_docker_deployment/) | Docker 部署 | Compose + healthcheck |
| [s16](s16_observability/) | 可观测性 | Prometheus + trace_id |
| [s_full](s_full/) | 完整整合 | 16 章合一 |

每个章节顶部有 `Previous / Next` 导航，跟着读就行。

## 学习路径

下图中色块按 Layer 分组——蓝=L1 协议与转发 / 橙=L2 鉴权与身份 / 绿=L3 计量与扣费 / 粉=L4 路由与韧性 / 紫=L5 运维与可观测 / 黑=LX 整合形态。

```mermaid
flowchart LR
    s01["s01 最小转发<br/>把请求转出去"]:::L1
    s02["s02 OpenAI 协议<br/>对外说 OpenAI"]:::L1
    s03["s03 流式响应<br/>一个字一个字流出去"]:::L1
    s04["s04 多 provider<br/>前缀选 provider"]:::L1
    s05["s05 API key 鉴权<br/>Bearer 一行拦住"]:::L2
    s06["s06 Token 计数<br/>tiktoken 数明白"]:::L3
    s07["s07 预扣+结算<br/>多扣一点退给你"]:::L3
    s08["s08 限流<br/>桶里取 token"]:::L3
    s09["s09 用户系统<br/>注册即有 JWT"]:::L2
    s10["s10 渠道管理<br/>priority + weight 排序"]:::L4
    s11["s11 调用日志<br/>100ms 异步刷一次"]:::L5
    s12["s12 响应缓存<br/>相同 prompt 才命中"]:::L4
    s13["s13 失败回落<br/>失败即换下一条"]:::L4
    s14["s14 管理后台<br/>把 in-memory 表露出来"]:::L5
    s15["s15 Docker 部署<br/>compose 一行起服务"]:::L5
    s16["s16 可观测性<br/>trace_id 串起请求"]:::L5
    full["s_full 完整整合<br/>16 章合一"]:::LX
    s01 --> s02 --> s03 --> s04 --> s05 --> s06 --> s07 --> s08 --> s09 --> s10 --> s11 --> s12 --> s13 --> s14 --> s15 --> s16 --> full

    classDef L1 fill:#e3f2fd,stroke:#1976d2
    classDef L2 fill:#fff3e0,stroke:#f57c00
    classDef L3 fill:#e8f5e9,stroke:#388e3c
    classDef L4 fill:#fce4ec,stroke:#c2185b
    classDef L5 fill:#f3e5f5,stroke:#7b1fa2
    classDef LX fill:#212121,stroke:#000,color:#fff
```

## 跑任意一章

每一章都是独立可跑的 FastAPI app：

```sh
cd sNN_topic
python code.py
```

每个 `code.py` 会把项目根加进 `sys.path`，跨章节 import 直接写就行。启起来后看那一章 README 里的 curl 和路径。

## 跑测试

```sh
make test                # 全部
make test-s05            # 只跑 s05
```

## 其他

```sh
make run-s05             # 用 8005 端口起 s05
make clean               # 清 __pycache__、.pytest_cache、*.db
```

## 依赖

Python 3.11+。先 `pip install -r requirements.txt`。
