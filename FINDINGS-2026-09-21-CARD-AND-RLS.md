# 发现：数据卡片做完了，但两件事让它到不了客户手里

> 日期：2026-09-21 晚 · 作者：接手会话（数据卡片任务）
> 关联：`HANDOVER-2026-09-21-THREE-SURFACES.md`（§5-A 本次实现）、`AUDIT-2026-09-21.md`
>
> **本文所有结论都是实测的**，命令与原始输出在每节里。凡未实测的都标了"未验证"。
>
> **2026-09-21 深夜更新**：P0-1 **已修复**（§1.6），"数据卡片"与"worker 隔离"两条均已实测通过。
> 同批还清掉了 §5-C 的重复入队实现（§6），并把 outbox relay 的同类例外写明（§7）。

---

## 摘要

数据卡片（§5-A）本身**已实现并端到端跑通**（证据见 §4）。但为了让它在**运行中的部署**里出现，
我撞到两个此前没人测过的东西：

| # | 结论 | 级别 | 状态 |
|---|---|---|---|
| P0-1 | **worker 的 agent run 全程在"绕过 RLS"的角色下执行**，于是特性开关取到了**别的租户**的那一行 | P0 | **已修**（§1.6） |
| P1-1 | **中文问法永远选不中读工具**（全部落到 knowledge_qa），"订单交期查询"目前只有英文能用 | P1 | 不在本次改动范围（属另一会话的文件） |
| P1-2 | 真实 ERP 适配器的回执**永远不会被发布**（`fetched_at` 整数被脱敏破坏 JSON） | P1 | **已修** |
| — | §5-C：`router.py` 与 `chat_service` 各有一份入队实现 | 维护性 | **已修**（§6） |
| — | outbox relay 同类：认领与派发共用 owner 会话（同 §1 的根因） | 中 | 例外已**写明**，修法留待独立变更（§7） |

P0-1 是这两个里最重要的一条：它同时是**跨租户隔离**问题和**卡片进不去**的直接原因，现已修复。

---

## 1. P0-1：agent run 在绕过 RLS 的角色下执行（**已修**）

### 1.1 代码事实

`apps/worker/src/worker/runner.py:188`（交互式 worker 每个事件的工作单元）：

```python
async with session_scope() as session:          # ← 没有传 URL
    return await drain_once(session, deps=self._deps, batch=self._config.batch)
```

`session_scope()` 用 `get_session_factory()`，即默认 `settings.database_url`
（`postgresql+psycopg://platform:platform@…`）。而这是 **bootstrap owner：超级用户**
（实测 `rolname=platform, rolsuper=t, rolbypassrls=t`）。

同一个文件里，`session_scope_with_url` 的 docstring 恰好点名了这个坑：

> `"""Session scope against an explicit URL (e.g. the non-bypass app role).
> The bootstrap owner is a superuser and would bypass RLS entirely."""` — `db.py:114`

也就是说：**同一份代码里，一处显式用了 app 角色，另一处（交互式 worker 的主循环）没有。**

### 1.2 实测：RLS 确实没生效

在运行中的 worker 容器内，把 `evaluate` 前后的 `app.tenant_id` 与被取到的行一起打出来：

```
flagprobe__agent_business_read_enabled__want_17ab2c52
  __g1_17ab2c52__row_26c95619_False__g2_17ab2c52__en_False_DISABLED__g3_17ab2c52
```

- `want_17ab2c52` = 被评估的租户 `admin-demo`
- `g1/g2/g3_17ab2c52` = 查询**前、中、后** `current_setting('app.tenant_id')` 都是 `admin-demo`
- `row_26c95619_False` = **但它取到了 `huaqiu`（另一个租户）的那一行**，`enabled=false`

`feature_flags` 的 RLS 是强制开启的，策略为
`tenant_id::text = current_setting('app.tenant_id', true)`。在 `admin-demo` 绑定下，
**只有 `admin-demo` 的行可见**——除非角色绕过 RLS。

对照实验（同一容器、同一个库）：

| 会话角色 | 可见 `feature_flags` 行数 | `_load_flag` 结果 |
|---|---|---|
| `platform_app`（`app_role_url()`） | `admin-demo` 自己的 **11** 行 | `17ab2c52 True 100` ✓ |
| `platform`（`session_scope()` 默认） | **全部 13** 行 | `26c95619 False 0` ✗ |

### 1.3 为什么这一条会"杀了卡片"

`flag_service._load_flag` 是 `WHERE key = :key` + `.first()`，**没有 ORDER BY**，
且它的 docstring 明确说这是**故意**的：

> "`evaluate` deliberately omits the owner scope and relies on RLS: the tenant being
> *evaluated* is not necessarily the tenant that *owns* the flag definition"

在 RLS 生效时这个推理成立；在 RLS 被绕过的角色下，**同名 key 有两个租户各一行，`.first()`
返回哪一行由执行计划决定**（实测同一份代码在不同时刻取到不同行）。于是：

```
business_read_flag_off   ← 读工具分支被跳过
run_abstained  route=business_read  reason_code=EVIDENCE_BELOW_THRESHOLD
```

客户看到的是"I couldn't verify an answer from our authorized knowledge base"——
**不是"知识库没有答案"，而是"这个租户的读工具被别人的开关关掉了"。**
两者对客户和看板完全一样，但修法相反。

### 1.4 影响面（比卡片大）

run 期间的**所有**数据库读写都在绕过 RLS 的角色下。AGENTS.md 第 5 条
（"PostgreSQL RLS must protect tenant-owned tables"）在**这条路径上不成立**。
剩下的防线只有应用层自己写的 `WHERE`。

- 已知没有暴露：会话语义靠 `conversation_ref_id`（uuid5 派生，不可猜），`_load_flag` 是**配置**
  不是客户数据。
- **未验证**：run 路径上是否存在某条查询只靠 RLS 兜底而没有应用层租户条件。
  这次没有做系统性排查，不应当作"没问题"。

### 1.5 修法（**已实施**，见 §1.6）

不能简单把 `session_scope()` 换成 app 角色：`claim_pending()` 必须**在知道租户之前**
看到所有租户的待处理行（`outbox_relay` 里也有同样形状的 claim）。所以拆成两步：

1. **认领**仍用 owner 会话（现有行为，只做 claim）；
2. **处理**每个事件时换一个 `tenant_session(ctx)` 会话。

两个必须注意的点（都实测过）：

- `set_config(..., true)` 是**事务作用域**。实测 `绑定 → commit → UNKNOWN_FLAG`：
  任何时候 commit 之后都要**重新绑定**，否则 RLS 下什么都看不到（返回 0 行、
  开关读成 `UNKNOWN_FLAG`）。`lease_service` 自己就 commit，所以租约必须在 run 之前取、
  绑定必须在取完之后再做一次。**这就是为什么用 `tenant_session` 而不是自己 `apply_rls_tenant`**
  —— 前者在 `after_begin` 重新绑定，对每个后续事务都成立。
- 补一条**守卫**：worker 的工作单元断言 `current_user = 'platform_app'`。

### 1.6 实施内容与验证

**一个抽象归位（结构性的，不是补丁）**

`tenant_session(ctx)` —— 仓库里"app 角色 + 每事务重绑"的唯一实现 —— 原本住在
`platform_core.api`（该模块的 docstring 自称"shared **HTTP** helpers"）。worker 要复用它就得
import 请求层；它当初没 import，而是自己写了一遍 `apply_rls_tenant`，于是写成了只绑定一次的形式。
现已移到 `platform_core.identity.tenant_context`（RLS 层，`apply_rls_tenant` 的同一个文件），
`api.py` 用 PEP 484 的显式再导出保留 40 个既有调用点，两个已成死代码的私有别名删除。

**两步会话**

| 位置 | 之前 | 现在 |
|---|---|---|
| `worker/wiring.py` | 无 | `queue_bookkeeping_session()`：owner 角色，**docstring 写明"只用于认领，不得读写租户数据"**，并说明为什么不能用 `SECURITY DEFINER` 函数替代（迁移计数被 §5-E 的未跟踪版本耦合） |
| `worker/runner.py` | `session_scope()` | `queue_bookkeeping_session()`（调用点自解释） |
| `worker/inbox_consumer.drain_once` | 认领+处理同一个 owner 会话 | 认领/回收/标记在 bookkeeping 会话；**每个事件一个 `tenant_session(_event_context(event))`**，`process_event` 不再自己绑定 |
| `drain_once` 的提交点 | 整批结束时一次 | **认领后立即提交**（行锁必须释放，否则跨会话处理会互相争锁；顺带让"崩在批中"的行可被 `reclaim_stale_processing` 回收——之前 claim 活在同一事务里，崩了就整体回滚），之后每事件提交一次 |

**验证（全部实测）**

1. **行为**：两个租户、同一个 flag key、值相反（A=`true`，B=`false`）。A 的事件跑完后
   `agent_runs` 是 `route=business_read, abstain_reason=TOOL_UNAVAILABLE` —— 只有**进入读工具分支**
   才可能产出这个 reason code，即开关读的是 A 自己的值。
2. **变异**：把处理阶段改回 bookkeeping 会话（`nullcontext(session)`），reason 立刻变成
   知识库路径的 `NO_AUTHORIZED_EVIDENCE` → 上面那条断言会红。**守卫是真的。**
3. **部署链路**：重建镜像后，通过真实的 `/v1/support/messages` 提问，worker 日志出现
   `tool_read_executed tool_name=order.get_status`，`conversation_turns` 长出 `tool` 回执行，
   run 被**就地认领**（`input_hash` 非空，不是残留的占位行）。
4. **测试**：`apps/api/tests/integration/test_inbox_worker_isolation.py`（6 条）——
   含角色断言、`after_begin` 重绑断言（`绑定 → commit → 仍绑定`，无此监听器会读到 0 行）、
   以及一条读源码的守卫（`inbox_consumer.py` 里不得再出现 `session_scope()`）。

**这次"测试全绿"仍然不代表没问题**：整个缺陷在 1773 条测试全绿时存在，因为它只在
**两个租户 + 同名 flag** 时才可观测，而夹具从来只种一个租户。

---

## 2. P1-1：中文问法永远选不中读工具

实测（`classify` + `select_read_tools`，工作树代码）：

| 问法 | route | action | 候选工具 |
|---|---|---|---|
| `What is the status of order SO-9001?` | `business_read` | `call_read_tool` | `order.get_status`, … |
| `Where is order SO-9001?` | `business_read` | `call_read_tool` | `order.get_status`, … |
| `SO-9001 现在到哪一步了？` | `knowledge_qa` | `answer_from_knowledge` | **（空）** |
| `我想查一下 SO-9001 这个订单什么时候能发货` | `knowledge_qa` | `answer_from_knowledge` | **（空）** |
| `订单号 SO-9001 的生产进度如何` | `knowledge_qa` | `answer_from_knowledge` | **（空）** |
| `这批料有货吗？货期几天？` | `knowledge_qa` | `answer_from_knowledge` | **（空）** |
| `物流到哪了` | `knowledge_qa` | `answer_from_knowledge` | **（空）** |

结论：**四个读工具（订单/物流/发票/库存）在中文下一律不可达**。
`select_read_tools` 只在 action 为 `call_read_tool` 时给候选，而中文问法都被判成
`knowledge_question` → 直接走知识库。

对用户"如果只做三件事"的第①项（**身份归一 + 订单交期查询**）来说，这是**阻断级**的：
产品唯一的真实语言下，这条路径不会触发。

根因在 `intent.py` 的 kind 判定（"live data requested" 信号）。
另外 `selector.py::_READ_SUBJECT_NOUNS` 里 CJK 名词**只给了 `inventory.check_stock`**
（"有货"/"库存"/"货期"/"现货"），订单/物流/发票是纯英文名词——
但这不是主因：主因是 action 先被判成了 `answer_from_knowledge`。

**本次没有改**：`intent.py`、`tests/unit/agent_runtime/test_intent.py`、
`docs/research/chinese-intent-measurement.md` 是另一个会话正在改的三个文件（见交接文档 §1.1），
改它们会把对方半成品发布出去。这条归他们的 `chinese-intent-measurement` 议题。

---

## 3. P1-2：真实 ERP 适配器的回执永远不会被发布（**已修**）

`orchestrator._survives_redaction` 拒绝发布"脱敏后不再是合法 JSON"的回执——这个判断是对的。
但它对生产者的要求是：**任何一个长得像电话号码的字段都会让整份回执发不出去**。

`business_read.py` 原来返回 `"fetched_at": int(time.time())`，10 位整数。实测：

```
real(epoch int): replacements=1 still_json=False
   -> {"fetched_at": [PHONE], "found": true, ...}
demo(ISO str):   replacements=0 still_json=True
```

所以**demo 适配器（ISO 字符串）能出卡片，真实适配器（epoch 整数）永远出不来**——
而测试全绿，因为 `test_business_read_receipt.py` 里那个"假真实执行器"返回的**是 ISO**
（第 64 行），夹具与实现不一致。

修法：`fetched_at` 改为 ISO 8601，与 `case_create._iso` 的既有约定一致
（同一仓库、同一理由：回执给人看的时间不应该是需要心算的时间戳）。
新增守卫 `test_the_real_adapter_receipt_survives_the_turn_store_redactor`
钉住这个性质——**把修复回退成 epoch，它会红**（已做变异验证）。

已知未解：`shipment.track` 的 `tracking_no` 若是 10 位以上的数字串
（demo 数据 `SF1234567890` 就是），同样会被脱敏成 `SF[PHONE]`。
这次**没有动脱敏器**：不在 JSON 值内部误伤是另一个话题，不该顺手改安全规则。

---

## 4. 数据卡片：实现与证据

### 4.1 做了什么

| 文件 | 作用 |
|---|---|
| `agent_runtime/tool_card.py`（新） | 把已发布的回执归一化成**结构化卡片**；白名单字段；两种适配器封装（demo 扁平 / 真实 `record` 包裹）只在这里处理 |
| `agent_runtime/chat_service.py` | `read_timeline` 增加 `card` 字段（**加法**，`text` 原样保留） |
| `admin-web/components/ToolCard.tsx`（新） | 唯一的卡片渲染器；标签走 props（客户页刻意不进操作员 i18n 包） |
| `admin-web/pages/SupportChat.tsx` | `tool` 分支渲染卡片（**不再是文本气泡**）；`system` 走中性提示；**修掉"tool turn 会提前结束等待"** |
| `styles-support.css` | 卡片与阶段时间轴的样式 |
| `scripts/seed_admin_demo.py` | 补种 `business_api` 连接器 + `agent.business_read_enabled`（否则读工具连执行器都拿不到） |
| `scripts/support_card_smoke.cjs`（新） | 浏览器守卫：**接口给了 N 个节点，页面渲染了几个** |

不新造存储：回执本来就在 `role=tool` 的 turn 里（`orchestrator._publish_receipt`），
卡片是**读取侧**的归一化——历史 turn 自动获得卡片，且不需要迁移。

### 4.2 端到端实测

在 app 角色下跑一次真实 run（真实 deps、真实选择器、真实 Tool Gateway、真实 demo ERP）：

```
flagprobe__agent_business_read_enabled__…__row_17ab2c52_True__en_True_ROLLOUT
tool_read_executed  tool_name=order.get_status  status=executed
[role=tool] card=YES
  {"kind": "order_status", "title": "SO-9001", "status": "in_production",
   "nodes": [{"label": "下单", "state": "done", …}, {"label": "工程确认", …},
             {"label": "生产", "state": "active", …}, {"label": "出货", "state": "pending"}],
   "eta": "2026-09-26T00:00:00+00:00", "quantity": 500,
   "fetched_at": 1789948800, "provenance": "demo"}
```

浏览器守卫（真实 Chromium，`/support`，空 localStorage 起步后注入该会话）：

```
ok   adopted an existing conversation (no run needed)
ok   session accepted, composer enabled
ok   a card was rendered
ok   stages: served=4 rendered=4
ok   order SO-9001 is on the card
SUMMARY [adopt] the card, the data behind it and the answer agree
exit 0
```

复现该会话（token 是给该 `(tenant, conversation)` 签的访客令牌，1 小时内有效）：

```
conversation_ref = 0f13d25e-124b-4d00-abf7-37b5036942e6   # 探针建的那次 run
tenant_slug      = admin-demo
```

### 4.3 守卫的变异测试（证明它会红）

| 变异 | 结果 |
|---|---|
| `ToolCard` 不再渲染阶段列表 | `FAIL the timeline served 4 stage(s) and the page rendered 0` → exit 1 |
| `SupportChat` 把 `tool` turn 当文本气泡渲染 | `FAIL … waiting for locator('.tool-card')` → exit 1 |
| `business_read.py` 回退成 epoch `fetched_at` | `assert False is True`（`_survives_redaction`）→ 红 |
| `chat_service` 的 `card` 恒为 `None` | `assert None is not None` → 红 |

---

## 5. 建议的下一步（按优先级）

1. **P1-1 交给正在做中文意图的那个会话**（它已经在 `chinese-intent-measurement` 上）。
   需要的话把 §2 的表贴过去——那是"产品语言下读路径不可达"的最小证据。
2. 装好连接器与开关后（本次已加到种子脚本），`scripts/support_card_smoke.cjs` 的 **ask 模式**
   现在在真实 worker 上也能跑通（§1.6 第 3 条）；接 CI 时把它和 `ui_smoke.cjs` 一起跑。
3. outbox relay 的同类例外按 §7 的修法单独做一次变更（它会改变该类的单元-of-work 契约，
   需要自己的验证）。
4. §5-D 的历史占位行：**本次查清了它的性质，建议不动**（见 §8）。

---

## 8. §5-D 的占位行：查清之后，"改口径"是错的

交接文档把它记成"现存 263 条占位行仍占配额口径（单租户虚增 31）"，并给了两条路：清数据，或改
`usage_snapshot` 口径排除 `input_hash=''`。**实测之后两条都不该走**。

**现状（实测）**

| 租户 | agent_runs 总数 | 未认领占位 | 真正执行过 |
|---|---|---|---|
| `admin-demo` | 58 | **32** | 26 |
| `e2e-main` | 160 | 0 | 160 |

**为什么"改口径"是错的**：`test_tenant_usage.py::test_usage_counts_queued_runs` 明确断言
**入队即计入配额**（`_queue_run()` 之后立刻 `runs_used == 1`，此时还没有任何 worker 跑过它）。
这是一条刻意的产品语义：配额门的作用是**限制花费**，而花费的承诺在入队那一刻就发生了。
把占位行排除出配额，等于允许租户在超额状态下继续无限排队 —— 那不是修 bug，那是**削弱滥用防线**。

**那 32 行为什么存在**：它们是 §3.2（同一次 run 写两行）在修复前的历史产物，即同一个问题
被**计了两次**（一次占位、一次真实）。所以正确的描述不是"口径偏高"，而是
**"旧 bug 在历史数据里留下的重复计数"**。

**结论**：口径是对的，不该改；32 行是忠实的历史记录，删除属于**动租户数据**，
按 §5-G 的用户决定（保留）与交接文档自己的要求（先确认），本次**不动**。
若将来要清理，判据是"这份占位行对应的 inbox 事件已由真实 run 答复过"，而不是"它看起来没用"。

---

## 6. §5-C：入队实现在两处（**已修**）

`agent_runtime/router.py` 的 `create_agent_run` 与 `chat_service.queue_agent_run` 是同一套逻辑写了两遍：
配额门 → 背压门 → inbox 行 → 占位 run → 审计 → 响应。现在 router **委托**给
`chat_service.queue_agent_run`，`QueueRefused` 携带要返回的响应（429 的 code/details 不变）。

为保留 router 的审计血缘，`queue_agent_run` 新增可选 `audit_extra`，合并进
`agent_run.queued` 的 `after`（router 记录它收到的 `expected_control_version`；
该字段"仅记录、权威 CAS 在 orchestrator"是模块 docstring 明确写过的设计，不是缺陷）。

**顺带修掉一处潜在不一致**：router 原来把**路径字符串**放进 inbox payload 的
`conversation.id`，但 `conversation_ref_id` 是用 `parse_uuid()` 归一化后的形式派生的。
对标准小写 uuid 两者相同，但 `uuid.UUID()` 也接受 `{...}`、urn 前缀、大写等写法——
那些写法下 payload 与 ref 会指向**不同会话**（正是 §3.1 那一类"派生了两次"的缺陷，只是提前一步）。
现在统一传 `str(external_ref)`，两者按构造一致。

验证：`test_m2_http_api.py` + `test_customer_answer_roundtrip.py` 共 41 条通过
（含那条专门钉住占位行写入的用例）。

---

## 7. outbox relay：同一根因的例外，已写明未改

`OutboxRelay` 的认领与派发共用**一个 owner 会话**（`OutboxWorker.run_once`），所以
`_dispatch` 里逐行的 `apply_rls_tenant` 同样是**装饰** —— 这正是 §1 的根因，只是发生在
计费/通知路径而不是答复路径。

**本次没有改它的理由**（这是一个判断，不是遗漏）：

- 修法与 §1 相同（认领用 bookkeeping、每行派发用 `tenant_session`），但它会改变该类**文档化并
  被测试钉住的单元-of-work 契约**（"整批一个事务，崩在批中则整批重投"）；改成每行一个会话
  就是每行提交，是一套**不同的投递去重语义**，需要自己的设计与验证。
- 这个部署**没有真实下游消费者**（默认 handler 是 `log_only_handler`），无法端到端验证投递。
  在没有验证手段的情况下改投递语义，正是本仓库反复吃亏的那一类改动。

**已做**：把例外显式命名并写进 `OutboxWorker.run_once` 的 docstring ——
owner 角色的使用现在有名字（`queue_bookkeeping_session`）、有边界说明、有"handler 必须
显式按 `tenant_id` 限定"的硬要求，以及修法与代价。同日 `runner.py` 与 `inbox_consumer.py`
里对 owner 角色的使用也都收敛到同一个命名上：**全仓库 owner 角色的使用点从"散落"变成"两处具名"**。
`tests/integration/test_outbox_relay.py` 覆盖投递，但它看不见缺失的边界——这一点也写在了注释里。

---

## 9. §5-B 相似工单：交接文档记错了现状，而 trigram 对中文是盲的

交接文档 §5-B 写"Workbench.tsx 没有相似工单"。核对后发现**前后端都已经有**：
`/v1/cases/{id}/workbench` 返回 `related_cases`，`Workbench.tsx:187` 渲染它。
真正的问题是这块**没有相关性**：

- 查询 = 同 `category` + 最近 5 条，而 **`category` 默认 `general`**（admin-demo 23 条里 9 条）；
- `basis` 这个 API 枚举被原样拼进用户可见标题（`相似工单 (same_category)`）。

### 9.1 先量再设计：第一版方案被实测推翻

第一版用 `pg_trgm.similarity()` 当门槛。**实测（本库）：**

| 对 | similarity |
|---|---|
| `能不能加急` vs `加急打样多久` | **0.000** |
| `这批料有货吗` vs `现货库存查询` | 0.000 |
| `Short circuit claim` vs `Short circuit claim on batch 42` | 0.613 |
| `Short circuit claim` vs `Earlier claim` | 0.222 |

**结构性原因**：pg_trgm 用 3 字窗口，共享词"加急"在两个串里分别是"能加急/加急打"，永远对不上。
**trigram 对拉丁文是可用信号、对产品真实语言是盲的** —— 照第一版上线，坐席侧永远显示"没有相关工单"。

### 9.2 实现

`cases/service.find_related_cases`：**词项重叠**（拉丁词 ≥3 字符 + **CJK 二元组**），
Dice 系数排序；**词频自适应裁剪**（窗口内 >50% 标题都有的词不算共享证据；样本 <30 不启用，
避免小样本误伤）——不手写停用词表，因为"哪些词泛"取决于租户（"order" 对元器件商每单都有）。
两级信号都保留但**如实标注**：`match: subject | category`、每行带 `shared_terms`；
**桶匹配封顶 2 条**并排在措辞匹配之后。无迁移（`cases` 上没有 trigram 索引，
`RELATED_WINDOW=500` 是当前的界，索引属 schema 变更，另议）。

前端：标题不再拼 `basis` 枚举；每行 Badge"措辞相近/同类工单"+ 共同词；i18n 中英补齐。

### 9.3 验证

- `test_case_workbench.py` 8 条：排名（措辞匹配在前）、桶匹配封顶并标注、**跨租户负向**
  （另一租户**标题完全相同**的诱饵工单绝不出现，且面板非空——空面板会让负向测试空过）、basis 如实。
- 部署栈实测：`GET /v1/cases/{id}/workbench` 对 "EQ 0813448-B: confirm stackup (from console)"
  返回 5 条，前 4 条是同类 EQ 确认，**第 4 条是同单号的原始工单（score 0.667）**——
  正是坐席要的"这条咨询我们之前怎么处理的"。
- 全量：1788 条用例 / 1 failed（§5-E 既有）。
