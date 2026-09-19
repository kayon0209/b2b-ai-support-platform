# Round 3 — 性能与并发

日期：2026-09-19。对象：API :8000（宿主机进程）、admin-web dev server。全部数字为本机实测。

## A. 前端

| 指标 | 实测 | 门槛 | 结论 |
|---|---|---|---|
| JS bundle（生产构建） | 252.5 KB raw / **79.0 KB gzip** | < 300 KB gzip | ✅ |
| CSS | 9.6 KB raw / 2.6 KB gzip | — | ✅ |
| 单文件 > 500 KB | 无 | 无 | ✅ |
| DCL（8 页逐页，dev server 含 HMR） | **367–449 ms**（/gaps 374 · /quality 447 · /flags 367 · /cases 418 · /members 398 · /usage 391 · /branding 413 · /prompts 449） | LCP<2.5s 方向性参考 | ✅ |
| Lighthouse FCP/LCP | 未执行：本环境无可用 Chrome/注册表访问。以 dev-server DCL（上界）+ 生产 bundle 体积替代 | — | 如实标注 |

## B. 后端（压测，10s 窗口）

| 场景 | n | p50 | p95 | p99 | RPS | 错误 |
|---|---|---|---|---|---|---|
| GET /healthz, conc=10 | 2408 | 16.2ms | 21.9ms | 31.2ms | 240.8 | 0 |
| GET /v1/cases（限流内-paced 8rps） | 40 | **13.9ms** | **21.0ms** | 54.1ms max | 8 | 0 |
| POST /v1/retrieval/query（paced 1rps，真实外部 embedding） | 5 | **980.9ms** | 1036.2ms | 1110.8ms max | 1 | 0 |
| POST /v1/retrieval/query（conc=50 挤压） | 120 | 6.8s | 13.5s | 15.0s | 12 | 429×47 |

解读：DB+RLS+鉴权链路 p95=21ms（远低于架构目标授权 50ms/150ms）。检索 p95 由**外部 embedding 调用（Gitee AI ~1s）主导**，不是数据库；并发挤压下受 `concurrency_retrieval_limit=16` 信号量与外部 API 串联排队的双重约束。

限流行为（并发正确性问题之一）：认证租户桶 600 req/min，超限返回 **429 + `{"error":{"code":"RATE_LIMITED","retryable":true}}` 干净信封**——不误伤（限流内 0 错误），不堆积。/healthz 豁免（240 rps 零 429）。

数据库连接：压测前后 pg_stat_activity 8 → 14 后保持稳定（池增长到需求水位即封顶，**无泄漏曲线**）。

## C. 并发正确性（逐项实测）

| 场景 | 方法 | 观测 | 结论 |
|---|---|---|---|
| 同资源并发写 | 同一 case、同 expected_version，两个 change_priority 并发 | **200 + 409 CASE_VERSION_CONFLICT**，版本恰好 +1 | ✅ 无脏写 |
| 同 key 并发重放 | 同一 Idempotency-Key 4 路并发 | [200, 409, 409, 409]，版本只动一次 | ✅ 零重复生效 |
| 账本幂等重放 | 同 key 调整两次 | 第二次 200 且 entries 不变（3 条 = 3 笔不同调整） | ✅ 无重复入账 |
| 限流触发 | 50 并发打满 | 429 + 干净信封 + retryable:true | ✅ |
| 长任务互踩 | 真实 Chatwoot 双向 E2E + worker | run 顺序处理，弃权通知发出，outbound 自回环被 `event_skipped_not_customer` 拦截 | ✅ |
| 队列背压 | 深度 ≥500 → 429 QUEUE_SATURATED | 代码审查（agent_runtime/router.py:110-127），未构造 500 深度实测 | 标注：未实测 |

## C+. 核心链路端到端（真实 Chatwoot 回路）

修复本地环境漂移后（webhook 行 URL 指向 compose 内部 `ai-api` 主机名 + secret 与 .env 不一致——历史遗留，见下）实测：

1. 客户消息（Chatwoot API inbox）→ **WebhookJob 真实投递**（sidekiq 日志 1026c7b2，含 secret+delivery_id）→ 平台 202 → InboxEvent 落库 ✓
2. worker 认领 → route=knowledge_qa → 无授权证据 → **弃权（NO_AUTHORIZED_EVIDENCE，latency 1156ms）** ✓（该 e2e 租户语义正确：无语料必须弃权而非编造）
3. 弃权通知送达 Chatwoot ✓——证据：outbound 消息触发的新 webhook 被平台接收并按设计跳过（worker 日志 `event_skipped_not_customer`，21:29:36.281）
4. 检索带引用：向 admin-demo 租户上传文档（MinIO→摄取→2 chunks 全部真实 embedding）→ `/v1/retrieval/query` 命中正确 chunk，ranking 含 vector 0.888 / trigram 0.312 / rerank 0.9986 ✓

## D. 反向验证（mutation）

- 改坏点：`apps/api/src/platform_core/cases/models.py:74-77 check_version` —— 用 `if False and ...` 短路版本比较（重启 API 使其生效）。
- 结果：同一并发探针变成 **200 + 200，版本 1→3（双重生效，脏写复现）** → 探针抓到了守护被移除。`MUTATION DETECTED: True`。
- 恢复：revert 后 `git diff` 为空，重启后探针恢复 200 + 409、版本恰好 +1。**验证有效，非摆设。**

## 本轮新发现

1. **P2（测试资产）** `tests/e2e/e2e_chatwoot_loop.py` 清理逻辑在删除租户时撞 `conversation_turns` 外键（迁移 0034 新表未加入清理清单）→ 脚本崩溃且留下孤儿租户行。脚本本身验证目标（webhook→入库→弃权通知）全部达成。
2. **P2（环境漂移）** 本地 compose 的 Chatwoot webhook 行 URL 指向 `http://ai-api:8000`（compose 内部主机名）且 secret 与 `.env` 不一致（历史 e2e 运行残留）→ 平台侧表现为"沉默"。已修复为 `http://192.168.65.254:8000/...` + `local-dev-webhook-secret`（Docker Desktop 容器内 `host.docker.internal` 会解析到无路由的 IPv6，须用 IPv4 网关——如实记录，属本机环境细节）。
3. **P2（产品缺口）** 文档上传失败后的恢复路径断裂：存储失败时文档行已提交（设计如此），但同 `canonical_uri` 重传永远撞 `uq_document_uri`（无 upsert），且无删除端点；IntegrityError 被误报为 `STORAGE_UNAVAILABLE`。该 URI 只能靠直接改库解锁。
4. 知识空间无创建 API/页面（eval 脚本也是直接 SQL 插入）——已在 Round 2 报告记录，此处复核确认。

## 复现命令

```
.venv/Scripts/python.exe docs/acceptance/load_test.py http://127.0.0.1:8000 <token> 10 10
# 变异：编辑 check_version 短路 → 重启 API → 并发探针 → revert → 重启 → 探针
.venv/Scripts/python.exe tests/e2e/e2e_chatwoot_loop.py   # 需 set -a; source .env
```
