# 工作台改版实施记录与发布条件

日期：2026-09-24
参考图：[workbench-concept.png](blueprints/workbench-concept.png)
需求与验收：[规格](workbench-redesign-spec.md) · [验收表](workbench-redesign-acceptance.md)

## 已落地的代码边界

| 部分 | 文件与职责 |
|---|---|
| 页面框架 | `apps/admin-web/src/components/Layout.tsx`：窄导航轨、全部页面菜单、租户客服窗入口；`styles-workbench.css`：三栏与响应式布局 |
| 坐席接待 | `apps/admin-web/src/pages/Workbench.tsx`：会话队列、认领、人工回复、转接、结束、建议、引用、卡片、附件与历史翻页 |
| 客户资料 | `apps/admin-web/src/pages/Customers.tsx`：账户与联系人查看，管理员受控修改基本资料 |
| 客户窗口 | `apps/admin-web/src/pages/SupportChat.tsx`：人工回复期间持续获取、显式结束、结束后评价及新会话；`Landing.tsx`：无租户链接要求选择企业 |
| 收件箱 API | `agent_runtime/workbench_router.py`：由租约驱动的队列/详情/历史/动作；`identity/lease_service.py`：版本检查与所有权状态 |
| 数据投影 | `agent_runtime/chat_service.py`：最新时间线、搜索与预览；`cases/service.py`：会话关联工单；`support_bridge/continuity.py`：渠道和联系人 |
| 安全与质量 | `agent_reply_router.py`：回复幂等；`assignment_router.py`：认领 actor 绑定；`support_router.py`：评价资格；`config.py`：生产拒绝演示业务数据 |

旧 `/chat` 内部验证窗路由退回客户首页，旧 `CustomerChat` 与 `DataCard` 删除。ADR 0001/0010/0011 不删除，保留历史决策线索；现行方向为 ADR 0012/0015。

## 关键状态与接口约定

- `queue`：等待人工认领；`human/HUMAN_ACTIVE`：当前坐席拥有，客户最近已发消息；`human/HUMAN_WAITING_CUSTOMER`：坐席已回复，等待客户；`closed/RESOLVED`：会话结束。Case `resolved` 是另一个状态，结束聊天不会自动关闭工单。
- 队列项中的 `case` 可为 `null`；没有工单也必须可接待。账户、联系人、渠道可为空；不得伪造身份。
- 所有工作台写动作均需 `Idempotency-Key` 和会话租约版本。`claim` 的 actor 来自认证上下文；转交只能选本租户活跃坐席；碰到版本冲突返回 409。
- 人工回复接口用 `(tenant, conversation_ref, Idempotency-Key)` 派生唯一 turn ID，并在事务内用 advisory lock 串行化同键重试。
- 客户轮询：等待 AI 为 2 秒，人工队列／接待为 5 秒；浏览器标签不可见时暂停。正式并发容量必须测量后决定是否改为增量游标或流式更新。

## 上线前必须具备

1. **认证部署**：前端已接入 `oidc-client-ts` 授权码与 PKCE；生产必须配置 `VITE_OIDC_ISSUER`、`VITE_OIDC_CLIENT_ID`、IdP 允许的 `/auth/callback` 回调地址与静态站点安全头。本地开发仍可粘贴令牌，生产入口不接受此路径。临时 Keycloak 的 RS256/JWKS → API → membership 路径已验收；完整浏览器 PKCE 回跳、注销和真实企业 IdP 尚未验收。
2. **业务与渠道配置**：真实 ERP 连接和报价规则、出站邮件／微信凭据、至少一个已验证客户渠道。生产启动已拒绝 demo ERP 与公开参考价；部署配置需提供合法替代值。
3. **坐席目录**：同租户用户 ID 与 `AgentProfile.user_ref` 一致，在线坐席已登记，否则认领将返回 409。
4. **数据库与存储**：staging/production 必须通过密钥管理器注入独立的 `APP_DATABASE_APP_URL`，不能复用迁移 owner 连接；该运行角色不得有 `BYPASSRLS`。本地 Compose 由 `infra/compose/postgres-init/01-platform-app-role.sql` 创建受限 `platform_app` 角色。PostgreSQL RLS/迁移、MinIO 或 S3、出站 worker、审计与指标服务须健康；不得用模拟 API 作为集成验收依据。
5. **静态发布**：前端路由 fallback、`/api` 反向代理与 CSP/安全头。Vite 生产构建不再生成 `.map`；部署仍需确认历史构建文件不可访问。当前仓库只有 API ingress，没有完整前端托管清单。

## 明确未实现的概念图动作

- “重新生成”AI 话术：目前是刷新已有建议。真正重新生成需要模型调用预算、来源校验、超时／失败状态、审计和运营开关。
- 客户可见文件发送：现有附件是工单证据，不能作为消息送达。实现前需定义各渠道附件契约、病毒扫描与权限校验。
- 主管强制抢占其他坐席、真正的推送通知、个人班次切换：需要独立授权、通知与排班规则。
- 工单自动结案：会话结束与 Case 生命周期分开，避免客服点击结束就虚报业务问题解决。

## 建议发布顺序

1. 本地 Docker 已用 PostgreSQL 完成人工主流程：访客 → 转人工队列 → 坐席认领 → 回复 → 客户自动收到 → 结束 → 评价；RLS/应用层跨租户负向测试、schema 权限和双坐席同时认领也通过。后续在测试环境复验授权矩阵和 AI/人工发送竞态，并完成真实 OIDC、渠道送达与审计证据。
2. 用真实邮箱／微信沙箱验证出站送达、重复投递和失败重试，再配置租户级灰度。
3. 队列精确计数已拆分并增加三个部分索引，迁移使用并发建索引。本地 1 万条合成队列记录、单 API 容器、100 并发、每端点 1000 次复测：队列 p95 753ms、详情 428ms，全部 HTTP 200；队列 p95 仍超过 500ms 门槛，性能门禁继续阻断合并。继续分析连接池等待、认证成员解析与请求链路；优化后须在多 API 副本及生产近似 PostgreSQL 上复测，并覆盖 1000 在线窗口、长时间线翻页与持续轮询负载。
4. 完成 OIDC 与静态站点发布门禁后，逐租户启用，记录首响、平均处理时长、转人工后失联率和 CSAT 应答率，按异常回滚。
