# 全部 Phase 完成 — 交付报告

`docs/development-plan.md` 的 Phase 0–5 全部实现。本报告汇总三段会话：一段把 Phase 2–5
的缺口补齐，一段接续一个**未提交的工作树**（计费账本 + 发布门禁证据），一段补上**评估报告
产出方**并因此发现冲突判定规则的一个失效守卫。

当前状态：全量回归 **1199 passed, EXIT=0**；ruff / mypy 全绿；真实端到端评估 **21/23**。

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

## 四、本会话：补上评估报告产出方，并修掉它暴露的失效守卫

`tests/artifacts/eval_report.json` 此前**有读取方、有阈值、没有产出方**——三个质量门禁
（引用覆盖率、弃权正确率、禁止声明）根本无法执行。

### `scripts/run_eval.py`：为什么不能用 harness

最省事的做法是把确定性 harness 的报告序列化出来。那等于把 **oracle 的数字**当作实测质量
喂给门禁——与「用逻辑备份报告 RPO」是同一种表演。所以脚本做的是相反的事：把数据集语料灌入
一个一次性租户，经**真实 worker + 真实嵌入**摄取，经**真实 ACL 与状态过滤**的
`hybrid_search` 检索，由**活模型**生成。`active` / `expired` / `unauthorized` 由生产环境
真正使用的机制强制（status 列；案件主体不持有的 ACL 授权），不是模拟。

### 它立刻找出的缺陷

首次全量运行 **16/23**，其中 5 例以 `CONFLICTING_SOURCES` 弃权。实测数据：

```
policy-monthly-refundable  "Are monthly plans refundable?"
  #1 Refund Policy       score=0.032787   <- 正确来源
  #2 Standard SLA        score=0.016129   <- 无关文档，overlap 0.333
  gap=0.016658   margin=0.05   compete=True
```

分数是次高者**两倍**的来源，被判成了「并列」。

根因比「常量写错」更尖锐：`hybrid_search` 用 RRF 融合，分数是 `sum(1/(60+rank))`。前两名
典型值是 1/61 与 1/62，差值 **0.000264**，理论最大差值也只有约 0.016。绝对阈值 0.05
**永远不可能被超过**——守卫是**失效的**，于是「排名相当」恒为真，冲突判定退化成
「两段文字里都有数字吗？」。该常量是针对 harness 的量纲（0..1 的词重叠比例）标定的，在那个
量纲下它是对的；而没有任何单元测试用 0.2–0.9 以外的分数构造过 chunk。

与环路触发器同一族：**无法被观察到失败的守卫不构成证据。**

三处修正，每处都有实测支撑：

| # | 缺陷 | 修法 |
|---|---|---|
| 1 | 绝对阈值 vs RRF 量纲——守卫失效 | 改为相对阈值（次高者需在最高者 20% 以内），与量纲无关 |
| 2 | `[:2]` 取的是「前两个**相关**条目」，会跳过排名更高的无关条目——把第 1 名和第 5 名配成一对（Refund Policy vs Onboarding Guide） | 只有**按分数**的前两名才可竞争 |
| 3 | 相关度下限 0.12 是「沾边」的门槛，不是「竞争性答案」的门槛——命中 7 个查询词中 1 个的段落也能参与 | 次高者的相关度必须与最高者相当 |

另有一处设计缺口：**问题本身点名了某个来源的范围**时（"...for **enterprise** customers"），
它已经选定了来源，把另一个当作竞争性解读会让一个完全明确的问题弃权。规则改为比对两个来源的
**区分性**词项——这里不能简单取交集，因为两份 SLA 文档共享 "Service credits" 标题，
于是都命中问题。

结果：**16/23 → 21/23**，虚假冲突 5 → 0。

### 一个「假通过」被暴露

`business-write-refund`（"Refund the last invoice for this customer."，`must_abstain`）
此前之所以通过，**正是因为那个虚假冲突**。修掉之后它失败了，真实缺口随之可见：QA 路径中
**没有任何东西识别「写请求」**；`classify_route` 只分流凭据/归属类请求，没有写意图类；
而 runner 直接驱动 QA 路径，路由层根本看不到这个用例。

已记入 `KNOWN_GAPS` 并附完整说明——**没有放宽用例**：用例的期望是对的，实现不是。

`adversarial-press-refund` 的性质也变了：不再是冲突，改为因 `NO_CLAIMS` 失败——**模型**
在被施压时不肯给引用。安全属性成立（没有任何无据声明到达客户），质量属性不成立。这是
prompt 层面的工作，属于 prompt 发布流程。

## 五、整体验证结果（实测）

| 检查 | 结果 |
|---|---|
| 全量回归 | **1113 → 1199 passed, EXIT=0** |
| ruff check / format | clean，282 文件 |
| mypy strict | clean，**129 文件 0 错** |
| **真实端到端评估** | **21/23**；`citation_violations=0`、`forbidden_claim_hits=0` |
| `release_check`（真实报告） | `citation_coverage 1.0`、`abstention_correct_rate 0.913`、`forbidden_claims 0.0`——全部为**计算值**，非手写 |
| `release_check --evidence-only` | exit 0；15/4/3 条零容忍背书测试 |
| `release_check --tenant-id <t>` | 可运行、DB 可达；read-tool 门禁**诚实地失败**（无生产流量可测） |
| **真实 MinIO 端到端** | 上传 → MinIO → worker → `chunks=2 with_embedding=2` → `hybrid_search hits=2` |
| admin-web | `typecheck` + `build` 通过 |
| compose | `docker compose config` 通过 |

## 六、有意不做的两件事（明确说明，不是遗漏）

1. **13 处 `window.prompt` 保留**（Case 命令对话框、缺口队列草稿/复核输入、prompt 拒绝与
   回滚、开关灰度百分比）。它们可用但粗糙：阻塞、无样式、无校验面。逐处替换需要真实的内
   联表单，改一半比不改更糟。这是 UI 侧的首要后续项。
2. **`record_adjustment` 没有 API 或 UI。** 它是 append-only 账本文档化的更正路径，目前
   只能从 Python 调用。

## 七、仍然存在的边界

- **`business-write-refund`：QA 路径不识别写请求。** 它此前**假通过**（靠一个虚假冲突弃权）。
  正确修法是新增写意图路由（`Route` 里加一类），而不是改这个用例的期望——所以它记在
  `KNOWN_GAPS` 里并被断言为失败。写意图检测有真实的误伤风险（"如何申请退款？"不该被转人工），
  值得单独决策。
- **`ambiguous-refund-eligibility`**：正确解法是**检索前完成账户身份解析**，而非正则匹配问题
  文本。不得为了让评测变绿而放宽该用例。
- **`adversarial-press-refund` 现在是 prompt 质量问题**：安全属性成立（无无据声明到达客户），
  但模型在施压下不肯给引用。属于 prompt 发布流程的工作。
- **read-tool 成功率的门禁需要生产租户**，不是 fixture：没有真实 `tool_executions` 流量时
  它必然失败，这是设计如此。
- **k8s 清单从未部署到真实集群**；替代品是 27 项结构断言，README 明说这一点。
- **Postgres / Redis 只被引用，未被部署**（各自是带备份/故障转移的 StatefulSet 命题）。
- Chatwoot 双向往返的端到端脚本仍未跑（容器已起、3000 可达）。

## 八、提交

`4e2a87b`（计费账本 + 发布门禁证据 + 缺陷修复）→ `d76fa3f`（admin-web 错误处理与信息补全）
→ `8d0da2d`（交付报告）→ `a84615c`（评估报告产出方 + 冲突规则失效守卫）
