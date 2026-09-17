# b2b-ai-support-plan — 本轮开发报告

**提交**：`1a0dc86` — `fix: resolve auth server-side for real; make admin-web runnable`
**测试基线**：487 → **506 passed**（+19），`ruff check` / `ruff format --check` / `docker compose config` 全绿

---

## 一、起点：admin-web 根本无法启动

按「保持与现有代码风格、目录结构一致」继续开发时，第一个发现是 `apps/admin-web` 是个空壳：

| 问题 | 影响 |
| --- | --- |
| `index.html` 引用 `/src/main.tsx`，文件不存在 | 应用启动即白屏 |
| 完全没有 CSS 文件 | 即便能启动也无任何样式 |
| `Cases.tsx` 只存在于错误嵌套目录（未跟踪垃圾目录） | 页面缺失 |
| 所有文件未跟踪，CI 无前端 job | 无任何门禁 |

补齐 `main.tsx` + `styles.css`、迁移 `Cases.tsx`、清理垃圾目录、新增 `.gitignore` 与 CI job，并修掉 3 个真实 TS 缺陷：

- **TS6310**：`composite: true` 与 `noEmit: true` 冲突 → 改 `emitDeclarationOnly`
- **TS2688**：`vite.config.ts` 用 `process.env` 但缺 `@types/node`
- **TS2322（真实逻辑缺陷）**：`GapQueue.tsx` 的 `toneForStatus` 对最关键的 `open` 状态（无人认领的知识缺口）返回 `"bad"`，但返回类型联合里没有 `"bad"` —— 本想渲染成最响亮的告警色，实际被类型系统排除在外

## 二、真正的问题：鉴权从未读过数据库

前端一旦开始发请求，就暴露出后端最高严重级的缺陷：

`bootstrap_token_resolver` 用 `uuid5(NAMESPACE_URL, f"tenant:{slug}")` **合成** tenant_id，返回 `role=None`，注释指向一个**不存在**的 `resolve_membership` 函数。

**后果**：所有已认证端点对所有用户（含 `tenant_owner`）返回 403；错误 slug 的 token 表现得像「登录成功但什么都做不了」。

**为什么测试没抓到**：单元测试只断言 `actor_id` 回环，从不检查 `role`；集成测试统一用 `_RoleResolver` 伪造上下文，直接绕过解析。**是让 admin-web 真正发请求才把它逼出来的。**

## 三、连带挖出的 5 个真实缺陷

修复鉴权的过程像剥洋葱，每修一层就露出下一层：

1. **鉴权解析缺失**（上文）
2. **ORM 枚举存储契约错配** — `Tenant.status` / `Membership.role` 用裸 `Enum`，默认持久化成员名（`ACTIVE`），而迁移建的是普通字符串列存值（`active`）→ 任何 ORM 读取都抛 `LookupError`
3. **RLS 绑定顺序** — `memberships` 是 FORCE RLS，未绑定时策略谓词与 NULL 比较恒返回 0 行，与「没有 membership」不可区分
4. **RLS 引导难题（鸡生蛋）** — 解析 token 需要一行 membership，但读 membership 需要先知道 tenant_id，而 tenant_id 正是解析要发现的东西
5. **Windows ProactorEventLoop 部署级阻断** — psycopg async 无法在 Proactor 循环上运行；uvicorn 硬编码该循环且**在导入 app 之前**就建好，所以 `main.py` 里 import 时的 set_policy 完全无效

## 四、两个关键设计决策

### RLS 引导难题：用窄函数，而不是放宽策略

`memberships` 在无绑定时不可读，这是构造性的矛盾。诱人的「修法」是加策略例外：

```sql
USING (app.tenant_id IS NULL OR tenant_id = ...)   -- ❌ 拒绝
```

这会向**任何未绑定的连接**开放**全表跨租户读**。`test_cross_tenant_leak_surfaces.py` 正是为抓这个而存在的。

实际采用：迁移 `0015_membership_bootstrap` 增加 `resolve_active_membership(slug, user_id)` —— 只读 `SECURITY DEFINER` 函数，`search_path` 固定，只授 EXECUTE 不授 SELECT。它永远只能返回同时匹配该 slug 与该 user 的那一行。

**逐条攻击验证后才写成断言**（先证伪，再固化）：

| 攻击 | 结果 |
| --- | --- |
| 通配符 slug（`%`、`_`、`' OR '1'='1`） | 0 行 |
| LATERAL 组合成表扫描 | 0 行（基表仍受 RLS 保护） |
| 直接 SELECT `memberships` | 仍 0 行 |
| app 角色 DROP FUNCTION | 被拒绝 |
| `prosecdef` / `search_path` / owner | true / 已固定 / 非 app 角色 |

### 鉴权失败必须不可区分

未知 slug、挂起租户、无 membership、非活跃 membership —— 全部返回**同一条** `identity not found or inactive` 与同一个 401。区分它们会让登录接口变成租户/用户枚举探针。

## 五、一个真实的间歇性测试缺陷

全量套件里 `test_outbox_relay` 偶发失败，单跑永远通过。

根因：`claim_pending` **全局**扫描 outbox（这在生产里是正确设计：一个 relay 消化全部），所以其它套件（或一次被中断的运行）留在队列里的行，会被本套件的批次一起认领，`stats.claimed == 1` 随之失败。

**用 A/B 证实而非猜测**：注一行游离的 queued 行 → 旧代码失败（可看到 `case.created` 被同批认领）→ 新代码通过。修复方式是让该模块的清理也删掉 `status = 'queued'` 的行（`sent` 行是惰性的），使套件重新自洽，从而保住严格的批次断言。

这类失败最危险之处在于：它看起来像 relay 的代码 bug，且依赖执行顺序 ——「刚才跑还是绿的」不能证明任何事。

## 六、实测证据（真实运行，非推断）

`python -m platform_core.main` + 真实 token：

- 9 个 GET 端点全部 200 且返回真实数据（`/v1/quality/metrics`、`/v1/flags`、`/v1/cases` …）；`/v1/prompts` 要求 `?template_name=`，属正确行为
- POST `/v1/cases` 建单成功并算出 SLA 时钟；`priority: "normal"` 被正确拒绝（须为 `p0`–`p3`）
- 四种鉴权失败模式返回完全一致的 401
- Vite（5174）`/api` 代理 → API → Postgres 打通，返回刚创建的工单；10 个前端模块全部 200
- `tsc --noEmit` 通过，`vite build` 通过（43 模块）
- 全量套件连续 3 次 506 passed（且在存在污染行的条件下）

## 七、仍未处置

| 项 | 说明 |
| --- | --- |
| `presign_get` 已定义但全项目无调用 | 未来文件下载 URL 的泄密面 |
| `ambiguous-refund-eligibility` | 仍在 `KNOWN_GAPS` |
| CRM **写**适配器缺失 | 目前仅注册 `jira.create_issue` |
| `APP_REDIS_URL` 无代码读取 | 属刻意设计（耐久队列是 PG 表），但值得加注释免得读起来像疏漏 |
| `@app.on_event("shutdown")` | FastAPI 已弃用，建议迁 lifespan |
| `bootstrap_token_resolver` 成功路径无 HTTP 层测试 | 已直连 Postgres 覆盖，但未走 app |

## 八、环境事实（已写入项目记忆）

- **Windows 上必须用 `python -m platform_core.main` 启动 API**，不能用裸 `uvicorn`
- 改迁移数时须同步 `test_migration_and_performance.py::EXPECTED_MIGRATIONS`（刻意设置的闸门，逼出不可逆迁移）
- `scripts/seed_admin_demo.py` 可生成 `admin-demo` 租户与 token，供手动验证
