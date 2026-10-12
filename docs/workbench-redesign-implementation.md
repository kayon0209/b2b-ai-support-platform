# 工作台实现与接口边界

工作台以会话为主对象，不要求会话先关联 Case。页面规格见 [工作台规格](workbench-redesign-spec.md)，部署条件见 [部署与运维](deployment-and-operations.md)，验证历史见 [2026-09-24 环境报告](workbench-redesign-acceptance.md) 与 [后续实现摘要](implementation/production-agent-roadmap.md)。

## 代码边界

| 部分 | 入口与职责 |
|---|---|
| 页面框架 | `Layout.tsx`、`styles-workbench.css`：窄导航、会话队列、对话与响应式辅助栏 |
| 坐席接待 | `pages/Workbench.tsx`：认领/回复/转接/退回/结束、搜索/分页、草稿、话术、历史和工具卡片 |
| 客户入口 | `Landing.tsx`、`SupportChat.tsx`：租户入口、访客消息、业务核实、人工消息、结束/新会话与反馈 |
| 会话 API | `agent_runtime/workbench_router.py`、`chat_service.py`、`identity/lease_service.py` |
| 副驾与任务 | `tasks_router.py`、`copilot.py`、`tasks/` 与专用异步消费者：建议/摘要、来源、任务状态与受控执行 |
| 回复与证据 | `agent_reply_router.py`、Tool Gateway、附件服务：权限、幂等、来源校验、业务回执和工单证据 |

前端路径统一为 `/admin/*`；旧 `/chat` 返回客户首页，旧 operator URL 由前端重定向兼容。API 镜像提供 SPA，但生产 API base/身份变量须按构建方案配置。

## 所有权与状态

- `queue` 等待人工；`human/HUMAN_ACTIVE` 当前坐席接待；`human/HUMAN_WAITING_CUSTOMER` 等待客户；`closed/RESOLVED` 会话结束。
- Case 的生命周期和 SLA 独立；结束聊天不自动结案。
- 认领、转交、退回与结束检查服务端 actor、租约版本和当前 owner；转交对象必须是本租户活跃坐席。
- 人工回复需幂等键并校验发送权。跨标签页草稿更新提示冲突，不静默覆盖。
- 客户等待 AI 通常每 2 秒读取，人工队列/接待每 5 秒；隐藏标签页暂停。轮询参数和容量按部署测量校准。

## 副驾与任务

建议回复和会话摘要由异步 job 生成。结果绑定会话、来源轮次、timeline revision 与 human lease；新消息/归属变化后结果可能 stale，不能继续插入或作为有效来源发送。

坐席可插入空草稿、追加或替换已有草稿；生成不自动发送。重新生成是新 job，不是刷新旧建议。任意自定义生成指令当前不开放；租户开关和获准模型配置控制可用性。

任务面板展示补参、依赖、受控工具执行、审批和 UNKNOWN 对账。订单只读与内部 Case 的能力以实际接入/回执为准；标准流程不等于自动操作真实 ERP、开票或维修。

## 当前不支持的动作

- 客户聊天中的文件发送：附件按钮只上传工单证据。
- 主管通用强制抢占、独立推送通知、个人排班管理。
- 任意外部写操作通用撤销；补偿仅按业务专用规则执行。
- 把会话结束自动视作工单解决，或把未回复视作问题解决。

身份、坐席目录、业务系统、对象存储与质量/性能条件集中维护在 [生产接入条件](production-readiness.md)，不在这里重复阶段性开发清单。
