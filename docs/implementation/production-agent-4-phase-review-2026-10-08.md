# 四阶段提交前差异审查

审查日期：2026-10-08
基线：`081a7aede045190b5b6619bc97815f0407f7bc6f`（与 GitHub `master` 一致）
范围：四阶段工作树相对基线的 142 个已跟踪文件差异、51 个新增文件（合计 193 个路径）。

## 结论

| 项目 | 结果 |
|---|---:|
| 未解决的 Critical / High 代码发现 | 0 |
| 本次审查期间已修复的 High 发现 | 2 |
| 新增/调整的关键测试和 CI 覆盖发现 | 1 |
| 推荐 | 条件通过：提交并开 PR，等待全部必需 CI 状态通过后合并 |

阶段代码可以进入 PR 审查。仓库目前只有 GitHub `CI` workflow，没有 CD workflow；因此本审查不声称部署已通过，也不把生产集群、真实模型或 ERP 沙箱门槛当作代码验收证据。

## 审查范围与安全边界

按大型仓库的 surgical 策略，重点检查了 Tool Gateway、任务依赖与回执、AgentRun/Worker 恢复、出站与预算、tenant/RLS 查询、记忆和擦除、Prompt 发布证据、迁移 `0072–0080`、Kubernetes scrape/network policy 及其对应测试。检查了高风险 diff 中删除或替换的租户过滤、授权、幂等和锁定逻辑；没有发现未由等价校验取代的授权或 RLS 防护删除。

关键执行路径由四层控制：当前租户从服务端上下文解析；写入前由 Gateway 重验 actor、租户、工具风险、确认和前置条件；外部调用前持久化 execution intent；只有 verified receipt 才把业务任务记为成功。UNKNOWN 外部结果要求人工对账，未找到可核验结果时不会盲目重放。

## 审查发现与处置

### HIGH：完成后的人工重跑可被新幂等键再次触发

`rerun_failed_run` 原先只拒绝 queued/running 子重跑。旧失败 run 保持 failed 时，第一次重跑完成后，使用另一个幂等键仍可能创建第二条客户回复；分开的状态查询也存在旧重跑由 running 转 completed 的竞态窗口。

已在 [rerun.py](../../apps/api/src/platform_core/agent_runtime/rerun.py) 中锁定旧 run 与其已有重跑，在同一读取里拒绝 live 和已完成/已转人工的重跑；相同幂等键仍返回原重跑并报告其当前状态。隔离 PostgreSQL 集成覆盖同键终态重放和新键拒绝，Worker 演练 97 项通过。

### HIGH：Admin Web 锁定的 source-map-js 存在 high 级公告

依赖扫描指出 `source-map-js@1.2.1` 存在 `GHSA-68fv-2mgg-jv7q`。已将 [package-lock.json](../../apps/admin-web/package-lock.json) 中该传递依赖更新到兼容补丁 `1.2.2`；干净 `npm ci` 后 `npm audit --audit-level=high` 报告 0 vulnerabilities，Admin Web tests、typecheck 和 build 通过。

### MEDIUM：Contract suite 未进入 GitHub CI 单测命令

OpenAPI contract tests 已存在于 `apps/api/tests/contract/`，但 CI 的显式路径列表没有运行该目录。已将 contract suite 和仓库级 `tests/unit` 加入 [.github/workflows/ci.yml](../../.github/workflows/ci.yml) 的 unit-tests job。该改动让 PR 必需状态实际执行这些本地断言。

## 测试覆盖与证据

- 新建随机 PostgreSQL/Compose 项目运行 Worker drill：97 passed；包含 AgentRun 重跑幂等、migration `0080` 回滚/重升、租户隔离、Worker SIGKILL 接管和重复投递单次记账。`APP_TEST_DATABASE_URL` 指向 `platform_app`；临时数据库、卷和容器已清理。
- 全量 API unit、contract、`packages/contracts/tests` 与仓库 `tests/unit` 通过；Admin Web tests、typecheck 和 production build 通过。
- Mypy：272 个源码文件通过。Ruff check/format：全 CI scope 通过。`detect-secrets` 基线扫描、`pip-audit --strict` 和 `npm audit --audit-level=high` 通过。
- API image 本地构建通过 7 项内容/运行时探针。镜像未推送。
- `git diff --check` 通过。路线图 24 个清单项显示四阶段各 100%；该值是清单加权，不是生产就绪指标。

## Blast radius 与限制

Tool Gateway 被 API 主路由、Workbench task command、visitor/support flow 等 4 个 API 源文件构造；该入口覆盖面以真实 Gateway 集成、RLS 和撤权竞态用例验证。AgentOrchestrator 的 Worker 与同步/支持入口共享执行预算与 terminal claim；fresh PostgreSQL Worker drill 和全量 unit suite 覆盖其主要恢复边界。

本次没有对全部 140 个已跟踪文件逐行复核。Kubernetes Operator/OTLP receiver、真实连接器/ERP 条件写、实际模型成本与延迟、multi-host capacity、IdP 撤权以及法律/产品批准的全量合规导出仍需各自环境或负责人验收。CI 通过只代表仓库自动化门禁通过，不代表这些生产门槛已满足。
