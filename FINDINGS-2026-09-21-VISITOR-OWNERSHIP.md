# FINDINGS — 访客身份校验与归属门（功能清单 2.2 / 2.5）

日期：2026-09-21 · 结论：**安全红线已落地并端到端实测通过；Chatwoot 已变为可选。**

---

## 一、结论先行

| 项 | 状态 | 证据 |
|---|---|---|
| Chatwoot 依赖移除（个人模式） | ✅ | 四个 chatwoot-* 服务加 `profiles: ["chatwoot"]`，默认不启动；访客闭环全程未用 Chatwoot |
| 2.2 身份验证（`/verify`） | ✅ | 订单号 + 手机尾号 → `verify_ownership` → 重签带 `account` 的 token |
| 2.5 归属校验（**P0 红线**） | ✅ | 匿名访客在任何连接器调用前被挡；跨账户读取被挡且无卡片泄漏 |
| 全量回归 | ⚠️ | 1 个既有失败（§5-E）+ 1 个 flaky（见第五节） |

---

## 二、红线实测（端到端，纯 API，8 秒，exit 0）

```
ok   anonymous session opened (token len 231)
ok   anonymous refused with identity-verification prompt
ok   no order data reached the timeline for an anonymous visitor
ok   verified as account "acme" via /verify
ok   verified acme read SO-9001 (its own order) and got the card
ok   acme asking about other-co's SO-9002 was refused (IDENTITY_MISMATCH)
ok   no SO-9002 card delivered to a non-owner
ok   wrong phone tail is refused by /verify

SUMMARY ownership gate: all three states correct (8s)
```

三态语义（`verified_account` 三态，不是布尔）：

- `None` = 操作员跑批 → 不设门
- `""` = 匿名访客 → **门 1 触发**（`IDENTITY_REQUIRED`，在任何连接器调用之前）
- `"acme"` = 已验证 → **门 2 触发条件**：回执 `account` ≠ 已验证账户 → `IDENTITY_MISMATCH`

复现：

```bash
node scripts/visitor_ownership_smoke.cjs      # 需 stack 已起、worker 在跑
```

---

## 三、一路修掉的 4 个真 bug

每一个都是「代码看起来对、测试也绿，但实际没生效」——靠端到端实测才暴露。

### 1. `ConnectorOutcomeExecutor` 不转发 `verify_ownership` → `/verify` 恒 503

`registry.py` 的包装类转发了 `verify_postcondition`，**没有**转发 `verify_ownership`。
`/verify` 用 `hasattr(executor, "verify_ownership")` 探测 → False →
`503 ownership verification is unavailable for this tenant`。

修复：`registry.py` 增加转发（内层没有该方法时返回 `None`，保持 fail-closed）。

### 2. 空字符串塌缩 ×3 → **门 1 永不触发**

`""`（匿名访客）在三个地方被塌成 `None`（语义是「操作员跑批」）：

- `chat_service.py` — `verified_account` 写入 inbox payload
- `inbox_consumer.py` — 从 minimized payload 取回
- `minimize.py` — `if isinstance(verified, str) and verified:` 丢掉空串

修复：三处保真传递 `""`。**这是最危险的一个**：门存在、代码在跑，但匿名访客从未被挡。

### 3. Prometheus label 名写错 → **归属门一触发就崩在告知之前**

`policy_denials_total` 声明 `labelnames=("action", "reason_code")`，
调用写成 `.labels(result="ownership")` → `prometheus_client` 抛 `ValueError`。

后果：门生效了（`read_ownership_refused` 已记日志），但 run 随即崩溃，
**客户收不到任何回复**——比没有门更糟。

修复：`action=Action.TOOL_READ.value, reason_code="IDENTITY_MISMATCH"`。
已全仓扫描其余 `.labels()` 调用点，无同类问题（另两处是正则误报，实际四个 label 都传了）。

### 4. 冒烟脚本读到了陈旧消息（测试自身的 bug）

state 2 / state 3 共用同一个已验证会话（这是设计，要测的就是这个会话），
而等待条件是「存在 agent 消息」→ 立刻命中**上一轮**的回复 → 把通过的门判成失败。

修复：先记基线，等**新**回复（`agentReplyCount(it) > base`）。

---

## 四、两个未修的环境 / 工程问题（需你授权）

### P1 · compose 的 `.env` 解析目录不一致 —— **两种调用方式都不完整**

`infra/compose/docker-compose.yml` 里两种取环境变量的机制，基准目录不同：

| 机制 | 基准目录 | 从仓库根跑 `docker compose -f infra/compose/...` | 加 `--project-directory .` |
|---|---|---|---|
| `env_file: ../../.env` | compose 文件所在目录 | ✅ 找到根 `.env` | ❌ 解析到上层目录，找不到 |
| `${APP_LLM_API_KEY:-}` 插值 | **project 目录** | ❌ 找 `infra/compose/.env`（不存在）→ 回落空串 | ✅ 读到真值 |

实测：

```
不带 --project-directory：APP_LLM_API_KEY: ""                        ← 全部容器现状
带 --project-directory .：APP_LLM_API_KEY: YTNK...(真值)
```

后果：**整个 stack 一直在没有 LLM key 的状态下跑**，run 全程 `MODEL_NOT_CONFIGURED`，
state 2 虽然出了卡片，但答复文本是兜底文案。
而加 `--project-directory .` 虽然修好了 key，却因 `env_file` 失效导致
API 起不来（`no authentication configured`）。

> 建议修法（未动）：把 `env_file` 改成相对 project 目录的 `.env`，
> 并把启动命令固定为 `docker compose -f infra/compose/docker-compose.yml --project-directory . up -d`，
> 写进 runbook。**两种机制统一到同一个基准目录。**

### P2 · Dockerfile 分层 —— 改一行源码触发 22 分钟重装

`infra/compose/api.Dockerfile` 把 `COPY apps/api/src ...` 放在 `RUN pip install` **之前**，
源码一变，pip 那一层的缓存就失效 → 每次改代码都整轮重装依赖（实测 **22 分 54 秒**）。

> 建议修法（未动）：把 `pip install -r requirements.txt` 移到源码 `COPY` 之前，
> 只留 `pip install -e .` 在源码之后。改完重建一轮，之后每次秒级。

迭代期的临时绕过（已在用）：`docker cp` 单文件进容器 + `docker restart`。
注意：之后**不能** `--force-recreate`，会丢掉 cp 的内容。

---

### P1b · worker 镜像与 api 镜像**分开构建** —— 与 Dockerfile 注释声称的相反，会漂移

`api.Dockerfile` 的注释写着 worker「reusing this exact image so API and worker
cannot drift apart」，但 compose 里 `ai-worker-interactive` / `ai-worker-outbox`
各有自己的 `build:` → **各自一个镜像**。

本次真踩到了：只 `build ai-api` 就 `--force-recreate` 全部服务，结果 worker 回到旧代码
（`docker cp` 的临时补丁被 recreate 丢弃）→ metric 修复回退 → 归属门一触发就又崩。
判断依据：容器里 `orchestrator.py` 的 `IDENTITY_MISMATCH` 出现次数 api=3、worker=2。

**必须三个服务一起 build**（预热后只要 7 秒）：

```bash
docker compose -f infra/compose/docker-compose.yml build \
  ai-api ai-worker-interactive ai-worker-outbox
```

> 建议修法（未动）：让 worker 服务不再各自 build，改为复用 `ai-api` 镜像 + `command:`
> 覆盖，与 Dockerfile 注释的意图对齐，从根上消除漂移。

---

## 五、测试状态

| 范围 | 结果 |
|---|---|
| `apps/api/tests` 全量 | 1 failed：`test_migrations_apply_and_are_reversible_on_fresh_database`（**§5-E 既有**，非本次回归） |
| `test_billing_ledger.py::test_usage_recorded_is_aggregated_through_the_relay` | **flaky**：单跑 5 次 4 通过 1 失败（`claimed=0`） |
| `test_visitor_ownership.py`（本次新增 5 条单测） | 通过 |

relay 那条是**时序 flaky，不是确定性回归**。`outbox_relay.py` / `runner.py` 在本工作区里
被**更早那一轮**改过（29 / 16 行），文档基线（1787 passed / 1 failed）当时它是绿的，
所以大概率是那轮引入的时序敏感，需要单独确认（见第六节）。

跑测试前务必：`unset ACC_PRODUCT_CONFIG_V3`（否则 teardown 会假失败）。

---

---

## 追加（同夜续做）：功能清单缺口复核 + 3.2 / 7.2+7.1 / 8.7

### 复核先行：审计本身有 3 处低估（已更正到 `outputs/功能清单覆盖度-2026-09-21.md`）

| 项 | 原判 | 实况 | 证据 |
|---|---|---|---|
| 6.1 承诺拦截 | ❌ | **✅** | `qa_path.redline_violations()` 句子级中英双语，在 `orchestrator.py:1269` 对**出站草稿**拦截，有 `test_redline.py` |
| 4B.1/4B.3/4B.4/4B.5 报价 | ❌/🟡 | **✅** | `platform_core/pricing/`（engine/parse/reference/service），`orchestrator` 调 `quote_label()`；`lead_time_tier`+`quantity`阶梯=4B.3，`QuoteBand`+`basis`=4B.4/4B.5 |
| 其余 ❌ 项 | ❌ | ❌（确认） | 逐项 grep 复核：1.3/1.4/1.7/3.7/5.2/7.2/7.10/8.6/8.7/9.5/11.2 确实 hits=0 |

汇总从 42✅/17🟡/21❌ 更正为 **53✅/14🟡/13❌**（P0 从 24/7/5 → **30/4/2**）。

### 本轮新实现

1. **3.2 业务线路由（P0）** — `BusinessLine`（元器件/PCB/SMT/DFM），中英词表，
   进审计快照；`\bpcb\b` 不匹配 pcba（PCBA=SMT）；发票/密码 → UNSPECIFIED（猜线会误转）。
   11 条测试。
2. **7.2 情感分析 + 7.1 第 7 类触发闭合（P0）** — `agent_runtime/emotion.py` 确定性词典
   （路由决策必须可复现），四级；ANGRY/ESCALATION_RISK 在检索前转人工，带专属
   `EMOTION_ESCALATION` reason 与命中词证据。**FRUSTRATED 不转人工**（否则队列被灌满）；
   情绪只改路由不改话术（避免安抚性承诺触碰 6.1 红线）。10 单测 + 5 集成。
3. **8.7 意图分布与趋势（P1）** — `aggregate_intent_distribution` 读 run 已有
   `model_config→intent`（**无需迁移**），出 scene/kind/business_line 分布 + 时间分桶趋势；
   `GET /v1/quality/intent-distribution`。无该维度的老 run 计 `unrecorded` 不丢弃。
   5 条测试。

### 验证
ruff + ruff format 全绿；全量 `apps/api/tests` **仅剩 1 failed = §5-E 迁移（既有）**。
`test_billing_ledger.py` 为全量序列下的 flaky（单独跑 3 次均 12/12 绿）。

### 剩余 P0 仅 2 项（需产品决策）
- **1.3 富媒体接收**：与"渠道 payload 刻意剥离媒体"的最小化原则直接冲突，需你拍板。
- **1.7 排队与等待体验**：客户侧排位/预计等待，队列深度目前只用于 429 背压。

---

## 六、建议的下一步（按优先级）

1. **P1 compose env 统一** —— 否则 LLM 永远不生效，答复质量无法验证。
2. **P2 Dockerfile 分层** —— 否则每次改代码 22 分钟，无法迭代。
3. **relay flaky 定位** —— 单独跑 `test_billing_ledger.py`，确认是否与 `outbox_relay.py`
   那轮改动有关；建议先钉住再动。
4. 提交基线：当前工作区有 41 个文件改动未提交，建议先 commit 再继续。

---

## 附：本次改动文件

- `tool_gateway/registry.py` — `verify_ownership` 转发
- `agent_runtime/orchestrator.py` — 两道归属门 + metric label 修正
- `agent_runtime/support_router.py` — `/verify` 端点、token 重签
- `support_bridge/visitor_token.py` — `VisitorClaim.account`
- `support_bridge/minimize.py` — 空串保真
- `agent_runtime/chat_service.py` — `verified_account` 入 payload
- `worker/inbox_consumer.py` — `verified_account` 透传
- `integrations/demo_erp.py` — `verify_ownership`、`_CONTACT_TAILS` 外置
- `integrations/business_read.py` — fail-closed `verify_ownership`
- `agent_runtime/qa_path.py` — `IDENTITY_REQUIRED` / `IDENTITY_MISMATCH` 文案
- `infra/compose/docker-compose.yml` — chatwoot profile
- `scripts/visitor_ownership_smoke.cjs`（新）、`tests/unit/integrations/test_visitor_ownership.py`（新）
