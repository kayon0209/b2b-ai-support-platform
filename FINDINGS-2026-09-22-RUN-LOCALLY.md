# 把项目跑起来给用户试 · 实测发现（2026-09-22）

起因：`运行一些项目，我试试`。
结论先行：**能跑起来了，但过程中撞到 2 个 P0 级阻塞缺陷** —— 不修的话，你点"上传文档"
会拿到 503，而文档永远无法进入检索。全部已修并验证。

---

## P0-1 · 对象存储端点漏配：上传直接 503，摄取永久重试

**现象**：前端点上传 → `503 STORAGE_UNAVAILABLE`，报文：
`document registered but object storage rejected the upload: ConnectError`。
MinIO 服务健康、宿主机 `curl /minio/health/live` → 200。

**根因**：`settings.object_storage_endpoint` 默认 `"localhost:9000"`（`config.py:83`）。
在容器里 `localhost` 是容器自己。compose 的 `environment:` 块本来就把 DB/Redis/Chatwoot
重映射成容器网络地址，**唯独漏了对象存储**（`env_file` 里的 `.env` 是宿主机地址，
`localhost:5435` 那种，在容器里同样不对 —— 所以这个块必须存在）。

**影响面**：`ai-api`（写对象）与 **`ai-worker-ingestion`（读对象解析）** 都缺，后者后果更重：
- API：上传报 503（用户可见，还算"有反馈"）
- 摄取 worker：`get_object` 抛错 → 归为 `_Retryable` → 释放占用 → 文档永远停在 `uploaded`，
  **知识库对检索永久不可用，而上传看起来是成功的**

**修复**：两个服务的 `environment:` 各加 `APP_OBJECT_STORAGE_ENDPOINT: minio:9000`，
并给两者加 `depends_on: minio: condition: service_healthy`（冷启动窗口内上传不会失败）。

**验证**：上传返回 200，`object_uri` 带租户前缀；文档 `ingestion_status` 走到 **`ready`**（已可检索）。

---

## P0-2 · MinIO 健康检查永远不可能通过（连续失败 14016 次）

**现象**：`docker compose ps` 里 minio 长期 `unhealthy`，而服务本身 200。

**根因**：`healthcheck.test: ["CMD", "mc", "ready", "local"]` —— **`mc` 不在 minio 服务镜像里**，
每次探测都是 `executable file not found in $PATH`（ExitCode -1）。容器内有 `curl`。

**为什么这不是小事**：一个永远失败的健康检查会训练所有人把红线当正常；
而且任何将来的 `depends_on: service_healthy` 会永久阻塞。

**修复**：改成 `["CMD", "curl", "-fsS", "http://localhost:9000/minio/health/live"]`（先在容器内实测通过）。
**验证**：`healthy (failing streak 0)`。

---

## P1-1 · 结构化日志静默丢字段：37 个调用点在写"看起来完整"的日志

**现象**：调试摄取卡住时，日志只有
`{"event": "ingestion_deferred", "reason_code": "_Retryable"}` ——
没有文档 id、没有原因。

**根因**：`JsonLogger` 在无 trace context 时按 `ALLOWED_LOG_FIELDS` **静默过滤**
（这是有意的脱敏边界）。但**没有任何东西约束调用点这一侧**，于是调用点自己发明字段名，
字段就消失了。全仓扫描：**37 处调用点、26 个字段名**。

典型：
| 位置 | 传了 | 白名单里的名字 | 结果 |
|---|---|---|---|
| `ingestion_consumer` | `version_id` | `document_version_id` | 卡住的文档在日志里没有 id |
| `ingestion_consumer` | `detail=str(exc)` | —（不在白名单） | 原因完全丢失 |
| `ingestion_consumer` | `chunks=` | `chunk_count` | **成功路径也没有分块数** |
| `inbox_consumer` ×2 | `error=str(exc)` | — | 失败原因丢失 |
| `runner` | `detail=str(exc)` | — | **worker 启动失败只说"配置错了"** |
| `middleware` ×2 | `exc_info=...` | —（当作字段被丢） | 想要 traceback，什么都没拿到 |
| （我自己新加的） | `abandoned_runs` | — | 我当天写的代码也在丢字段 |

**修复分三类，按字段语义而不是一刀切**：
1. **安全的标量进白名单**（`count`/`attempts`/`event_id`/`conversation_ref_id`/`abandoned_runs`…），
   并在注释里写明：**这个集合就是日志 schema，且它有测试守着**。
2. **自由文本改成码**：`error`/`detail`（异常消息，可能内嵌客户原话）→ `error_code=type(exc).__name__`。
   `runner` 那处例外保留可读文本——它继承 `SystemExit`，消息会随退出打到 stderr。
3. **`exc_info` 让 logger 真正支持**（转交 stdlib，traceback 打在 JSON 行之后，结构化行仍可解析）。

**防复发**：新增 `apps/api/tests/unit/test_log_fields.py` ——
源码扫描所有 `logger.*(...)` 调用，断言每个关键字字段都在白名单内；附带"扫描本身必须扫到东西"
和"排查必需的字段必须常驻白名单"两条守卫。

**验证**：该测试 5 条全绿（修之前它一次性列出全部 37 处，包括我自己的）。

---

## P1-2 · 摄取重试热循环：404 被当成"可重试"（~6 次/秒、永不停止）

**现象**：摄取 worker 每秒刷 6 条 `ingestion_deferred`，同一 `document_version_id`。

**根因**：`storage.get_object` 对**任何** ≥300 都抛同一个 `StorageValidationError`，
消费端一律映射为 `_Retryable`。于是"对象不存在（404，永久）"与"存储不可达（瞬时）"
在代码里是同一件事，前者被无限重试。

触发场景很具体：**上传在存储步骤失败** → API 已经注册了文档并写了 `object_uri`
（`no_object = false`！）→ 对象实际不存在 → 摄取每轮 404 → 释放占用 → 再来。

**修复**：
- `storage.py` 新增 `ObjectNotFound(StorageValidationError)`，只在 404 时抛
- 摄取消费端把它映射为 `IngestionError`（**终态 failed**，文档列表可见），其余仍为 `_Retryable`

**既有测试被这次修复"暴露"了**：`test_drain_versions_raises_rather_than_returning_partial_success`
的 docstring 写着"**缺失对象应延期而非失败**"，但它**从不 monkeypatch 存储** ——
它一直在靠"存储不可达"冒充"对象缺失"，而它之所以长期通过，正是因为 P0-1 那个漏配。
存储修好后真实的 404 出现，两种条件才分离开。已按该测试**本意**改成显式注入 503（依赖不可用），
并新增 `test_a_missing_object_fails_instead_of_retrying_forever` 钉住另一半。

**验证**：27 条摄取测试全绿（含新增）。

---

## 环境 · 我上一轮的误判（纠正）

- **"前端 dev server 不稳定"是错的**。真因两条：
  1. **Vite 5 默认只监听 IPv6 `[::1]`** → `curl http://127.0.0.1:5173` 返回 `http=000`（连接失败），
     而 `http://localhost:5173` 是 **200**。加 `--host 127.0.0.1` 后两种写法都通。
  2. **本机 curl 走系统代理** → 访问 localhost 必须 `--noproxy '*'`。
- **`agent-browser` 技能未安装**（要下 ~500MB Chromium，C 盘紧张不值得）。但**盘上已有 Playwright
  Chromium**（`D:/migrated/AppData/Local/ms-playwright`）。新增
  `scripts/admin_render_check.cjs`：真实 Chromium 打开各页面，**0 console error 才算过**并截图，
  零下载（用 `PLAYWRIGHT_CHROMIUM` 指定可执行文件绕开 revision 不匹配）。
  **6 个页面全部干净渲染**。

---

## 新增/变更清单

| 类型 | 文件 |
|---|---|
| 修 | `infra/compose/docker-compose.yml`（对象存储端点 ×2、minio 健康检查、depends_on） |
| 修 | `apps/api/src/platform_core/knowledge/storage.py`（`ObjectNotFound`） |
| 修 | `apps/worker/src/worker/ingestion_consumer.py`（404 终态、原因码、日志字段） |
| 修 | `apps/worker/src/worker/inbox_consumer.py`、`runner.py`、`identity/middleware.py`（日志字段） |
| 修 | `packages/observability/src/observability.py`（白名单扩充 + `exc_info` 支持） |
| 新 | `apps/api/tests/unit/test_log_fields.py`（日志字段守卫） |
| 新 | `scripts/admin_render_check.cjs`（渲染检查） |
| 改测试 | `apps/api/tests/integration/test_ingestion_worker.py`（显式注入故障 + 新增 404 用例） |
| 新 | `apps/admin-web/.env.local`（本地 token，已被 gitignore） |

## 遗留

- 供演示的 `kb://demo/pcb-capability-3` 是**上传失败的残留行**（有 `object_uri`、无对象）。
  它现在被保留期清扫标成 `expired`（不是我的修复标的），所以这条实活行**不能**用来证明我的修复；
  修复由集成测试钉住。
- dev 库 254 行孤儿 run（上一轮记录）仍未清理，等你确认。
