# 交叉复核（反向审计）

日期：2026-09-19。**口径声明**：协议要求由全新会话执行本复核；子代理通道因账户余额不足不可用（两次尝试均失败），故由验收会话自审。自审无法完全规避自我确认偏差——已用「只找漏报 + 每条附新证据」的纪律对冲，但这是本次验收的已知局限。

## 1. 覆盖缺口（B − A：系统有、报告未提）

| 面 | 状态 |
|---|---|
| OIDC/Keycloak 登录全链路 | **运行态未验证**（验收全程走 bootstrap 令牌；OIDC resolver 仅单测/集成测试覆盖）。Keycloak 容器在跑但 realm 未配置、未走通 |
| SAML ACS 流 / SCIM bearer | 仅代码+测试层盘点（Round 1），运行态未测 |
| k8s 清单（infra/kubernetes/） | 未审计（仅 compose 被检） |
| packages/contracts 内容 | 未审计（Round 1 只确认 testpaths 包含它） |
| docs/launch-checklist-and-runbook.md | 迭代新增文件，未对照门禁逐条核实其「每条阈值有对应门禁」声明 |
| SLA / retention worker 的运行态行为 | 从未实时观察（60s/3600s 循环未等到或未触发验证） |
| webhook 匿名桶限流 / 连接器桶 | 只实测了租户桶 |
| compliance export 的导出内容 | 只测了 403 拒绝（低权限），未测 owner 正向导出 |
| e2e_chatwoot_duplicate_delivery.py | 未复跑（重复投递验收依赖 Round 3 间接证据 + 历史测试） |
| 知识下载预签名链路 | 复核时已补测（见 §3），原报告确实漏了 |
| admin-web dist 携带 936KB sourcemap | 原报告漏了——生产部署若同发 .map 会暴露源码结构（本地 dev 无碍），P2 观察项 |

## 2. 报告中证据不足的声明（自纠）

| 声明 | 问题 |
|---|---|
| 迭代交付报告「1372 passed」 | **未独立复现**。本轮只跑了 804 项单元（-m "not integration"）；集成套件因 worker 竞态警告未跑。Round 1 中已声明「Round 3 复核」，实际未复核——降级为「未验证声明」 |
| Round 6 批次④ assign 修复的「反向验证」 | 用的是 Round 2/4 的历史 500 实录，未做当场 revert 重启复现（批次①②④均做了当场反向，此项不完整） |
| Round 1「全部 onClick 可追到终点」 | 代码侧结论成立，但运行态确认受输入管线限制（已声明），属于有条件成立 |

## 3. 独立复验（复核时新做，非引用原报告）

1. **审计事件跨租户隔离**：tenant B（owner）GET /v1/audit-events → 200 空集；A 侧同库存 3161 条事件 → B 零泄漏 ✅（RLS 对 audit_events 生效）。
2. **知识下载预签名链路**（原报告未测）：owner 对已摄取版本 POST download-url → 200 返回 presigned URL（SigV4，localhost:9000）；B 对同一 version → 404 ✅。观察：URL 内嵌 minioadmin 凭据（dev 默认，生产必须换）。
3. **批次③修复的活体确认 + 波及面**：B 的 members 响应现含 `membership_id` 字段；`_member_out` 唯一调用方是 list_members（grep 证实无其他消费方被破坏）；identity 单测 110 项过。

## 4. 「自我安慰」表述重判

| 原表述 | 重判 |
|---|---|
| Round 3 前端「✅」 | DCL 数字为 dev-server 上界且已如实标注——维持，但「LCP 未测」应视为缺口而非通过 |
| Round 5「管理台全英文属产品现状，不算泄漏」 | **收回**：这是回避 i18n 决策。改判：若目标用户含中文运维，全英文 UI 为 P2 缺陷；需要产品决策记录 |
| Round 6「assign 反向验证=历史实录对照」 | 不完整，见 §2 |

## 5. 复核期间发生的事故与处置（如实记录）

**事故**：复核探针发现 admin-demo 的成员列表 403、审计 403、flags 403。追查：数据库中 `admin-demo` 成员的角色已变为 `support_admin`（A 的权限画像与此完全吻合）。审计事件链（append-only）显示 `identity.member.role_changed` @07:15:02，actor=A 自己的令牌，resource=A 的 membership。
**归因**：未能定论。已排除：批次③浏览器测试（prompt 文本与审计 resource_id 均指向 round2-member）；API 级反向探针（其请求体为 tenant_owner，服务端 400 拒绝且不可达 support_admin）。候选解释：本会话某次未归因的已认证请求（时间窗内唯一活跃调用方是本验收会话自身的工具链）。
**处置**：僵尸进程清理（历史 taskkill 过滤器失效导致 1 个旧 API + 多个旧 worker 存活）；直接 SQL 恢复 `admin-demo` 为 `tenant_owner`；复验 members/audit/flags/aliases 全部 200。
**正面结论**：这次意外本身验证了审计链价值——变更被 append-only 审计完整捕获（actor/action/resource/时间），使事后追查与恢复成为可能。**附带发现（P2）**：role_changed 审计事件的 metadata 为 `{}`、before/after_hash 为空——审计记录了「谁改了谁」，但没有记录「改成了什么」，审计信息量不足，建议补 after 角色。

## 6. 复核结论

- 报告主体（Round 0–6）证据密度高、反向验证纪律执行到位（5 批中 3 批当场 revert 复现）。
- 主要缺口集中在**未运行态验证的身份面**（OIDC/SAML/SCIM）与**未复跑的集成/评测套件**（1372 声明未独立复现）。
- 复核新增 3 条 P2 观察项（sourcemap 分发、审计 metadata 缺 before/after、presigned URL dev 凭据）与 1 条更正（i18n 判定）。
- 测试数据恢复：admin-demo 已恢复 owner；验收产生的工单/文档/旗标/别名保留作活证据；两个 e2e 孤儿租户待脚本修复后清理。
