# Round 4 — 安全审计（对手视角）

日期：2026-09-19。方法：真实 HTTP 请求 + 代码核对；每条附请求/响应或 file:line。

## 验证通过项

| # | 项目 | 证据 |
|---|---|---|
| 1 | 未认证访问矩阵：14 个受保护端点全部 401 `AUTH_UNRESOLVED`（cases/usage/billing/members/audit/flags/prompts/knowledge/quality/connectors/dead-letters/branding/compliance/tool-proposals） | curl 实测记录 |
| 2 | 公开端 fail-closed：`/v1/public/branding` 无匹配 Host 时 404；`/healthz`/`/metrics` 按设计豁免 | 实测 |
| 3 | 角色闸门：support_agent 令牌打 owner/admin 专属端点，合法形状请求一律 403 + 稳定码（`FLAG_ACCESS_DENIED`/`ROLE_LACKS_ACTION`+action 明细；invite/quota/billing 调整/compliance/promote 全部 403） | 实测 |
| 4 | 跨租户隔离（tenant B 实体令牌 vs tenant A 数据）：case 详情 404 `CASE_NOT_FOUND`；case 列表空；members 只有自己；文档版本 404 "no such document version for this tenant"；检索 0 命中 A 的语料；usage 只见自己的计数 | 实测（B 租户经 SQL 种子 + bootstrap 令牌） |
| 5 | 注入防御：retrieval query / case subject / alias term 喂 `' OR '1'='1; DROP TABLE...` —— 全部按字面量处理，表数据完好（参数化查询，无一处字符串拼接 SQL） | 实测 + data intact 查询 |
| 6 | Webhook：正确签名 202；错误签名 401 `WEBHOOK_SIGNATURE_INVALID`；过期时间戳（+4000s>300s 窗口）401 | 实测 |
| 7 | 密钥：.env 已 gitignore（git check-ignore 通过）；tracked 文件 grep 无硬编码密钥（**工作树扫描，未扫全历史**——如实标注） | git grep |
| 8 | /metrics 无租户标识（grep 租户 UUID/slug 零命中；命中仅为 URL 路由字样），受 APP_METRICS_ENABLED 门控 | 实测 |
| 9 | 无 cookie 会话；React 全站转义（Round 2 XSS 存储实验 0 注入点） | 代码 + 实测 |
| 10 | 依赖：pip-audit `No known vulnerabilities found`；npm audit 2 moderate（react-router-dom 传递依赖，非可触发路径） | 工具输出 |

## 缺陷

**缺陷 S1（P1）别名 upsert API 对任何请求 500（出生即死）**
- 复现：`POST /v1/knowledge/aliases`，任意合法 JSON（如 `{"term":"a","alias":"b","weight":50}`）→ 裸 500。
- 根因：ORM 声明 `UniqueConstraint("tenant_id","alias", name="uq_aliases_tenant_alias")`（knowledge/models.py:207），而迁移 0033 创建的是 **UNIQUE INDEX** 同名（0033_retrieval_multipath.py:77）。SQLAlchemy 生成 `ON CONFLICT ON CONSTRAINT uq_aliases_tenant_alias` → `psycopg.errors.UndefinedObject: constraint ... does not exist`（API 日志含完整堆栈）。
- 影响：Phase 1.3 第四召回路的**管理面完全不可用**（别名无法经 API 增删改；检索别名路将永远空转）。SQLi 探针只是碰巧暴露了它——注入防御本身没被突破。
- 修复方向（Round 6）：改 `ON CONFLICT (tenant_id, alias)` 索引推断，或迁移把唯一索引转为约束；加回归测试。

**缺陷 S2（P1，与 Round 2 缺陷 2 同族）超长/畸形输入打崩 handler**
- `assign` 5000 字符 assignee_ref → 500 裸文本（Round 2 已记）；本 round 补充确认：错误响应无信封无 trace_id，违反 api-contracts.md 错误契约。

**缺陷 S3（P2）422 先于 403**
- 低权限用户发送缺字段请求时，FastAPI body 校验（422 + `{"detail":[{type,loc,msg}]}` 字段结构）先于策略检查执行，向已认证调用方泄漏请求 schema 细节。非绕过（仍被拒），但更好的顺序是先鉴权后校验，或统一 422 为错误信封。
- 复现：support_agent `POST /v1/identity/members/invite` 空 body → 422 detail 而非 403。

**观察（非缺陷）**
- bootstrap 令牌为无签名 `pt_<slug>_<uuid>`（代码注释与 config.py:221 明确 dev-only，local/test 之外拒绝启动——已核）。
- CORS 中间件未配置：跨源浏览器调用被浏览器策略拦截（同源/代理部署模型下的 fail-closed，部署约束应写进部署文档）。
- 令牌存 localStorage（XSS 面换取简单性；当前 React 转义下可接受，若引入富文本渲染需重估）。

## 复现命令
```
for ep in ...; do curl -s -o /dev/null -w "%{http_code} $ep\n" http://127.0.0.1:8000$ep; done   # 401 矩阵
curl -X POST .../v1/knowledge/aliases -d '{"term":"a","alias":"b","weight":50}'                 # S1 500
curl -X POST .../v1/webhooks/chatwoot -H "X-Signature: deadbeef" ...                            # 401
.venv/Scripts/python.exe -m pip_audit -r requirements.txt ; npm audit --omit=dev
```
