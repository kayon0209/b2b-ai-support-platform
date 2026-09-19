# Round 5 — UX 与文案审计

日期：2026-09-19。对象：admin-web（源码 + dist 构建 + 后端错误信封在 UI 的呈现）。口径：这是面向企业运维的管理台，领域词汇（Agent、Prompt、Flag、Token、配额、工单）视为产品语言；审计目标是**开发者视角泄漏**与**状态/反馈缺失**。

## 5-A 技术术语泄漏

| # | 术语/原文 | 位置 | 呈现场景 | 定级 | 建议文案 |
|---|---|---|---|---|---|
| L1 | `[object Object]` | GapQueue.tsx:99-106（`int(v)` 喂入嵌套对象；后端 `/v1/knowledge/gaps/stats` 返回 `by_status:{}`） | 质量统计卡片值 | **P1（协议红线）** | 修渲染（只渲染数值字段），卡片值应为数字 |
| L2 | `unknown case status: open`（后端异常原文直出） | Cases transition（前端 STATUSES 与后端枚举脱节，Round 2 缺陷 1） | 错误横幅 | P1（随缺陷 1 修复消除） | 修复词表后用户不可达；后端改稳定码 |
| L3 | `membership not found`（英文裸消息） | Members 角色变更/移除（Round 2 缺陷 4） | 错误横幅 | P2（随缺陷 4 修复消除） | 同上 |
| L4 | `every write command must carry an Idempotency-Key header`（HTTP 头概念直出） | Flags/Prompts/Gaps 全部写操作（Round 2 缺陷 6） | 错误横幅 | P2（随缺陷 6 修复消除；该文案对 API 调用方仍是正确的契约指引） | 前端补键后运维不可达 |
| L5 | `HTTP 401` | lib/api.ts unwrap 的默认消息；401 时页面横幅 + TokenDialog 同时出现 | 错误横幅 | P2 | 401 时静默转入令牌弹窗，不再渲染冗余横幅；横幅文案改「登录已过期，请重新输入访问令牌」 |
| L6 | `Latency P50 / P95`（QualityDashboard.tsx:100-103） | 质量看板指标卡 | 看板 | 可接受（运维看板的领域指标；如需统一可改「响应时长 P50/P95」） | — |
| L7 | `Internal Server Error` 裸文本（无信封） | 超长 assignee_ref（Round 2 缺陷 2）、别名 upsert（Round 4 S1） | 错误横幅 | P1（随两个 500 修复消除） | 修根因 + 兜底信封 |
| L8 | `invite created`/`Token copied to clipboard.` 等英文 UI 文案 | 全站 | — | 观察 | 管理台全英文属产品现状，不算泄漏；如面向中文运维需 i18n 决策 |

dist 构建产物与源码字符串一致（minify 不改变字面量），无额外泄漏。

## 5-B 状态完整性与反馈闭环

**三态检查（逐页，Round 2 实测证据）**

| 页面 | 空态 | 加载态 | 错误态 | 结论 |
|---|---|---|---|---|
| Quality | ✅ 零运行有解释文案；⚠️ "Citation coverage 100.0%"（0/0 显示 100%，误导） | ✅ Spinner | ✅ ErrorBanner+Retry | 缺陷 U1（P2）：0/0 应显示「—」 |
| Cases | ⚠️ "No cases yet." 无下一步引导（且 UI 无创建入口，Round 2 观察） | ✅ | ✅ | U2（P2） |
| Gaps | ✅ 分状态空态 | ✅ | ✅ | 过 |
| Prompts | ✅ "No versions for this template yet." | ✅ | ✅ | 过 |
| Flags | ✅ "No feature flags defined."（定义表单就在旁边） | ✅ | ✅ | 过 |
| Members | 不适用（owner 恒存在） | ✅ | ✅ | 过 |
| Usage | ✅ 零值面板+语义说明 | ✅ | ✅（billing 403 → 权限说明而非报错，Round 1/2 证据） | 过 |
| Branding | 不适用（表单） | ✅ | ✅ | 过 |

**反馈闭环**：所有写操作均有成功（role=status 绿）/失败（role=alert 红）反馈；唯一「无反馈」场景是 Round 2 之前的历史（`window.prompt` 已全部移除，overview.md 与实测一致）。禁用按钮（owner 行 Remove、空邮箱 Invite）无解释 tooltip——P2 可达性小项。

**一致性**：同一动作语义一致（Confirm/Cancel 弹层统一 usePrompt；反馈统一 ActionFeedback）；命名一致（Pages 与导航一致；"Send invite" vs 导航 "Members" 无冲突）。按钮位置统一（详情页命令栏、页头 actions）。

**可达性**：全站交互元素为原生 button/a/input/select（键盘可达，静态判定）；alert/status live region 齐全；375px 视口无横向滚动但侧栏不折叠（P2）；本会话键盘走查受自动化输入管线限制未完成（如实标注）。

**破坏性操作**：Remove/改角色/Promote/Rollback 均有确认弹层 ✅；取消即取消 ✅（历史缺陷已修：旧 `window.prompt(...) ?? ""` 会在取消时仍批准）；Branding PUT 整体替换语义在契约中文档化但 UI 无确认（P2 观察，表单总是发送全字段，实际风险低）；Flag 启停一键无确认（可逆+审计，可接受）。

## 汇总

| 级别 | 项 |
|---|---|
| P1 | L1 [object Object]（=Round 2 缺陷 7）；L2/L7 随对应根因修复 |
| P2 | U1 0/0→100% 误导；U2 空态引导；L5 401 冗余横幅；禁用无解释；侧栏不折叠；S3 422 先于 403（Round 4） |
