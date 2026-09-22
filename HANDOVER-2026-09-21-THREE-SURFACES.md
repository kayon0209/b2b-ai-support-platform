# 交接：三个界面 + 一个循环 —— 2026-09-21 傍晚

> **这份是自包含的。** 接手只需读本文档。
> 目标不是"继续审计"，而是**按用户的三界面方案把产品做完**。
> 前序文档：`HANDOVER-CONTINUE-2026-09-21.md`（更早的审计交接）、`AUDIT-2026-09-21.md`（审计全文）、
> `docs/adr/0010-*.md`（被本文档的方案**修订**）、`.workbuddy-ai/memory/REFERENCE.md`（环境陷阱全表）。
>
> ---
> ### ⚠️ 接手前先读这一段（2026-09-21 晚场补充，深夜更新）
>
> **§5-A 数据卡片已实现并端到端跑通**（交付清单 `outputs/CARD-DELIVERY-2026-09-21.md`），
> 过程中的发现、证据与结论在 **`FINDINGS-2026-09-21-CARD-AND-RLS.md`**。要点：
>
> 1. **P0（已修）**：交互式 worker 的工作单元用 `session_scope()` → **bootstrap owner
>    （`rolbypassrls`）**，**run 全程绕过 RLS**，于是读工具开关 `agent.business_read_enabled`
>    取到了**另一个租户**的那一行 → 读工具分支被跳过 → 卡片进不去（同时也是跨租户隔离问题）。
>    现改为：**认领用 `queue_bookkeeping_session()`（owner，唯一允许的例外），每个事件用
>    `tenant_session`（app 角色 + 每事务重绑）**。守卫 `test_inbox_worker_isolation.py`（6 条，
>    含变异验证）；真实 worker 的 ask 模式守卫 35s 全绿。
> 2. **P1（未修，属另一会话）**：中文问法永远选不中读工具（5/5 落到 `knowledge_qa`，候选为空）。
>    根因在 `intent.py`，那是**另一会话正在改**的文件。证据表在 findings §2。
> 3. §5-C（`router.py` 重复入队）**已修**；§5-D（占位行）**查清后建议不动**，理由见 findings §8
>    （"入队即计入配额"是 `test_usage_counts_queued_runs` 断言的产品语义）。
> 4. 本文档 §5-E 的那 1 个失败**仍在**（`expected 42 migrations, got 43`）。**不要单独改计数**：
>    本地会变绿而干净 checkout / CI 仍红——那正是 §5-E 自己警告的"绿得说谎"。
> 5. 起服务除了本文档 §6.1，还**必须先跑 `scripts/seed_admin_demo.py`**（本次补种了 demo ERP
>    连接器 + 读工具开关；该部署的 `connectors` 表原本是空的，读工具连执行器都解析不到）。
> 6. 跑 `pytest` 前**必须停掉两个 worker 容器**：活的消费者会在 1 秒内抢走你刚种下的事件，
>    表现为"事件处理了 0 条 / run 永远 queued"（本次踩过，花了十几分钟才定位）。

---

## 0. 一句话现状

**"客户侧对话窗"今天从"不存在"做到"端到端可用"**（浏览器实测通过），
过程中修掉**四个真 bug**。**三个界面里还缺"数据卡片"和"坐席工作台相似工单"**，以及几处欠账（§5）。

---

## 1. 当前状态（**命令核对过的数字，勿凭记忆**）

| 项 | 值 |
|---|---|
| 仓库 | `D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan` |
| 分支 | `master` |
| HEAD | `b6611f1 docs: record the audit round, the consolidated memory and the current blockers` |
| 领先 origin | **56 个提交，全部未 push** |
| 未提交 | **28 项** = 我的 13 + 另一会话 14 + 混合 1 |
| 静态检查 | `ruff check` ✅ · `ruff format --check` ✅（370 文件）· `mypy` ✅（157 文件） |
| 全量测试 | 上次 1740 passed / 1 failed（那 1 个是 §5-E 的耦合，不是回归） |

### 1.1 未提交清单（**按归属分类，提交前务必照这个来**）

**我的 13 项**（今天做的，可以提交）
```
M  apps/admin-web/src/main.tsx                              # /support 顶层路由
M  apps/api/src/platform_core/agent_runtime/orchestrator.py # 根治：一次 run 一行
M  apps/api/src/platform_core/identity/middleware.py        # EXEMPT_PREFIXES += /v1/support/
M  apps/api/src/platform_core/identity/tenant_context.py    # ActorKind += "customer"
M  apps/api/src/platform_core/main.py                       # 注册 support_router
M  apps/worker/src/worker/inbox_consumer.py                 # 修重复 turn
M  .workbuddy-ai/memory/2026-09-21.md                       # 当日记录
M  .workbuddy-ai/memory/REFERENCE.md                        # 环境陷阱
?? apps/admin-web/src/pages/SupportChat.tsx                 # 客户对话窗页面
?? apps/admin-web/src/styles-support.css                    # 它的样式
?? apps/api/src/platform_core/agent_runtime/chat_service.py # 共享会话操作
?? apps/api/src/platform_core/agent_runtime/support_router.py # /v1/support
?? apps/api/src/platform_core/support_bridge/visitor_token.py # 访客令牌
```

**另一会话的 14 项**（**不要 `git add`，不要改**）
```
M  apps/admin-web/vite.config.ts
M  apps/api/src/platform_core/agent_runtime/intent.py
M  apps/api/src/platform_core/cases/models.py
M  apps/api/src/platform_core/knowledge/service.py
M  apps/api/src/platform_core/knowledge/storage.py
M  apps/api/tests/unit/agent_runtime/test_intent.py
M  docs/launch-checklist-and-runbook.md
M  docs/research/chinese-intent-measurement.md
M  docs/research/huaqiu-research.md
M  HANDOVER-2026-09-19.md
D  .workbuddy-ai/agent-handover-prompt.md
?? apps/api/migrations/versions/0040_case_attachments.py
?? apps/api/src/platform_core/cases/attachments.py
?? apps/api/tests/integration/test_case_attachments.py
```

**混合 1 项**（两边都写过，**提交前必须逐段确认**）
```
M  .workbuddy-ai/memory/2026-09-20.md
```

---

## 2. 今天建成并验证的：客户侧对话窗

### 2.1 为什么它之前不存在（**这条很重要，别再退回原判断**）

用户要求"做客户侧对话窗"。**ADR 0010 把它写成了"客户面是 Chatwoot，`/chat` 是内部验证面，
客户体验不在本仓库实现且不是缺口"** —— 用户的请求被上一个会话转成了一份**否决它的架构决策**，
我接手后又顺着这个框架一路用 Chatwoot 验证，被用户当场指出。

**纪律：别人（包括上一个会话）写下的 ADR 不等于用户的决定。用户说过的需求被 ADR 否决时，
要当面提出来，而不是继承。** 现已按 **ADR 0011** 的方向实现，ADR 0011 也已补写（见 §5-H）。

### 2.2 新增的后端

| 文件 | 作用 |
|---|---|
| `support_bridge/visitor_token.py` | `vs_<payload>.<sig>`，HMAC-SHA256 签名，绑定 **(tenant, conversation, external_ref) 三元**，带过期；`compare_digest` 防时序；**不含任何角色** |
| `agent_runtime/support_router.py` | `POST /v1/support/sessions`（唯一免鉴权，只发令牌）· `GET /v1/support/timeline` · `POST /v1/support/messages`（**一次调用完成"存 turn + 入队 run"**） |
| `agent_runtime/chat_service.py` | 把「读时间线 / 脱敏+去重写入 / 配额+背压入队」抽成**共享实现**，避免三处各写一遍。**授权刻意不共享**（操作员走 `CASE_READ/CASE_UPDATE`，访客只有绑定） |

**安全模型**：令牌不含角色，授权就是"绑定" —— 请求里**根本不带 conversation id**，所以猜不到别人的会话；
`role=None` 让所有操作员策略门自动拒绝它。花费由**租户月配额 + 全局队列深度**封顶（都是既有的）。

### 2.3 新增的前端

`pages/SupportChat.tsx` + `styles-support.css` + `main.tsx` 的 `/support` 顶层路由（与 `/chat` 同级，不套 Layout）。

**它刻意不走 `lib/api.ts`** —— 那个模块会附操作员令牌、并在 401 时弹令牌框，正是客户页最不该发生的事。

### 2.4 浏览器实测（真实 Chromium，空 localStorage，无操作员令牌）

```
标题(租户品牌): <img src=x onerror=alert(2)>Acme & Co
页面报错: (无)            输入框 disabled: false
是否带后台侧边栏: false     ← 独立页面 ✓
已发送: 你们的板子支持加急打样吗？多久能出货？
客服回复: I couldn't verify an answer from our authorized knowledge base...
UI_CHECK_EXIT=0
```

---

## 3. 今天修掉的四个真 bug（**都有实测证据，别改回去**）

### 3.1 会话 ref **被派生两次** —— "答复回不来"的真因

**证据**：API 把 turn 写在 `6995d1b4-…`，worker 把 run 写在 `92c2a744-…` —— **不是同一个会话**。

**根因**：`chat_service.queue_agent_run` 把**已派生好的 ref** 塞进 payload 的 `conversation.id`，
而 consumer 会**再派生一次**（`conversation_ref_for`）→ 答复记到别的会话名下，**产生了但永远找不到**。

**修法**：令牌里额外带**原始 external id**（`visitor_token` 的 `x` claim / `VisitorClaim.external_ref`），
入队时传 external 而非 ref。`router.py` 本来传的就是路径上的 external，所以它没这个问题。

> `customer_router._conversation_ref` 的 docstring **早就警告过这个失败模式**
> （"答案产生了却永远找不到"）。**改动涉及会话 ref 时先读它。**

### 3.2 同一次 run **写两行** → 已按根治方案修

**证据**：配额口径（`started_at IS NOT NULL`）统计 **434**，真实 run 只有 **171**，占位行 **263**；
单租户单月 **占位 30 / 真实 12 → 配额读成 42**，**虚增约 3.5 倍**。

**根因**：`router.py` / `chat_service` 写**占位**（`queued`, `input_hash=''`），
`orchestrator` 又写**真实 run** —— 同一次运行两行。**占位行的消费者从未被实现。**

**修法（根治）**：`orchestrator._adopt_or_create_run` —— 按
`(tenant, conversation_ref_id, status='queued', input_hash='')` **最旧优先**、`FOR UPDATE SKIP LOCKED`
认领占位行，**就地把** route/status=RUNNING/started_at/input_hash/配置写上去；认领不到才新建。
`started_at` 改为**实际开始执行的时间**（占位时是入队时间，留着会报出没发生过的延迟）。

**注意**：`test_m2_http_api.py:1088` 那条测试**专门钉住占位写入**（注释：`started_at` 必须填，
否则 `/v1/quality/metrics` 看不到 run）。**我的改动没有碰它**（入队端点行为未变），它仍应通过。

### 3.3 客户 turn **写两次**

**根因**：`inbox_consumer.persist_memory`（约 471 行）**无条件**再写一遍客户 turn，
而平台侧会话的 turn 在**消息被接受时就已经写过**。

**修法**：复用 `_local_turn_text(message_id)` 的结果 —— **取得到**（=这一行已存在）就**直接用它的 id，
不再 append**；取不到（Chatwoot 来源）才写。顺带保住 `source_turn_id` 指向**真实那一行**。

**实测**：`客户 turn 条数: 1 (期望 1) · 客服 turn 条数: 1 (期望 >=1)` ✓

### 3.4 `/v1/support` 前端走错代理前缀

**根因**：`vite.config.ts` 的代理是 **`/api`**（`rewrite: p => p.replace(/^\/api/, "")`），
`lib/api.ts` 会自动加 `/api`；我的页面绕过了它却直接请求 `/v1/...` → 拿到 SPA 回退的 **404**。

**修法**：页面里 `const API = "/api"` 并前置。

> **我误诊过一次**：先怀疑是 IPv6 端口幽灵（`[::1]:8000` 确有另一个监听者），**但那是错的**。
> **先读配置再猜机制。** 不过 `[::1]:8000` 的陈旧监听者是真的，所以 Vite 启动时
> **用 `VITE_API_TARGET=http://127.0.0.1:8000`**（显式 IPv4）更稳。

---

## 4. 你要实现的目标（用户原话）

**最终形态：三个界面 + 一个循环。**

| 界面 | 谁在用 | 看到什么 |
|---|---|---|
| **客户侧对话窗** | 客户 | 对话 + **数据卡片**（订单状态、物流节点可视化，**不是一段文字**） |
| **坐席工作台** | 客服 | 会话 + AI 建议话术 + 已查数据 + **相似工单**，不用切系统 |
| **运营后台** | 主管/总经办 | 自动化率、**漏点分析**（哪些问题还在转人工）、知识库维护 |

> "第三个界面的价值容易被低估 —— **'哪些问题还在漏'是这套系统最值钱的一张表**，
> 它直接告诉你下一步该自动化什么。"

**用户给的"一天"闭环**（要能演示）：
凌晨 2:14 工程师问"能加急吗" → 身份通过 → 查订单 → 识别改价类 → **风控拦截（AI 不许定价）**
→ 回复"已记录，9:30 前专员联系您" → 工单带全部上下文生成；
早上 9:05 坐席打开工单不用重问，**AI 副驾给出加急费参考（依据：价目表 v3.2 + 同类工单 3 条）**
→ 坐席确认 → 发送 → 客户确认 → 系统改单；
下午 3:00 主管看到"加急咨询 47 次，100% 转人工" → **标记为可自动化候选**；
一周后上线自动应答，该品类自动化率 **0% → 62%**。

**功能清单**：用户另外给了一份 100 项清单（`华秋智能客服-功能清单.md`，在用户微信目录里）。
其中"如果只做三件事"：**① 身份归一 + 订单交期查询 ② 风控闸门（6.1/6.2/6.3）③ 漏点分析 + 人工回流（8.1/7.8）**。

### 4.1 三个界面现状盘点（**已核对**）

| 界面 | 已有 | 缺 |
|---|---|---|
| 客户侧对话窗 | 后端 ✓ 前端 ✓ 闭环 ✓ | **数据卡片** |
| 坐席工作台 | `Workbench.tsx`：会话 ✓ **AI 建议话术**（`ai_suggestion`）✓ 数据卡片 ✓ | **相似工单** |
| 运营后台 | `QualityDashboard.tsx`：自动化率 ✓ **自动化候选** ✓；`GapQueue.tsx`：漏点分析 ✓ | 知识库运营台（8.4）等 |

---

## 5. 待实现（按优先级，含实现建议）

### A. 【最高】数据卡片（客户侧，用户明确说"不是一段文字"）

**现状**：`GET /v1/support/timeline` 只返回 `{role, text, at, source}`，**没有结构化字段**。

**要做**：
1. 后端：让 agent turn 带上结构化载荷。**不要新造机制** —— 项目里已有"工具回执/数据卡片"
   （功能阶段第 4A.3/4A.4 项做过，`test_receipt_publishing.py` 是它的测试）。
   先 `grep -rn "receipt" apps/api/src` 摸清它现在存哪、怎么读，然后把该载荷透出到时间线
   （建议：`ConversationTurn.ref` 里已经放了 `abstain:...` / `clarify:...`，可以按同样方式放
   `receipt:<id>`，或给时间线加一个 `card` 字段）。
2. 前端：`SupportChat.tsx` 渲染卡片（订单状态时间轴 / 物流节点），**纯文本气泡之外的另一条渲染分支**。
3. 测试：卡片渲染的守卫（参照 `scripts/ui_smoke.cjs` 的做法：断言"接口给了 N 个节点，页面渲染了 N 个"）。

**陷阱**：`expires_at` / 令牌里**不要**放卡片数据；卡片是展示层，数据从时间线接口来。

### B. 【高】坐席工作台补"相似工单"（规格 7.6）

**现状**：`Workbench.tsx` 有 `Suggestion`/`ai_suggestion`、会话、数据卡片；**没有相似工单**。

**要做**：后端加"相似工单"查询（同租户、同意图/同品类、最近 N 条），前端在工作台加一块。
**注意**：这是跨租户**绝对不能**泄漏的地方 —— 必须走 `tenant_session`（RLS），并补跨租户负向测试。

### C. 【中】`router.py` 里重复的入队实现改为委托 `chat_service.queue_agent_run`

`router.py` 的 `create_agent_run` 里有一份与 `chat_service.queue_agent_run` **重复**的实现
（约 80 行）。改成委托可去掉重复，顺带让它也走同一套配额/背压逻辑。
**注意**：`router.py` 传的 `conversation_ref` 是**路径上的 external**（正确），改的时候别传错。

### D. 【中】历史占位行 263 条

根治只防**新增**。现存 263 条仍占配额口径（单租户虚增 31）。**需要单独清理或改口径**。
**两种做法**：① 清理旧占位（注意：这些行可能是某次真实执行的重复记录，删前要确认）；
② 或改 `usage_snapshot` 口径排除 `input_hash=''`（治标，但一行改动、风险低）。
**先跟用户确认**——涉及租户数据。

### E. 【中】三件套必须同一个提交一起动

`0040_case_attachments.py`（**另一会话的**）提交时，必须同时：
1. `EXPECTED_MIGRATIONS` 42 → **43**（我为了自洽把它改成了 42，见 `79db89d`）
2. `test_migration_and_performance.py::TENANT_TABLES` **加上 `case_attachments`**（否则 RLS/存在性门禁不覆盖新表）
3. `test_cross_tenant_negative.py::TENANT_TABLES` 考虑加上（需同时补种子行）

**原因**：`migration_count` 来自 `ScriptDirectory.walk_revisions()`（**读磁盘**），
所以本地恒绿、**干净 checkout / CI 会红**。**测量 tracked 集合，不是磁盘。**

### F. 【阻塞】HEAD 现在是**断的**

已提交的 `cases/router.py` 引用了**未跟踪的** `cases/attachments.py` 与**未提交的**
`models.CaseAttachment` → 纯 HEAD 代码 `import platform_core.cases.router` 直接 **ImportError**，
`main.py:46` 就 import 它 → **应用启动即崩**。**任何干净 clone / CI 都会红。**
**修法只有一条**：等另一会话把附件相关文件一起提交。**在那之前不要 push。**

### G. 【决定】租户数据：**用户明确选择保留，不要动**

- `conversation_turns` 里 **10 组重复轮次**（各 n=2，文本 "What are your support hours?"）
- `admin-demo` 的 `brand_display_name` = `<img src=x onerror=alert(2)>Acme & Co`

**这是用户的决定，不是待办。** 除非用户再次明确要求，否则**不要清理**。

### H. 【文档】ADR 0011 —— 已写（2026-09-21 深夜，✅）

按 ADR 0010 自己的话，做客户面"**需要一份新的 ADR，因为它改变了谁能花掉租户的模型预算**"。
已写 `docs/adr/0011-customer-chat-is-a-visitor-session.md`：建议的三点都在，另加两点 —— 与 0010 三条论据的逐条对照（哪些成立、哪些被修订），以及一个如实写明的代价：访客会话与 Chatwoot 是两套记录，今日无任何关联，若 pilot 客户要在两者之间移动，那是下一次设计决定。0010 已标注 Amended。
访客会话 + 每会话签名令牌 + 无角色绑定 + 花费由配额/队列深度封顶；并把 0010 标注为被修订。

---

## 6. 环境、命令、陷阱

### 6.1 起服务（**注意 `--env-file`，见陷阱 1**）

```bash
cd "D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan"
docker compose --env-file .env -f infra/compose/docker-compose.yml up -d \
  ai-postgres ai-redis minio ai-api ai-worker-interactive ai-worker-outbox
# 前端（显式 IPv4，避开 [::1]:8000 的陈旧监听者）
cd apps/admin-web && VITE_API_TARGET=http://127.0.0.1:8000 \
  VITE_API_TOKEN=pt_admin-demo_8c89893c-09ce-4252-b839-971ac15e9a07 \
  node node_modules/vite/bin/vite.js --port 5173 --strictPort
```

| 入口 | 地址 |
|---|---|
| **客户对话窗** | http://localhost:5173/support |
| 管理后台 | http://localhost:5173 |
| Chatwoot | http://localhost:3000 |
| API | http://localhost:8000 |
| MinIO 控制台 | http://localhost:9001（`minioadmin`/`minioadmin`） |

令牌：`pt_admin-demo_8c89893c-09ce-4252-b839-971ac15e9a07`（`tenant_owner`）。

### 6.2 陷阱（**都是今天真踩到的**）

1. **`docker compose` 必须带 `--env-file .env`**。否则 `environment:` 里的 `${VAR:-}`
   解析为空并**覆盖** `env_file` 的值 → worker 拿不到 Chatwoot/LLM 密钥 →
   **唯一症状是日志里一行 `worker_cannot_send`**，而 API 正常、测试全绿、**客户永远收不到回复**。
2. **改了 Python 代码必须重建镜像**：worker/api 跑的是镜像，不是工作树源码。
   `docker compose --env-file .env -f infra/compose/docker-compose.yml up -d --build ai-worker-interactive ai-api`
   （否则你会对着"没生效的修复"调半天）。
3. **worker 日志是块缓冲的**，启动后一行都不刷。要观测就加 `PYTHONUNBUFFERED=1`。
4. **MinIO 的 `unhealthy` 是误报**（健康检查用 `mc`，而服务端镜像里没有 `mc`）。
   看 `/minio/health/live`，别看容器状态。
5. **`documents` bucket 没有任何代码会创建**，新卷必然导致所有上传 `NoSuchBucket`。
   用 `minio` python 客户端建（`.venv` 里有）。
6. **Vite 代理前缀是 `/api` 不是 `/v1`**；`lib/api.ts` 会自动加，绕过它的代码要自己加。
7. **全量跑得久 ≠ 在跑**：先看 CPU 时间（我遇到过一次 20 分钟只用 5.6 秒 CPU = 卡死），
   再探端口。容器可能已经 Exited。
8. **`curl` 用 `-o` 写文件、别用管道**（exit 23），且**加 `--noproxy "*"`**。
9. **`(echo > /dev/tcp/host/port)` 不能用来测连通性** —— `/dev/tcp` 是 bash 特性，
   Chatwoot 镜像的 `sh` 没有，会**全部误报不可达**。用 `ruby -rsocket` 或 `getent hosts`。
10. **停服务**：`psutil` 匹配 `vite.js` 时**别把 bash 包装进程一起匹配**（它的命令行里同时含
    "vite" 和 "node"，会把自己的 shell 杀掉）。
11. **两个会话共用这棵工作树**：**不要** `reset --hard` / `checkout --` / `clean -fd`；
    **不要** `git add` §1.1 里另一会话的 14 个文件（那会把人家半成品发布出去）。

### 6.3 验证命令（照抄）

```bash
cd "D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan"
R="$(cygpath -w "$(pwd)")"
export PYTHONPATH="$R/apps/api/src;$R/packages/contracts/src;$R/packages/policy/src;$R/packages/observability/src;$R/apps/worker/src"

./.venv/Scripts/python.exe -m ruff check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m ruff format --check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m mypy
mv tests/artifacts/release_gate_evidence.json /tmp/evidence.bak 2>/dev/null   # 沙箱守卫
./.venv/Scripts/python.exe -m pytest --junitxml=tests/artifacts/junit-final.xml
./.venv/Scripts/python.exe -m platform_core.evaluation.release_check --evidence-only

cd apps/admin-web && node ./node_modules/typescript/bin/tsc --noEmit \
  && node ./node_modules/vite/bin/vite.js build
```

**跑 pytest 前先停掉两个 worker**（活的队列消费者会让测试飘）：
```bash
docker compose -f infra/compose/docker-compose.yml stop ai-worker-interactive ai-worker-outbox
```

**两条运行时守卫**（需 API + 前端在跑）：
```bash
# 并发与限流（必须打真实服务；TestClient 单 portal，用它写的用例可能"没修复也能通过"）
APP_BASE_URL=http://127.0.0.1:8000 APP_TOKEN=pt_admin-demo_8c89893c-09ce-4252-b839-971ac15e9a07 \
APP_ADMIN_DATABASE_URL=postgresql://platform:platform@localhost:5435/platform \
./.venv/Scripts/python.exe scripts/concurrency_probe.py

# 接线类 bug（页面渲染行数 vs 接口返回行数）
NODE_PATH="C:/Users/Rose/.workbuddy-ai/binaries/node/workspace/node_modules" \
APP_BASE_URL=http://localhost:5173 APP_TOKEN=pt_admin-demo_8c89893c-09ce-4252-b839-971ac15e9a07 \
node scripts/ui_smoke.cjs
```

### 6.4 今天写的临时探针（可直接复用/参考）

```
C:/Users/Rose/AppData/Local/Temp/support_ui_check.cjs   # 浏览器跑客户对话窗（真实 Chromium）
C:/Users/Rose/AppData/Local/Temp/dup_check.py           # 客户 turn 是否只出现一次
C:/Users/Rose/AppData/Local/Temp/adopt_probe.py         # run 是否被认领（一次 run 一行）
C:/Users/Rose/AppData/Local/Temp/e2e_probe.py           # 经 Chatwoot 的闭环
```
（它们在临时目录，可能被清理；内容都很短，按需重写即可。）

---

## 7. 纪律（**这些是这棵仓库反复付出代价换来的，请沿用**）

1. **"测试全绿"不足以证明界面可用。** 今天 6 个 bug 就是在 1740 条全绿时存在的，
   且**全部出在"接线"与"失败时的表现"处**。界面改动用 `ui_smoke`，并发改动用 `concurrency_probe`。
2. **一个不会失败的守卫比没有守卫更糟。** 每条守卫都要做**变异测试**：把它改回去，确认它会红。
3. **测量，不要凭记忆或旧文档。** 今天两次撞上"全绿是混合工作树的性质，不是任何提交的性质"
   （`EXPECTED_MIGRATIONS` 与 HEAD 断裂）。
4. **先看清被检查对象的实际形态，再决定"通过"的标准。** 我今天自己误诊过一次
   （把代理前缀问题当成端口幽灵）。
5. **错误必须是真 4xx。** FastAPI 把返回的 **dict** 渲染成 200 —— 一律用 `error_response()`。
6. **改了启发式要前后各测一次**；**加枚举值前先 grep 有没有消费者**。

---

## 8. 可以直接粘贴给新会话的第一条消息

```
接手一个 B2B 智能客服平台。请先读 HANDOVER-2026-09-21-THREE-SURFACES.md（自包含交接文档）。

当前状态：HEAD = b6611f1，领先 origin 56 个提交（全部未 push）；
未提交 28 项 = 我的 13 + 另一个共用工作树会话的 14 + 混合 1（文档 §1.1 有分类）。
客户侧对话窗已端到端可用（浏览器实测通过），今天修掉四个真 bug（文档 §3）。

重要约束：
1. 工作树被两个会话共用 —— 不要 reset --hard / checkout -- / clean -fd，
   不要把 §1.1 里另一会话的 14 个文件 git add 进来。
2. HEAD 现在是断的（§5-F），不解决不能 push。
3. 租户数据用户明确选择保留（§5-G），不要清理。
4. 起服务必须 docker compose --env-file .env（§6.2 陷阱 1）；
   改了 Python 必须 --build 重建镜像（陷阱 2）。
5. 任何"测试全绿"都不足以证明界面可用（§7）。界面改动用 scripts/ui_smoke.cjs 验证，
   并发改动用 scripts/concurrency_probe.py，并做变异测试证明守卫会失败。

我想先做：<在这里写你要做的，建议是 §5-A 数据卡片>
```
