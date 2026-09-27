# R1 本地验收证据

- 验收代码提交：`247290e5df86c529dde17465efaab64765439764`
- PR：[#19](https://github.com/kayon0209/b2b-ai-support-platform/pull/19)
- 分支：`codex/ai-support-v2-r1-fix`
- 评测集：`semantic-v2-r1-balanced-highrisk-synthetic-2026-09-27`
- 评测集哈希：`200cb1fa0a42da8c59083cbb150e8303b4eb4f379420dbed1e9029a532c13608`
- 当前 Prompt 版本：`semantic-v7`

## 本地结果

在项目 Python 3.12 虚拟环境、隔离 Docker PostgreSQL 数据库 `r1_ui_accept_20260927` 和合成数据上完成：

| 检查 | 结果 |
|---|---|
| 完整 pytest | 2,782 passed、2 skipped、12 warnings，70.45 秒 |
| Ruff 检查 | 通过 |
| Ruff format | 562 个文件通过 |
| Mypy | 236 个源文件通过 |
| Admin Web 测试 | 通过；包含 7 项副驾 tabs 键盘移动检查 |
| Admin Web typecheck / production build | 通过 |
| `kubectl kustomize infra/kubernetes` | 通过 |
| 上版兼容回滚 smoke | `master` 基线 `96c81ad` 在 0065 扩展 schema 上读会话队列、领取并释放合成会话，均返回 200 |

完整 pytest 的 12 条非失败 warning 来自 Starlette/httpx、SQLAlchemy DISTINCT ON 弃用和现有重复 OpenAPI operation ID。

浏览器手工走查使用本地合成会话：副驾 tabs 的 ArrowRight、Home、End 可更新焦点与选中项；390 CSS px 页面能加载会话详情。1536、1280、768、390 CSS px DOM 溢出矩阵见 [交付报告 §8](../../delivery-report.md)。

## GitHub CI

对验收代码提交 `247290e5df86c529dde17465efaab64765439764` 的 [GitHub Actions run #36295891831](https://github.com/kayon0209/b2b-ai-support-platform/actions/runs/36295891831) 已通过全部检查：unit、integration、Release Evidence、Web、typecheck、lint、secret/dependency scan 和 concurrency guard。

## 限制

- 此次没有新增真实模型调用；历史诊断报告的 Prompt 版本标签有偏差，不作为当前 v7 质量证据。
- 合成语料尚未由两位独立标注人复核，不能用于生产质量声明。
- 真实 ERP/CRM 沙箱、真实双坐席在途接管、OS/多主机 Worker 重启、生产负载、真实 200% 浏览器缩放和屏幕阅读器验收未执行。
- 全部新功能开关仍关闭；`semantic_read` 未启用。

本证据文件只保留汇总、命令结果和合成环境信息，不包含原始会话、模型输出、令牌或连接串。
