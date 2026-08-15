# learn-new-api

Build your own AI API gateway, one chapter at a time.

Each chapter adds one concept — HTTP forwarding, then SSE, then auth, then quota —
on top of the previous one, ending with a production-shape integration in `s_full/`.

## Reader Path

| # | Title | Adds |
|---|-------|------|
| [s01](s01_minimal_relay/) | Minimal relay kernel | HTTP forwarding |
| [s02](s02_openai_protocol/) | OpenAI-compatible protocol | `/v1/chat/completions` |
| [s03](s03_streaming_sse/) | Streaming responses | SSE |
| [s04](s04_multi_provider/) | Multi-provider adapters | Claude / Gemini |
| [s05](s05_api_key_auth/) | API key auth middleware | Bearer + blocklist |
| [s06](s06_token_counting/) | Token counting | tiktoken |
| [s07](s07_pre_consume_settle/) | Pre-consume & settle | Quota transactions |
| [s08](s08_rate_limiting/) | Rate limiting | Token bucket |
| [s09](s09_user_system/) | User system | Signup / JWT |
| [s10](s10_channel_management/) | Channel management | Multi-upstream + weights |
| [s11](s11_call_logs/) | Call logs & stats | Async write |
| [s12](s12_caching/) | Response cache + Redis | Exact cache |
| [s13](s13_retry_fallback/) | Retry / fallback | Tenacity + priorities |
| [s14](s14_admin_dashboard/) | Minimal admin dashboard | Jinja2 CRUD |
| [s15](s15_docker_deployment/) | Docker deployment | Compose + healthcheck |
| [s16](s16_observability/) | Observability | Prometheus + trace_id |
| [s_full](s_full/) | Full integration | All of the above |

## Run any chapter

Each chapter is independently runnable from its own directory:

```sh
cd sNN_topic
python code.py
```

Each `code.py` adds the project root to `sys.path` so cross-chapter imports resolve
without needing `PYTHONPATH` or `make` indirection. Then call the route shown in
that chapter's README.

## Run all tests

```sh
make test                # full suite
make test-s05            # one chapter
```

## Other targets

```sh
make run-s05             # boot chapter s05 (uses port 8005 by default)
make clean               # remove __pycache__, .pytest_cache, *.db litter
```