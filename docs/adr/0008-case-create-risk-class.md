# ADR 0008: `case.create` 是 `confirmed_write`，参数由人给定

日期：2026-09-19　状态：已接受

## 背景

阶段 3 的质量投诉半自动化要求"`case.create` + 证据附件 → 人工裁定"。在实现之前
必须先回答三个问题，因为它们各自都有一种"看起来也能跑通"的错答案：

1. **为什么是这个风险级？** `case.eq_confirm` 是 `human_approval`，而"创建工单"听起来
   同样是"对客户做了个承诺"，很容易顺手归到同一级；
2. **谁提议、谁批准？** 如果智能体也能提议，它是否就成了流程的实际驱动者；
3. **参数从哪来？** `create_case` 的签名里有一个 `enterprise_account_id`，
   它决定 SLA 层级与两个截止时间。

### 事实：`enterprise_account_id` 决定 SLA，不是标签

`cases/service.py::create_case` 在给定该参数时：

```python
facts = await account_sla_facts(...)
if facts is None:
    raise CaseError("ACCOUNT_NOT_FOUND")
tier, contract_status = facts
policy = sla_policy_for_tier(tier, contract_status=contract_status)
# sla_tier 在构造函数里被快照
first_response_due_at, resolution_due_at = sla_deadline(...)
```

并且注释明确写了为什么是快照而不是延迟解析：

> Snapshotted, not resolved later: the deadline is recomputed on a priority change,
> and re-reading the account then would let a mid-Case contract change move a clock
> that is already running.

也就是说：**账号选错 = SLA 时钟从第一秒起就是错的，且这个错误被快照进 `Case.sla_tier`
不再自我纠正**。而华秋场景里最容易出投诉的恰恰是 4 层级客户里的顶层（报告 §2.4 难点 5
"大客户分层"）——错在最贵的那一档上。

### 事实：没有与 `_find_locked` 对等的账号解析器

`case.eq_confirm` 的引用来自 `_find_locked`：`case_ref` 命中多个候选时返回 `None`，
**绝不挑一个**（"Ambiguity is not resolved by picking one"），于是"不明确"是一个可被
表达、可被测试的结局。`enterprise_account_id` 没有这种东西：账号名到 id 的映射在
本仓库里没有唯一性保证（同名不同公司、简称、`EnterpriseAccount` 无 slug 唯一索引）。
若允许智能体从话术里抽取账号并"推测一个"，就是把 SLA 时钟的输入交给字符串相似度。

## 决策

### 1. 风险级：`confirmed_write`

不是 `human_approval`。四条互相独立的证据：

| 证据 | 内容 |
|---|---|
| 报告的风险分级清单 | §2.4 难点 4 在 `HUMAN_APPROVAL` 下**只列了**"EQ 放行、赔付、退款"，创建工单不在其中 |
| 阶段 3 的措辞 | "`case.create` + 证据附件 → **人工裁定**"——人工的裁定落在**投诉的处理结论**上，不落在"开一张单"这个动作上 |
| 既有 RBAC | `support_agent` 本就持有 `CASE_CREATE`：创建工单是被授权为常规动作的，提级到 `human_approval` 会与这张表矛盾 |
| 与该等级的定义一致 | `engine.py` 注释：`TOOL_WRITE_CONFIRMED` 让 `integration_service` **propose**；`support_admin` "can authorize a confirmed write"。这正是"平台侧记账、人批准"的形态 |

**反面论证（为什么不是 `human_approval`）**：`case.eq_confirm` 之所以是 `human_approval`，
理由是 `packages/policy/engine.py` 写的"the case status **is** the signal the factory reads"
——记录确认离放行只有一步，状态列本身就是物理世界的指令。`case.create` **不移动任何
外部世界的东西**：它新建一行记录，SLA 时钟只约束我们自己。它是一笔内部账。

**顺带解决一处悬空**：`integration_service` 持有 `tool.write.confirmed`，但在 EQ 升到
`human_approval` 之后，全仓库**没有任何工具消费这个权限**——它是权限表里一个无人使用的
格子。`case.create` 是它的第一个真实消费者，这本身就是"这个等级不是为 EQ 而设"的证据。

### 2. 谁提议、谁批准

| 动作 | 角色 | 闸门 |
|---|---|---|
| 提议 | `support_agent` / `support_admin` / `integration_service` | `tool.write.confirmed` |
| 批准 | `support_admin` / `tenant_owner` | `Action.CASE_UPDATE`（`POST /v1/tool-proposals/{id}/confirm` 的闸门） |
| 执行 | 同提议方 | 执行前重查 `tool.write.confirmed` |
| **智能体自动提议** | **不做** | 见下 |

"agent 提议、人批准"这条链路**必须存在**（AGENTS.md 规则 7），`confirmed_write` 让
`integration_service` 具备提议能力，且它**仍然拿不到批准权**——唯一生产 `ActionConfirmation`
的路径要求 `CASE_UPDATE`，而该角色不持有。可达性不对称依然是控制点。

但本轮**不把 `case.create` 加进写选择器**（`_WRITE_SCENE_AFFINITY` / `_WRITE_SUBJECT_NOUNS`）。
理由与附录 D 排除 `crm.update_account` 的是同一条，且更硬：

- 按上面第 3 条，`enterprise_account_id` 必须由人给定，**智能体无法满足一个"必填且必须由人决定"的参数**；
- `subject` / `description` 是自由文本，按 `_extract_write_args` 的既有取舍（只做确定性抽取，
  不引入模型）没有可用的抽取方式；
- 列进选择器就是"每选中一次就必然失败一次"的候选：它在选择审计里是噪声，
  与"根本没发布这个工具"不可区分。

**对标 `case.eq_confirm` 的落地方式**：工具 + 控制台路径（人可见、可提议、可批准），
智能体侧**识别到"建工单"意图后转人工**。注意 `ACTION_VERBS` 现在已经包含 `create`，
所以"帮我建一张工单"会路由到 `BUSINESS_WRITE`，然后**必须干净地转人工**
（`TOOL_NO_CANDIDATE`），而不是走进一条半成品分支。

### 3. 参数从哪来：A 方案，账号必填且由人给定

`enterprise_account_id` **必填**，由人类在提议时提供（控制台表单字段 / API 请求体）。
智能体不得提议缺少它的调用，**也不得静默降级为 `None`**。

沉默降级被否决，因为它的失败模式是隐形的：`enterprise_account_id=None` 时
`create_case` **完全正常成功**，只是没有 SLA 快照——于是工单建出来了、面板是绿的、
而这张单**永远不会因为首次响应超时而升级**。没有报错的地方就是最贵的地方。

配套的守卫（三处，缺一不可）：

1. **schema 层**：`TOOL_CATALOG` 的 `input_schema` 把 `enterprise_account_id` 放入
   `required`。`validate_against_schema` 在 `propose` 阶段就会拒（`TOOL_ARGS_INVALID`），
   使"忘了这个参数"在**创建提议之前**就失败，而不是执行到一半；
2. **执行器层**：显式检查，缺失/空白 → `ACCOUNT_NOT_FOUND`，并且**明确区分于**
   `create_case` 自己抛的 `ACCOUNT_NOT_FOUND`（那个是"账号不存在或不属于本租户"）；
3. **测试层**：一条断言"缺少账号 → 拒绝"的反向用例，外加一条断言
   "确实没有产出 `enterprise_account_id IS NULL` 的工单"。

`subject` / `description` / `priority` / `category` 由人给定；`conversation_ref_id`
用于把投诉对话挂到工单上（`relationship="origin"`），使附录 F 修好的 `CaseConversation`
写入路径在投诉场景下真正被用上。

## 后果

- `case.create` 的提议**不来自智能体**，来自坐席/管理员；这是本项目里第一个
  "工具存在但智能体侧只转人工"的写工具，与 `case.eq_confirm` 同类，但原因不同
  （EQ 是"必须够不到"，这个是"参数必须由人决定"）。
- 新增工具**不需要迁移**：`ensure_tool_definitions` 只增不改，且 `case.create` 此前
  不存在行。这条要与迁移 `0038` 对照记录，避免下次把"改风险级"和"新增工具"混为一谈。
- 控制台需要能选择 `enterprise_account_id`。管理台「新建工单」页已存在，
  若它当前以 `enterprise_account_id` 为可选项，需要一并收紧——**UI 允许的空值
  就是 API 允许的空值**。
- 证据附件（MinIO 预签名 URL）见"未决"。

## 未决

- **证据附件是否并入本轮。** 阶段 3 的 marker 写的是"`case.create` + 证据附件"。
  存储路径已存在（`knowledge/storage.py` 的 `presign_get`、`knowledge/service.py::presign_for`、
  `config.presign_expiry_seconds = 300`），但有一个必须先解决的碰撞：
  `gateway.sanitize_arguments` 会对**输入和输出双向**脱敏，而 `SENSITIVE_FIELD_NAMES`
  含 `email` / `phone`，且脱敏**递归进嵌套 dict**。附件元数据里如果带客户联系方式字段，
  会被替换成 `***`，于是回执里引用不到真正上传的东西。
  这一条需要单独决定（换键名 / 在网关白名单 / 附件元数据不含联系方式），
  不在本 ADR 范围内。
- BOM 配单进度、NextPCB 英文语料、大客户分层仍属阶段 3 未开始部分。
