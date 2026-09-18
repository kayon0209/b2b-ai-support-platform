# 全部 Phase 完成 — 交付报告

本会话从「Phase 2 尚有两项未落地」推进到 **Phase 0–5 全部完成**，并执行了整体验证与端到端测试。全量回归 **1032 → 1118 passed, EXIT=0**。

## 一、按开发计划逐项交付

| Phase | 本轮补上的内容 | 依据 |
|---|---|---|
| **2** | `EnterpriseAccount` + `Department`（此前**完全不存在**）；`memberships.department_id`、`cases.enterprise_account_id` 的悬空列接上**组合外键**；契约层级驱动 SLA 时钟 | 迁移 0028 |
| **2** | **SLA 升级执行器**——`is_breached` 此前**零调用方**，违约的 Case 只会一直违约 | 迁移 0029 + worker `sla` 角色 |
| **3** | **Linear 适配器**（计划中「Jira **或** Linear」，此前只有 Jira）；IM 渠道拆分为 Slack/Teams/Feishu 三种真实载荷 | 提供方注册表 |
| **5** | **自定义域名**已在上轮完成；本轮补 **SAML 2.0 SP**、**SCIM 2.0 供应**、**合规导出**、**k8s 高可用模板** | 迁移 0030 + `infra/kubernetes/` |

## 二、本轮的元规律：缺陷都是「跑出来」的

| 缺陷 | 怎么发现的 |
|---|---|
| **两节点环路被放行** | 活库探测：`EXECUTE ... INTO` 之后 `FOUND` 是 **false**，我那句 `IF NOT FOUND` 把刚读到的父节点丢掉了，遍历在第一跳就终止。单节点自环仍被拦——但那是 CHECK 约束拦的，**触发器其实一直没起作用** |
| **SAML 状态码检查从未生效** | 单元测试断言「IdP 拒绝必须被拒」：状态码在 `Value` **属性**里，我按文本读，`"" and ...` 短路，**IdP 的显式拒绝被当成成功** |
| **`redact_value` 只脱敏文本，不认键名** | 写审计元数据时发现 `{"api_token": "..."}` 原样穿过去了——它是「恰好会遍历容器的文本脱敏器」 |
| **迁移建表却忘授权（第二次）** | 域名接口首个请求 `permission denied`；且**只在全新库**上失败 |
| **`tenants` 缺 DELETE** | `downgrade base && upgrade head` 之后才暴露：这条断言此前是**在漂移过的库上通过的**，迁移本身从未授予该权限 |
| **消费方读 `ambiguous` 会 KeyError** | 适配器早返回路径的字典形状与其它路径不一致 |
| **`PATCH` 被当成完整资源校验** | SCIM 集成测试：只带 `active: false` 的下线请求因缺 `userName` 被拒 |
| **元数据依导入顺序不完整** | worker 侧测试 `NoReferencedTableError`，而 API 侧测试通过——因为 API 导入全部路由 |

**结论**：绿测试是不够的。真实端到端、迁移降级/升级往返、以及「这条断言到底在断言什么」的复核，各自都抓到了单元测试看不见的东西。

## 三、整体验证结果（实测）

| 检查 | 结果 |
|---|---|
| 全量回归 | **1118 passed, EXIT=0** |
| ruff / format / mypy | clean（263 文件 / 124 文件 0 错） |
| `pip-audit --strict` | No known vulnerabilities found |
| 迁移链 | `downgrade base` → 1 表 → `upgrade head` → **40 表 @ 0030** |
| **真实 MinIO 端到端** | 上传 → MinIO → worker → 2 chunks（含嵌入）→ `hybrid_search hits=2` |
| **备份/恢复演练** | **62 项检查、0 失败**，40 表比对，RTO 1.5s |
| **真实 Keycloak OIDC** | 真实口令换取的令牌**通过校验**；错误签发者 / 错误受众 / 篡改令牌**全部被拒** |
| k8s 清单 | `kubectl kustomize` 构建 20 个对象；27 项结构断言 |
| compose | `docker compose config` 通过；新增 `ai-worker-sla` |
| Chatwoot | 容器已起，3000 端口可达（未跑双向往返的端到端脚本） |

## 四、有意不做的三件事（明确说明，不是遗漏）

1. **SAML 登录不签发会话。** API 鉴权是 Keycloak 的 OIDC bearer token；在这里另造一种凭据会多出一条更少被审视的取数路径。ACS 只做它该做的：证明身份、拒绝未获角色者、写审计。
2. **首次登录不授予角色。** IdP 属性由 IdP 管理员控制，据此发角色等于把租户权限交给对方。返回 `SAML_NO_MEMBERSHIP`（403），角色仍由租户自己授予。
3. **SCIM 的 Group 映射到 Department，绝不映射到 Role**；`filter` 只支持 `attribute eq "value"`，并在错误里写明支持范围。

## 五、仍然存在的边界

- **k8s 清单从未部署到真实集群**：`kubectl apply --dry-run=client` 需要 API discovery，无法离线做闸门；替代品是那 27 项结构断言。README 明说这一点。
- **Postgres/Redis 只被引用，未被部署**：各自是带备份/故障转移/升级问题的 StatefulSet 命题。
- Chatwoot 双向往返的端到端脚本本轮未跑。
- 知识缺口队列仍有 1 条 `ambiguous-refund-eligibility` 记录在案。

## 六、提交

`e4c0936`（Phase 2 组织架构）→ `3c293ed`（SLA 升级）→ `f38b14c`（Linear + IM）→ `3879d5c`（k8s）→ `2e1d606`（合规导出）→ `6774e38`（SAML + SCIM）→ 最终验证提交。
