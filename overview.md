# 全部 Phase 完成 — 交付报告

`docs/development-plan.md` 的 Phase 0–5 全部实现。本报告汇总两段会话：一段把 Phase 2–5
的缺口补齐，一段接续一个**未提交的工作树**（计费账本 + 发布门禁证据），并在验证过程中
修掉它掩盖的缺陷。

当前状态：全量回归 **1192 passed, EXIT=0**；ruff / format / mypy 全绿。

## 一、按开发计划逐项交付

| Phase | 内容 | 依据 |
|---|---|---|
| **0** | ADR 4/4、CI 8 个 job、依赖与密钥扫描、评估数据集（含 `must_abstain`） | `docs/adr/`、`.github/workflows/ci.yml` |
| **1** | 签名 webhook + 去重、摄取流水线、预过滤检索 + 引用校验、弃权与交接、发送前租约复检、可观测性 | 迁移 0017–0019 |
| **2** | Tenant/Membership/Department/EnterpriseAccount、OIDC、RBAC+ABAC、全表 FORCE RLS、知识 ACL、Case 生命周期与 SLA、append-only 审计 | 迁移 0001/0008/0015/0028/0029 |
| **3** | 连接器 SDK、Jira/Linear/CRM/IM、凭证解析与轮换、健康检查与重新授权、死信、可续传同步、通用提供方 webhook | 迁移 0026 |
| **4** | 评估 runner 与发布门禁、质量看板、知识缺口队列、prompt 版本发布与回滚、PII 脱敏、保留期清扫、入站限流、备份/恢复演练、特性开关消费方 | `rate_limit.py`、`scripts/backup_restore_drill.py` |
| **5** | 租户品牌与自定义域名、成员自助、用量配额、**计费账本**、SAML/SCIM、合规导出、k8s 模板 | 迁移 0022/0023/0027/0030/**0031** |

## 二、本会话：接续未提交的工作树

未提交的内容是**计费账本**与**发布门禁证据**两件事，质量本身是好的：FORCE RLS + 策略、
只授 `SELECT, INSERT`（append-only，且在 `test_schema_privileges.py` 里带理由断言）、
以事件 id 做幂等（`uq_billing_entry_event`）、relay 逐行绑定 RLS。

### 补上的两个「有产出方、无消费方」

- **计费**：`build_default_relay` 只注册了两个 case 事件，于是 `usage.recorded` 全部落到
  log-only 默认处理器。orchestrator 一直在发事件，**平台有发射器却没有聚合器** ——
  「这个租户 3 月消耗了多少」无法回答。
- **发布门禁**：三个零容忍计数此前是**手写字面量**。它不构成断言，也无法在「本该发现泄漏
  的测试套件停止运行」时变红。现在改为**派生**：测试声明
  `@pytest.mark.zero_tolerance(...)`，`gate_evidence` 插件在会话结束时写
  `tests/artifacts/release_gate_evidence.json`，`evidence.py` 是唯一读取方；少于 500 条
  的局部运行会被**拒绝读取**。

relay 还需两处修正才能工作：`run_once` 从不提交（调用方传裸 session 会看到 `sent=1`
而什么都没落库），以及每行必须在**自己租户的 RLS 绑定下**派发（claim 发生在租户已知之前）。

## 三、本会话修掉的缺陷

| 缺陷 | 怎么发现的 | 修法 |
|---|---|---|
| `release_check` 在 Windows 上**永远读不到** read-tool 遥测 | 跑这个 CLI：裸 `asyncio.run` 选到 ProactorEventLoop，psycopg 拒绝，于是门禁报「无遥测」——而原因与遥测无关 | `_run_async` 显式选 `SelectorEventLoop`，并加回归测试断言 `isinstance(loop, SelectorEventLoop)` |
| `release_check` + `evidence.py` **零测试** | grep 覆盖率 | 新增 12 个测试，覆盖每一条拒绝路径（含「手写零无法表达」） |
| `drain_outbox_once` / `_now` **零调用方** | 本仓库惯用的「grep 调用方」 | 删除 |
| docstring 指向不存在的类 `OutboxRelayRunner` | 核对提交契约时 | 改为 `OutboxWorker` |
| `GET /v1/tenant/billing` 无 HTTP 测试 | 检查新 UI 依赖什么 | 新增 4 个：信封键、`support_admin` 403、`auditor` 200、落账后进入汇总 |
| 4 个文件 `ruff format` 不过 | 门禁 | 重排 |
| CI 只 lint `apps packages`、**没有 mypy job**、从不运行自称「CI 入口」的 `release_check` | 读 `ci.yml` | 扩到 `scripts tests pytest_plugins_release`；新增 typecheck job；新增 `release-evidence` job |
| **`ApiError` 不是 `Error`** —— 全站错误提示渲染成 `[object Object]` | 读前端错误处理 | 见下 |

### 两个系统性 UI 缺陷

**`ApiError` 曾是 interface，`toApiError` 返回普通对象。** 所有调用点都写
`err instanceof Error ? err.message : String(err)`，于是落到 `String(err)`，**全站错误横幅
和 `alert()` 都显示字面量 `[object Object]`** —— 服务器给的消息（运维唯一能据以行动的东西）
在最后一步被丢掉。改成真正的 `Error` 子类，一次性修好所有调用点。

**四个页面用 `alert()` 反馈写操作。** 它阻塞整个标签页、无法样式化、也不作为 live region
被朗读；运维连续执行 Case 命令时每一步都要关一个弹窗。改为 `useAction()` +
`<ActionFeedback>`（真正的 `role="alert"` / `role="status"` 横幅）。顺带两个行为修正：
开关表单**只在成功时**清空输入；`unwrap` 不再让 `JSON.parse` 在 HTML 错误页上抛异常
（那会吞掉状态码）。

两者对测试套件完全不可见 —— 它们是前端行为，而前端门禁只有 `tsc` + `vite build`。

同时把 API **已经返回、却没有任何界面展示**的两组数据接上：Usage 页的计费账本
（403 渲染为权限说明而非失败横幅），以及质量看板的 supported / wrong resolution
（`docs/development-plan.md` 明确点名的 Phase 4 看板指标）。

## 四、整体验证结果（实测）

| 检查 | 结果 |
|---|---|
| 全量回归 | **1113 → 1192 passed, EXIT=0** |
| ruff check / format | clean，281 文件 |
| mypy strict | clean，**129 文件 0 错** |
| `release_check --evidence-only` | exit 0；1192 条测试，15/4/3 条背书测试 |
| `release_check --tenant-id <t>` | 可运行、DB 可达；read-tool 门禁**诚实地失败**（窗口内无流量） |
| **真实 MinIO 端到端** | 上传 → MinIO → worker → `chunks=2 with_embedding=2` → `hybrid_search hits=2` |
| admin-web | `typecheck` + `build` 通过 |
| compose | `docker compose config` 通过 |

## 五、有意不做的三件事（明确说明，不是遗漏）

1. **`tests/artifacts/eval_report.json` 没有产出方。** 它必须来自**真实租户语料 + 活模型**。
   用确定性 harness 生成它，等于把 oracle 的数字喂给门禁 —— 与「用逻辑备份报告 RPO」是同
   一种表演。`release_check` 的报错信息现在如实说明这一点，而不是此前误导性的
   「先跑评估数据集」。
2. **13 处 `window.prompt` 保留**（Case 命令对话框、缺口队列草稿/复核输入、prompt 拒绝与
   回滚、开关灰度百分比）。它们可用但粗糙：阻塞、无样式、无校验面。逐处替换需要真实的内
   联表单，改一半比不改更糟。这是 UI 侧的首要后续项。
3. **`record_adjustment` 没有 API 或 UI。** 它是 append-only 账本文档化的更正路径，目前
   只能从 Python 调用。

## 六、仍然存在的边界

- **k8s 清单从未部署到真实集群**；替代品是 27 项结构断言，README 明说这一点。
- **Postgres / Redis 只被引用，未被部署**（各自是带备份/故障转移的 StatefulSet 命题）。
- Chatwoot 双向往返的端到端脚本仍未跑（容器已起、3000 可达）。
- 知识缺口队列仍有 1 条 `ambiguous-refund-eligibility` 记录在案；正确解法是**检索前完成
  账户身份解析**，而非正则匹配问题文本。不得为了让评测变绿而放宽该用例。

## 七、提交

`4e2a87b`（计费账本 + 发布门禁证据 + 缺陷修复）→ `d76fa3f`（admin-web 错误处理与信息补全）
