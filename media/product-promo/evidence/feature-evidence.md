# 影片功能证据（基准 e07d2a1）

| 功能与观众收益 | 状态与来源 | 影片复用组件 | 画面数据及限制 |
|---|---|---|---|
| 客户可在自有 `/support` 页面提问，订单回执显示状态、来源与更新时间，便于核对 | 已合入；`apps/admin-web/src/pages/SupportChat.tsx`、`apps/admin-web/src/components/ToolCard.tsx` | `SupportChat`、`ToolCard` 原组件及 `styles-support.css` | 固定合成订单 `SO-DEMO-2026`，前端 fixture；不表示已查询真实 ERP，也不保证交期。 |
| 坐席可在同一会话接管并继续回复，人工所有权覆盖 AI | 已合入；`apps/admin-web/src/pages/Workbench.tsx`、`apps/api/src/platform_core/agent_runtime/workbench_router.py` | `Workbench` 原页面及 `styles-workbench.css` | `claim` 通过原按钮点击，fixture 返回 human lease；影片不执行真实服务端事务。 |
| 写入提案需人工确认；执行后展示独立的核验状态 | 已合入；`apps/admin-web/src/pages/Approvals.tsx`、`apps/admin-web/src/components/Prompt.tsx` | 原 `ProposalPanel` 和 `usePrompt`，仅在展示构建中追加 export | 提案是内部 `case.create` 演示，状态通过本地 fixture 驱动；不代表开票或真实 CRM 写入。 |

视频外层画面和标题是本片代码；功能窗口使用仓库组件与原样式。未展示的知识版本发布、R3 真实业务系统、移动端和 API 操作不构成影片主张。
