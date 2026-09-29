# 发给 WorkBuddy 的开发指令

将下列内容复制给 WorkBuddy，并让其读取共享仓库中的链接文档。若它使用独立 Worktree，T00 先从下面的绝对项目路径读取执行包，再将完整执行包复制到自己的开发分支并记录来源。尚未提交的文件不会自动进入另一个 Worktree；不要因此修改或清理共享主工作区。

---

请实现 B2B AI 客服平台 R1 能力增强，基于以下执行包开展工作：

- 项目：/Users/mac/Documents/Codex/b2b-ai-support-platform
- 总入口：docs/implementation/ai-support-v2/README.md
- 目标契约：docs/implementation/ai-support-v2/spec.md
- 架构决策：docs/implementation/ai-support-v2/architecture-decision.md
- 开发任务：docs/implementation/ai-support-v2/tasks.md
- 最终验收：docs/implementation/ai-support-v2/acceptance.md

执行 T00–T09：语义理解接口与影子模式、多意图任务、受控工具候选与补参、真实坐席副驾、工作台任务体验和交付证据。R2/R3 是后续计划，本次不要用占位实现把它们标记完成。

先读当前 AGENTS.md、现有代码与执行包，记录当前 commit 和契约差异，检查其他 Worktree/用户改动，使用独立功能分支。复用现有 provider、Tool Gateway、RLS、会话租约、Worker、审计和评估机制。现有 CLASSIFY 模型配置入口已存在，先核对实际调用链。

模型输出仅作建议；服务端推导 tenant/actor 并裁决权限。先过滤租户可用工具，再接受语义候选。写动作经既有提案与确认路径；没有实际能力的改地址/补开发票必须进入人工，不伪造工具或成功结果。任何新业务表都有 tenant_id 和 FORCE RLS；模型原始输出、客户原文、敏感槽位和凭据不得进入普通日志和公开证据。

默认关闭新能力。shadow 异步分析且不改变业务状态；实际模型无凭据/预算时继续完成代码、fake provider 的控制逻辑测试和故障路径，真实模型质量门禁标为阻塞。启用 semantic_read 需要 ADR 正式评审及真实模型验收证据。不得自行购买服务、上传真实客户数据或扩大付费调用范围。

按任务依赖交付有逻辑边界的提交，维护 OpenAPI、类型、迁移、权限、审计、失败恢复、回滚与用户旅程。不要将 .codex/、.DS_Store、密钥、用户数据或他人改动提交。不要降低门禁、删除失败测试或把 mock 通过写成生产通过。

最终提供：
1. 分支、PR/完整 commit、相对基线的文件清单和迁移记录；
2. T00–T09 与全部验收 ID 的完成/失败/阻塞状态；
3. 启动、开关、演示、复现与回滚命令；
4. 脱敏证据 manifest、模型/数据集/Prompt 版本、质量与成本/性能报告；
5. 真实服务未配置部分、缺陷和剩余工作。

开发完成后等待 Codex 独立验收。该交接没有授权自动合并或生产发布，也不要求你替 Codex 填写最终通过结论。

---
