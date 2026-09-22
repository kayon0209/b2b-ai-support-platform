# 8.3 会话回放：交付 + 两个必须如实说明的发现

日期：2026-09-22
范围：功能清单 8.3（全链路会话回放），以及做这项时被逼出来的两个数据层结论。

---

## 一、交付

| 层 | 文件 | 说明 |
|---|---|---|
| 服务 | `apps/api/src/platform_core/agent_runtime/replay.py` | `list_conversations` / `build_replay` |
| 端点 | `apps/api/src/platform_core/agent_runtime/router.py` | `GET /v1/conversations`、`GET /v1/conversations/{ref}/replay` |
| 页面 | `apps/admin-web/src/pages/Conversations.tsx` | 路由 `/conversations`（侧边栏 🔁） |
| 样式 | `apps/admin-web/src/styles.css` | 末尾新增回放布局块（纯布局，颜色全用既有 token） |
| 测试 | `apps/api/tests/integration/test_conversation_replay.py` | 21 条，全绿 |

验证：`ruff` 全绿；`npm run build`（tsc -b + vite build）通过；全量 `apps/api/tests` **仅剩 §5-E（既有）**。
真实数据实测（开发库该租户）：列表 5 条、`awaiting_agent=23`，回放一条 6 轮会话，3 条 run 全部按哈希精确归因。

---

## 二、决策如何关联到"哪一句话"（这是回放的地基）

**规则**：`conversation_turns.text_hash == agent_runs.input_hash`。

- 两列都是对**未脱敏原文**的完整 `sha256`（`conversation_store.append_turn:52` 与
  `chat_service.append_customer_turn` 都是先哈希原文再丢弃原文）
- 已用真实数据实测命中：`dc2641243e12…` 同时是客户轮次的 `text_hash` 与那条
  `business_read / IDENTITY_MISMATCH` run 的 `input_hash`

**为什么不用时间对齐**：按 `ts` 就近匹配在会话繁忙时最不可靠，而"会话为什么变忙"恰恰是有人打开回放的原因。

**答复侧不宣称归因**：`agent_runs.output_hash = sha256(draft)[:16]`（`orchestrator.py:1412` 截断），
且草稿与实发文本可能不同 —— 实测 agent 轮次的 `text_hash` 与任何 `output_hash` 都对不上。
所以回放把决策挂在**触发它的提问**下面，答复按顺序展示，并在载荷里写明 `matched_by: "input_hash"`。
若强行宣称"这条回复来自那条 run"，会在两者不一致时出错 —— 而不一致的场景正是值得回放的那种。

---

## 三、发现 1（P0/口径）：**"原文"在平台里不存在，不是 UI 问题**

你要的是回放展示原文。实测结论：**做不到，因为库里没有原文**，这不是回放能选的一个选项。

证据链：

1. `conversation_turns` 的实际列（`information_schema`）：`id, tenant_id, conversation_ref_id, role,
   text_redacted, text_hash, ts, ref, source, created_at` —— **只有 `text_redacted`**。
2. 全库扫描：`column_name ~* 'raw|original|unredact|plain'` → **0 行**。
3. 写入点 `chat_service.append_customer_turn`：`redacted, _count = redact_text(text)`，
   原文在该函数内被丢弃，注释写着 "the platform must not hold raw customer PII"。
4. 渠道入口 `support_bridge/minimize.py`：`content` 被**显式排除**，只留 `content_length`。
5. `ConversationTurn` 的文档字符串直接写了答案：**"The raw message lives only in Chatwoot"**。

**那回放展示的是什么**：客户**原话本身**。`evaluation/pii.py` 的 `_VALUE_PATTERNS` 只替换三类值：

| 模式 | 替换为 |
|---|---|
| 邮箱 | `[EMAIL]` |
| 13–19 位卡号 | `[CARD]` |
| 电话 | `[PHONE]` |

也就是说，除这三类值外，字句与客户写的一模一样。页面已把这件事**显式写在回放顶部**，
避免操作员把一个 `[PHONE]` 误读成客户没写电话。

**要真正的未脱敏原文，只有两条路（都需要你决策，我没做）**：

- **A. 从 Chatwoot 取**（`support_bridge/chatwoot_client.py` 已有客户端）。只对 Chatwoot 来源的会话可行；
  平台自有的访客通道从未保存原文。代价：新增一条**出境读取**路径 + 它自己的访问控制 + 留痕。
- **B. 新增原文落库**。这是**存储策略变更**，与现有最小化红线（同一份代码里三处注释都把它当硬约束）
  直接冲突，需要保留期、访问控制、审计、告知口径一起设计。**不建议**，除非有明确的合规依据。

**我的判断**：A 如果要做，应当是一个独立决定 + 独立批次，而不是回放页面的一个开关。

---

## 四、发现 2（已修复）：占位 run 永久滞留，且**污染配额与看板**

> **修复结论（2026-09-22 当日完成）**
>
> ⚠️ **先纠正我自己的错数**：上一版说"287 条"是**跨租户**的。`platform` 是
> **超级用户**，超级用户**绕过 FORCE RLS**（`relforcerowsecurity=t` 对 superuser 无效），
> 所以我用 psql 查的 287/276/458 是全库，服务层（`platform_app`）看到的只有 **34**。
> 以后核对数据一律**显式带 `tenant_id` 过滤**，不依赖 RLS 帮我兜底。
>
> **实测到的真实缺陷（已修）**：`usage_snapshot` 的谓词只有 `started_at`，
> 把"排队但从未执行"的 run **也算进配额**。实测该租户 9 月：
> **配额 76，实际执行 42**（虚高 81%）。且这是**活的**：`usage_snapshot` 不只是报表，
> 它是 `chat_service.queue_agent_run:178` 的 429 准入闸门。
>
> 更重的是看板：占位 run 带着排队默认路由 `knowledge_qa` 且**没有意图快照**，
> 于是 `route_counts` **凭空报告不存在的路由流量**，8.7 的 `unrecorded` 桶
> 被"从未跑过"的行灌满（该桶本意是"早于该字段的旧 run"）。
>
> **差点做错的修复**：第一版我把"未执行"直接排除出用量 —— 这会让闸门对刚排队的 run
> 视而不见，并发突发可全部放行（成本漏洞）。**是既有测试 `test_usage_counts_queued_runs`
> 抓住了我**：它断言排队 run 必须计入用量，那条断言是对的。
>
> **最终方案**：不改"计不计"，而是让状态自己说清 ——
> 新增终态 `RunStatus.ABANDONED` + 清扫器
> `agent_runtime/abandoned.py`，接入 retention 循环（per-tenant、system actor）。
> - `usage_snapshot` 只排除 `abandoned`（**待执行的仍计入 → 闸门完好**）
> - 两处质量聚合排除**所有未执行**（未执行就没有结果可统计）→ 谓词单一来源
>   `models.run_executed()`
> - 排除量都**返回给调用方**（`usage.abandoned`、`metrics.never_executed_runs`），不静默丢
> - 清扫只碰"有 `started_at` 且够旧"的行（无时间戳 = 无年龄 = 不判断）
>
> **实测验证**：`drain_retention_once()` → 11 个租户、**废弃 34 条**、exit 0；
> 清扫后该租户 `runs_used` 立即回落。
>
> **顺带修掉两个残留来源**：
> 1. `test_m2_http_api.py` 的清理**删租户但不删 `agent_runs`** → 每跑一次留下 254 行
>    **属于已删除租户**的 run，任何按活跃租户枚举的清扫都永远够不着它们。已补
>    `citations` + `agent_runs` 删除（实测残留归零）。
> 2. `test_intent_distribution.py` 的夹具用 `status='completed'` + 空 `input_hash`
>    —— 生产里不可能出现（编排器执行时必写该字段）。已修正夹具。
>
> **仍留一处（需你决定是否删）**：dev 库里那 254 行**孤儿 run**（租户 `…00c1` 已不存在）。
> 它们对任何租户范围查询都不可见、也不影响任何指标。清理命令：
> ```sql
> DELETE FROM citations WHERE agent_run_id IN (SELECT id FROM agent_runs WHERE tenant_id='01900000-0000-7000-8000-0000000000c1');
> DELETE FROM agent_runs WHERE tenant_id='01900000-0000-7000-8000-0000000000c1';
> ```
> 我没有擅自执行 —— 这是对数据库的破坏性操作，交给你确认。

以下为修复前的原始观察记录（证据仍然有效，仅规模数字按上文更正）：

### 修复前的观察

已核实的事实：

```sql
-- 该租户
SELECT status, route, (input_hash='') AS empty_input, count(*) ...
--  queued | knowledge_qa | t | 287        (各自对应 287 个不同会话)
```

- 速率约 **1–5 条/小时**，时间跨度 09-18 → 今日；与本地测试执行节奏吻合，**但也可能是真实缺陷**。
- 它们**没有 inbox 伙伴**。注意：`inbox_events.conversation_ref_id` 对 agent-run 事件
  **根本不写入**（`queue_agent_run` 调用 `persist_inbox_event` 时没传该参数），
  会话 id 只在 `minimized_payload.conversation_id` 里 —— **别用 `conversation_ref_id` JOIN**，
  我自己先连错过一次，得到"0 匹配"的假结论。
- 其中约 **253 条与「已执行 run」共用同一 `conversation_ref_id`**：即同一会话既有执行过的 run，
  又躺着一个 `queued` 占位。**可观测症状**：回放列表里该会话的 `latest_run` 显示 `queued`，
  尽管更早已有跑过的 run。回放页已把这个状态如实显示出来。
- 剩下 **23 个会话**只有这个占位、没有任何内容 → 无法回放。已从列表排除，但**计数返回**
  （`awaiting_agent`），因为静默丢弃会让人以为"这就是全部"。

**为什么没当场修**：这是 run 生命周期的问题（占位查找/复用），不是回放的问题；
在回放里绕过它只会把症状藏起来。建议单独开一轮，先确认"占位 run 按什么键被复用"。

---

## 五、给回放的三个设计约束（写进代码注释，防止被后来的改动推翻）

1. **关联用哈希，不用时间** —— 见第二节。
2. **不静默丢行**：未归因的 run 单独列出；只有占位的会话计数而非丢弃；
   已执行的 run 与 turns 取并集（实测该租户 turns_only 12 个、runs_only 458 个，
   任何单边取数都会隐藏真实会话）。
3. **回放不是绕过脱敏的通道**：服务端只返回 `text_redacted`，有测试专门断言响应里
   不出现被脱敏的电话号码原文。

---

## 六、剩余 3 项 🟡 → 现在 3 项

| 项 | 缺什么 |
|---|---|
| 2.1 身份归一扩展 | 渠道账号体系 + 真实标识样本 |
| 1.1 多渠道接入 | 各渠道 API 凭证 |
| 4C.6 图纸/复杂表格解析 | 真实样本 + 明确解析目标 |

清单口径：**79✅ / 3🟡 / 3❌**（P0 36/36 已闭合）
