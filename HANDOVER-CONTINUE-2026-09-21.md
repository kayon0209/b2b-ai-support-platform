# 交接文档（用于新会话继续）— 2026-09-21

> ## ⚠️ 本文档的 §2/§3.2/§5/§7 已被 2026-09-21 下午的进展取代
>
> 下面这些数字和状态**已经不是当前状态**，不要据此行动：
>
> | 本文档写的 | 现在 |
> |---|---|
> | HEAD = `1924f4b` | `1322186` |
> | 领先 origin 50 | **55**（仍未 push） |
> | 未提交 44（29 我的 + 14 他们 + 1 混合） | **20**（14 他们 + 6 我的） |
> | 我的 17 个审计修复**未提交** | **已提交**，分 4 个提交 |
> | `EXPECTED_MIGRATIONS = 43` ✓ 一致 | 已改为 **42**（43 是数工作树数出来的） |
>
> **当前状态请看 `.workbuddy-ai/memory/2026-09-21.md`（当日记录）与 `REFERENCE.md`。**
>
> **另有一个本文档没有的阻塞项**：**HEAD 现在是断的** —— 已提交的 `cases/router.py` 引用了
> 未跟踪的 `cases/attachments.py` 与未提交的 `models.CaseAttachment`，纯 HEAD 代码
> `import platform_core.cases.router` 直接 `ImportError`，**应用起不来、任何 clone/CI 都会红**。
> 必须等另一会话把附件相关文件一起提交才能解。**在那之前不要 push。**
>
> §1 / §4 / §6 / §8 / §9 的背景、决策与陷阱仍然有效。

> **这份是自包含的。** 新会话只需读本文档即可接手，不必先读其它文件。
> 更深的细节在：`HANDOVER-2026-09-21.md`（功能阶段）、`AUDIT-2026-09-21.md`（审计全文）、
> `docs/adr/0010-*.md`（本次唯一的新架构决策）。
> 文末附**可直接粘贴给新会话的第一条消息**。

---

## 0. 一句话现状

**功能清单已全部实现（50 个提交，未 push）；随后做了一轮 10 回合的用户旅程审计，
发现并修复 17 个问题，但这 17 个修复全部尚未提交。**

---

## 1. 背景与目标

**项目**：B2B 企业智能客服平台（对标华秋电子，个人复刻项目，**没有外部系统**）。

**架构定位**（关键，决定了后面很多决策）：**这不是一个聊天产品，是一个 AI 控制平面。**

```
客户 → Chatwoot（外部、有界上下文）→ 签名 webhook → Support Bridge → 本仓库
                                        ↑ 回程：AI --REST--> Chatwoot
```

本仓库 = `apps/admin-web`（React 后台）+ `/v1/*` 管理 API + 一个 webhook 入口。
**平台自己不拥有客户会话内容**（Chatwoot 拥有），只拥有租户、账户、工单/SLA、
知识库、AgentRun、ToolExecution、Citation、Evaluation、AuditEvent。

**两阶段目标**：
1. **功能阶段**：按《华秋智能客服-功能清单》逐项核对（**实测，不凭文档**），补齐缺失。
2. **审计阶段**：用户要求按**真实用户旅程**走查，排查功能 bug / 安全 / 性能 / 资源泄漏 / UX。

---

## 2. 当前仓库状态（准确数字，勿凭记忆）

| 项 | 值 |
|---|---|
| 仓库路径 | `D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan` |
| 分支 | `master` |
| HEAD | `1924f4b docs: document the workbench, corrections and quality-metrics contracts`（2026-09-21 10:12） |
| 领先 origin | **50 个提交，全部未 push** |
| 未提交文件 | **44 个**（含本交接文档；其中 **10 个是新文件**） |
| 其中**我的审计修复** | **29 个** |
| 其中**另一会话**的文件 | **14 个**（见 §7.3，我未审阅过） |
| 其中**混合**（两边都改过） | **1 个**：`.workbuddy-ai/memory/2026-09-20.md` |
| 迁移 | 43 个，`EXPECTED_MIGRATIONS = 43` ✓ 一致 |
| 全量测试 | **1741 / 0 失败 / 0 错误 / 0 跳过** |

**关键**：工作树被**两个会话共用**。另一会话有一批未完成的改动（附件功能、intent、
knowledge 等）混在同一批未提交文件里。**审计的 17 个修复也全部未提交。**

---

## 3. 已完成的工作及产出

### 3.1 功能阶段（50 个提交，已提交）

按功能清单实现并验证，主要项：
投诉 L6 转人工 · 身份归一（contact→账户绑定 + 可见性 + 渠道记录）· 坐席工作台 ·
漏点分析与可自动化候选 · 工具回执发布（数据卡片）· 非工作时间兜底 · 影子模式 ·
技能组路由 · 报价引擎 + 公开参考价目表 · 人工修正回流 · 本地演示 ERP · 附件感知 ·
外部系统故障话术。

### 3.2 审计阶段（17 个修复，**未提交**）

产出：`AUDIT-2026-09-21.md`（10 个回合的完整报告：复现步骤、证据、修复、验证）。

**17 个问题**：`F-1…F-10`（功能）、`S-1…S-3`（安全）、`U-1…U-4`（体验）。

**其中 6 个是 1741 条测试全绿也抓不到的**，且**全部出在"接线"与"失败时的表现"处**：

| 编号 | 问题 | 为什么测试抓不到 |
|---|---|---|
| **F-5** | 坐席工作台队列**恒为空**（库里 21 条） | 页面读 `data.cases`，接口返回 `{items,total}`；`apiGet<T>` 泛型是**断言不是校验**，且**没有测试打开过页面** |
| **F-7** | 品牌配置**到不了客户面**（设置等于无效） | 客户面品牌是硬编码的 |
| **F-8** | 提示词页两个控件**没有可访问名** | 只有 `placeholder`（不是可访问名） |
| **F-9** | 幂等去重**并发下失效**（10 并发写入 4 条） | SELECT-then-INSERT 无原子性；顺序测试看不到；**库里已有 10+ 组真实重复** |
| **F-10** | 重新启用开关可能**瞬间恢复放量**且无确认 | 需理解 `enabled` 是覆盖放量的 kill switch |
| **U-4** | 宕机显示成一句 "HTTP 500" | 需在真实代理形态下把 API 停掉才能看到 |

### 3.3 新增的两条可复用守卫（零新项目依赖）

| 文件 | 作用 | 关键 |
|---|---|---|
| `scripts/ui_smoke.cjs` | 按页面核对"**接口返回了 N 行，页面是否真的渲染了 N 行**" | **已做变异测试**：把 F-5 改回去，它精确报 `FAIL /workbench: /v1/cases returned 23 row(s) and the page rendered 0` |
| `scripts/concurrency_probe.py` | 并发去重 + 限流 | 必须打**真实服务**（`TestClient` 单 portal，用它写的用例可能"没修复也能通过"） |

**这两条守卫的核心教训**：**一个不会失败的守卫比没有守卫更糟**。两条都做过变异测试，
`ui_smoke` 甚至**改过两版**才成为真守卫（第一版用关键词匹配会误报；第二版 URL 匹配写错导致
`served` 恒为 0，**断言永远不成立**）。

---

## 4. 已做出的关键决策及原因

### 4.1 `docs/adr/0010-platform-chat-is-internal.md`（唯一的新架构决策）

**决定**：平台**不做**客户身份方案。**客户面是 Chatwoot，`/chat` 是内部验证面。**

**为什么**（不是偏好，是可查的约束）：
- **AGENTS.md 第 1 条**把 Chatwoot 定为客服内核；再做一个客户渠道是重复它的职责。
- **平台已有的客户身份句柄就是 `conversation_ref`**，来自 Chatwoot 的**签名 webhook**。
  在这里另发明一套，是给一个已答的问题再造一个更弱的答案——**被暴露出去的会是弱的那套**。
- **未鉴权的发送路径不是中性的便利**：每条被接受的消息排队一次 agent run = 一次模型调用。
  "任何人都能发"就是"任何人都能花钱"。

**已实现**：`/chat` 面板标注「内部验证面」（悬停："客户渠道是 Chatwoot"），浏览器实测通过。

### 4.2 F-9 用**咨询锁**而不是唯一索引

**为什么不用唯一索引**：① 它会**同时约束 Chatwoot 摄入路径**（那条路径记录本端点看不到的消息，
有自己的保留理由），用本端点的去重语义去约束它属于越界；② **库里已有重复数据，索引会创建失败**。
咨询锁只串行化"同一逻辑消息的并发写入者"，零迁移、对其它路径零影响。

### 4.3 F-3 修**两侧**（前端 busy 禁用 + 后端重复保护）

幂等键防的是"网络重试"，**不防双击**。而 `GapQueue` 每次点击生成**新键**、按钮又无禁用 →
双击真的建两份草稿（`create_draft` 只挡 `RESOLVED`，不挡 `DRAFTED`）。

### 4.4 S-1 要求 `APP_ENVIRONMENT` **被显式声明**

bootstrap 令牌是**未签名**的（`pt_<slug>_<user-id>`），知道 slug + 成员 UUID 即可冒充。
原防护（默认关、部署环境拒绝启动、fail closed）**是扎实的**，但 `environment` **默认 `"local"`**
→ 部署时忘设 + 沿用本地 `.env` 就能绕过。现在用 `model_fields_set` 区分"声明 vs 默认"。

### 4.5 4B 报价：公开数据 + **出处**，且**不发布任何"编造"的价目表**

数据取自公开资料（PCBSync，2026-08-20，USD/FOB Shenzhen），**每个区间都带
`NON-CONTRACTUAL` 警告与版本**。`per_cm2` 是拟合的 → **写了测试反过来验证它能否复现公开单价**。
**公开数据没有板厚定价** → 只 1.6mm 可报价；**加急区间 +30–150% 太宽** → 不报价。两者都转人工。

### 4.6 7.8 人工修正：**绝不自动学习**

AGENTS.md 明令禁止从未审核会话学习。生命周期 `记录(CASE_UPDATE) → 审核(KNOWLEDGE_PUBLISH)
→ approved|dismissed`。**批准不等于发布**——批准只说明"这条是对的"，不说明"它写得像文档"。

### 4.7 10.1 本地演示 ERP：记录带 `source: "demo"`

没有外部系统 → 用本地样例数据实现 `business_api` provider，**每条记录带 `source: demo`**
（同价目表的道理：**没有出处的数字最危险**）。接真实系统只需把 `business_api_adapter` 改为 `http`。

### 4.8 我**没有**做的事（也是决策）

- **没有**把 `/chat` 端点改成公开（会打开"任何人灌消息并触发 LLM 跑"的口子）。
- **没有**顺手本地化客户端错误文案（`TIMEOUT`/`NETWORK` 等既有消息本来就是英文；
  本地化是另一件事，混进来只会让改动难以审阅）。
- **没有**擅自删除租户数据（重复轮次、XSS 载荷品牌名）——见 §6。
- **没有**提交任何东西（用户要求"不能保证就别急"，而**我确实不能保证**——见 §6.1）。

---

## 5. 待办事项与优先级

| 优先级 | 事项 | 说明 |
|---|---|---|
| **P0** | **决定是否提交，以及如何提交** | 见 §7.2 的三组计划；**另一会话的 14 个文件不要动** |
| **P0** | **决定是否重写 `1924f4b`** | 它混入了另一会话的文档小节 → HEAD 中文档描述了实现仍未提交的端点（见 §6.2） |
| **P1** | 手工清理两处租户数据 | 重复轮次、`admin-demo` 的 XSS 载荷品牌名（见 §6.3） |
| **P1** | 把两条守卫接进 CI / 发布前检查清单 | `scripts/ui_smoke.cjs`、`scripts/concurrency_probe.py` |
| **P2** | 未验证项补测 | F-2 的 45 秒**时长本身**、小时级资源问题（见 §6.4） |
| **P2** | 功能清单剩余项 | **无阻塞项**；剩余都需要业务输入（真实价目表、制程矩阵）或外部系统 |

---

## 6. 已知问题与风险

### 6.1 **我不能保证"提交即无问题"**（这是当前最重要的风险）

| 范围 | 把握 |
|---|---|
| 我的审计修复（未提交） | 每条都跑了测试 + **真实环境验证**；F-3/F-9/F-10 有专门验证；两条守卫做过变异测试 |
| **另一会话的 14 个文件** | **我没有审阅过** |
| "全绿"的含义 | **不等于没问题**——本轮 6 个 bug 就是在全绿时存在的 |

### 6.2 **我自己在功能阶段犯过一次错**（已查清范围）

`1924f4b`（我的文档提交）把另一会话在 `docs/api-contracts.md` 里的**未提交小节**
`## Case evidence attachments` 一起提交了。证据：该小节在父提交 `de1aef` 中**不存在**（0 处），
在我的提交中存在（1 处）。

- **影响**：HEAD 的文档描述了 `/v1/cases/{id}/attachments`，而实现仍未提交 → **文档与代码不一致**。严重性低，另一会话提交代码后自愈。
- **污染范围**：用"会话开始时已修改的文件清单" ∩ "我提交过的文件" = **只有这一个**。
- **修法**：未 push，可安全重写（`git reset --soft HEAD~1` 后只重新 add 我写的三节）。**但这是共享工作树里的历史重写，且另一会话可能正在提交 → 需确认后再动。**

### 6.3 两处**租户数据**问题（我未擅自修改）

- **库里已有的 10+ 组重复客户轮次**（F-9 的真实后果）——新的咨询锁**只防新增**。
- **`admin-demo` 租户的品牌名是一条 XSS 载荷**：`<img src=x onerror=alert(2)>Acme & Co`
  （新校验只拦后续写入；不清掉的话客户面会一直显示它）。**注意**：它经 React 转义、
  **在后台不可执行**，不是可利用的 XSS，但是**脏数据 + 潜伏风险**（将来任何 HTML 消费端会执行）。

### 6.4 未验证项（我明确标注的，不含糊）

- **F-2 的 45 秒默认时长本身**未实测（只验证了分支逻辑与轮询停止）。
- **小时级资源问题未覆盖**：项目既有记录里 ~20 小时会出现 `WinError 10055`（socket 耗尽）。
  本轮只做了 3 分钟 / 1640 请求的长跑（RSS +0.5MB、连接数不累积、无短期泄漏迹象）。

### 6.5 过程风险：**我的检查本身出错 5 次**

审计中我**误报 3 次**（"启用开关应确认"、"确认时传错哈希被接受"、"有提议但列表为空"），
**断言过宽 1 次**（把 `HTTP 5\d\d` 当成"可操作的提示"，而它正是 U-4 本体），
**断言写错 1 次**（用 `/停用/` 判断"是否仍关闭"，但该行同时含状态"已停用"与按钮"启用"）。

**共同根因**：**没先看清被检查对象的实际形态，也没先想清楚"通过"的标准是什么。**
它们**没有变成假问题交出去**，只因为我每次都查证后才写进报告。**新会话请沿用这个纪律。**

---

## 7. 涉及的文件 / 代码 / 配置位置

### 7.1 审计修复涉及的文件（**未提交**，都是我的）

**后端**
```
apps/api/src/platform_core/main.py                    # U-2 404/405 统一信封 + trace_id
apps/api/src/platform_core/identity/middleware.py     # S-3 令牌 rpartition 解析
apps/api/src/platform_core/config.py                  # S-1 bootstrap 令牌要求显式 APP_ENVIRONMENT
apps/api/src/platform_core/identity/branding.py       # F-6 display_name 校验
apps/api/src/platform_core/knowledge/gap_service.py   # F-3 create_draft 去重
apps/api/src/platform_core/agent_runtime/customer_router.py  # F-9 咨询锁
apps/api/src/platform_core/pricing/service.py         # 仅 ruff format
apps/api/tests/integration/test_knowledge_gaps.py     # F-3 测试
apps/api/tests/integration/test_tenant_branding.py    # F-6 测试
apps/api/tests/unit/test_config_auth_guard.py         # S-1 测试（新文件）
apps/api/tests/unit/pricing/test_engine.py            # 仅 ruff format
```

**前端（11 个）**
```
apps/admin-web/src/lib/api.ts              # S-2 编译期令牌仅 dev 生效 + U-4 5xx 提示
apps/admin-web/src/lib/i18n.tsx            # 上述所有改动的文案（中英）
apps/admin-web/src/components/TokenDialog.tsx        # F-4 退出登录
apps/admin-web/src/main.tsx                # U-1 /workbench/:caseId 路由
apps/admin-web/src/pages/Cases.tsx         # U-1 ?case= / ?offset=
apps/admin-web/src/pages/Workbench.tsx     # F-5 data?.items（**核心修复**）+ U-1
apps/admin-web/src/pages/GapQueue.tsx      # F-3 busy 禁用 + U-1 ?tab=
apps/admin-web/src/pages/PromptRelease.tsx # F-3 busy 禁用 + F-8 aria-label
apps/admin-web/src/pages/FeatureFlags.tsx  # F-10 重新启用确认
apps/admin-web/src/pages/CustomerChat.tsx  # F-1/F-2/U-3 + ADR 0010 标注
apps/admin-web/src/styles.css              # 相关样式
```
**注意**：`lib/types.ts` 与 `pages/QualityDashboard.tsx` 也改过，但**已在 `24fb009` 提交**，
不在未提交清单里——别去找它们。

**新增文件（10 个未跟踪）**
```
docs/adr/0010-platform-chat-is-internal.md   # 唯一的新架构决策
scripts/ui_smoke.cjs                         # 接线类 bug 守卫（已变异测试）
scripts/concurrency_probe.py                 # 并发/限流守卫
AUDIT-2026-09-21.md                          # 审计全文
HANDOVER-CONTINUE-2026-09-21.md              # 本文档
.workbuddy-ai/memory/2026-09-21.md           # 当日工作记录
apps/api/tests/unit/test_config_auth_guard.py            # S-1 测试
apps/api/migrations/versions/0040_case_attachments.py    # ← 另一会话的
apps/api/src/platform_core/cases/attachments.py          # ← 另一会话的
apps/api/tests/integration/test_case_attachments.py      # ← 另一会话的
```

**已修改但属于我的**
```
HANDOVER-2026-09-21.md   # 功能阶段创建并提交过（4902f2f），本次追加了审计一节
```

### 7.2 建议的提交分组（**只准备，未执行**）

1. **鉴权与信封加固**：`main.py` + `middleware.py` + `config.py` + `test_config_auth_guard.py`
2. **数据完整性**：`branding.py` + `gap_service.py` + `customer_router.py` + 两个测试文件
3. **界面与文档**：前端 11 个文件 + `docs/adr/0010` + `AUDIT-2026-09-21.md` + 两个 scripts
4. **不要动**：§7.3 的 14 个文件

### 7.3 **另一会话**的文件（**不要 `git add`**）

```
apps/api/migrations/versions/0040_case_attachments.py   (新)
apps/api/src/platform_core/cases/attachments.py         (新)
apps/api/tests/integration/test_case_attachments.py     (新)
apps/api/src/platform_core/agent_runtime/intent.py
apps/api/src/platform_core/cases/models.py
apps/api/src/platform_core/knowledge/service.py
apps/api/src/platform_core/knowledge/storage.py
apps/api/tests/unit/agent_runtime/test_intent.py
apps/admin-web/vite.config.ts
docs/launch-checklist-and-runbook.md
docs/research/chinese-intent-measurement.md
docs/research/huaqiu-research.md
HANDOVER-2026-09-19.md
.workbuddy-ai/agent-handover-prompt.md   (被删除)
```

### 7.4 **混合**文件（两边都改过，**最需要小心**）

```
.workbuddy-ai/memory/2026-09-20.md   # 会话开始时已是修改状态（他们的）+ 我追加的当日记录
```

这类文件是 §6.2 那次失误的成因：**同一文件里既有我的段落也有别人的段落**。
提交前**要看整个 diff 里有没有不属于自己的段落**，不能只看自己关心的那几行。

核对口径：29（我的）+ 14（他们的）+ 1（混合）= **44** ✓

---

## 8. 必要的上下文与依赖

### 8.1 环境

- Python：`.venv/Scripts/python.exe`（3.12）。**受管 3.13 没有 pytest，别用。**
- **`PYTHONPATH` 必须绝对路径且用 `;` 连接**（否则 `No module named 'platform_core'`）：
  ```
  $R/apps/api/src;$R/packages/contracts/src;$R/packages/policy/src;$R/packages/observability/src;$R/apps/worker/src
  ```
  Git Bash 里 `$R` 用 `cygpath -w "$(pwd)"` —— Python-on-Windows 不认 POSIX 路径。
- 端口：ai-pg **5435** · chatwoot-pg 5434 · ai-redis 6380 · Chatwoot **3000** ·
  API **8000** · Vite **5173**（**只监听 IPv6 `[::1]`，用 `localhost` 而非 `127.0.0.1`**）。
- 数据库角色：`platform`（超级用户，绕过 RLS，用于 seed/清理）、`platform_app`（NOBYPASSRLS）。
- **API 必须用 `python -m platform_core.main` 启动**，不能用裸 uvicorn
  （ProactorEventLoop → psycopg async 拒绝 → 每个 DB 请求都在连接处失败 → 表现为 `401 AUTH_UNRESOLVED`）。
- **`curl` 要加 `--noproxy "*"`**：宿主机代理（:55940）会用 502 回答 `localhost`，看起来像路由坏了。

### 8.2 验证命令（**照抄**）

```bash
cd "D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan"
R="$(cygpath -w "$(pwd)")"
export PYTHONPATH="$R/apps/api/src;$R/packages/contracts/src;$R/packages/policy/src;$R/packages/observability/src;$R/apps/worker/src"

./.venv/Scripts/python.exe -m ruff check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m ruff format --check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m mypy
mv tests/artifacts/release_gate_evidence.json /tmp/   # 沙箱守卫：否则 release_check exit 2
./.venv/Scripts/python.exe -m pytest --junitxml=tests/artifacts/junit-final.xml
./.venv/Scripts/python.exe -m platform_core.evaluation.release_check --evidence-only

cd apps/admin-web && node ./node_modules/typescript/bin/tsc --noEmit \
  && node ./node_modules/vite/bin/vite.js build
```

**两条守卫**（需要 API 与前端在跑）：
```bash
# 接线类 bug
APP_BASE_URL=http://localhost:5173 APP_TOKEN=pt_<slug>_<uuid> node scripts/ui_smoke.cjs
# 并发与限流
APP_BASE_URL=http://127.0.0.1:8000 APP_TOKEN=pt_<slug>_<uuid> \
APP_ADMIN_DATABASE_URL=postgresql://platform:platform@localhost:5435/platform \
./.venv/Scripts/python.exe scripts/concurrency_probe.py
```

### 8.3 本地令牌与浏览器驱动

- 令牌格式：`pt_<tenant-slug>_<user-id>`。本次审计用的租户是 `admin-demo`，
  令牌形如 `pt_admin-demo_8c89893c-09ce-4252-b839-971ac15e9a07`（`tenant_owner`）。
  **从库里取**：`SELECT t.slug, m.user_id, m.role FROM memberships m JOIN tenants t ON t.id=m.tenant_id WHERE m.status='active';`
- 浏览器驱动：本机已有 Chromium（`C:/Users/Rose/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe`）
  与 `playwright-core`（在受管 node 工作区 `C:/Users/Rose/.workbuddy-ai/binaries/node/workspace/node_modules`）。
  跑脚本：`NODE_PATH=<上述路径> node script.cjs`。**不需要装任何东西。**

### 8.4 本会话踩过、值得继承的陷阱

- **后台进程**：Bash 工具的 `nohup ... &` 会在调用结束时被回收；**用 `run_in_background: true`**。
- **`cmd && python > file`** 有时莫名 `exit 23` 且文件不生成 → **用任务捕获输出更可靠**。
- **`head`/`tail` 接管道会 SIGPIPE 杀掉长脚本** → 脚本写结果到文件，再读文件。
- **heredoc 里的 `${...}` 会被 shell 展开**（`Bad substitution` / `\n` 变 `/n`）→ 复杂编辑用 Edit/Write 工具。
- **全量红灯不是证据**：本项目有过"同一份代码 6 次运行得到 3/0/2/0/10/2 个失败，每个单独跑都是绿"的记录。
  **先单跑失败用例再相信它。**

---

## 9. 建议的下一步行动（按顺序）

1. **先做两个决定**（都只需一句话）：
   - 是否按 §7.2 分三组提交我的文件（**不碰** §7.3）？
   - 是否重写 `1924f4b` 去掉混入的文档小节？
2. **手工清理两处租户数据**（§6.3）。这两件事只有你能做——我**不擅自改租户数据**。
3. **把两条守卫接进发布前检查清单**（它们已验证能失败，成本几乎为零）。
4. **若要继续功能开发**：功能清单**无阻塞项**。剩余项都需要业务输入
   （华秋真实价目表与制程矩阵）或外部系统（真实 ERP），**不能靠推理补**。
5. **若要继续审计**：已覆盖的 10 个回合见 `AUDIT-2026-09-21.md`；**未覆盖**的是
   F-2 的 45 秒时长、小时级资源问题、以及**另一会话那 14 个文件本身的正确性**。

---

## 10. 可直接粘贴给新会话的第一条消息

```
接手一个 B2B 智能客服平台的审计修复。请先读
HANDOVER-CONTINUE-2026-09-21.md（自包含交接文档），再读
AUDIT-2026-09-21.md（审计全文）。

当前状态：HEAD = 1924f4b，领先 origin 50 个提交（全部未 push）；
工作树有 43 个未提交文件，其中 14 个属于另一个共用工作树的会话，
我审计的 17 个修复全部未提交。全量测试 1741 / 0 失败。

重要约束：
1. 工作树被两个会话共用 —— 不要 reset --hard / checkout -- / clean -fd，
   不要把 §7.3 里另一会话的 14 个文件 git add 进来。
2. 我尚未授权提交。请先复述你打算提交哪些文件、分几组，等我确认。
3. 验证必须照抄 §8.2 的命令；注意 release_check 需要先移走 evidence 文件，
   且 API 必须用 python -m platform_core.main 启动。
4. 任何"测试全绿"都不足以证明界面可用 —— 本轮 6 个 bug 就是这样漏掉的。
   涉及界面改动用 scripts/ui_smoke.cjs 验证，涉及并发改动用
   scripts/concurrency_probe.py 验证，并做变异测试证明守卫会失败。

我想先处理：<在这里写你要做的事>
```
