# AI 增强能力契约与实现资料

此目录保存语义理解、多意图任务、副驾、标准流程、知识发布和企业数据的范围与接口契约。当前产品范围以 [能力矩阵](../../capabilities.md) 为准，部署与模型/业务验证要求以 [生产接入条件](../../production-readiness.md) 为准。

## 能力分层

| 层 | 实现范围 | 授权边界 |
|---|---|---|
| R1 | 语义契约、影子分析、多意图任务、补参、工具候选、坐席副驾与任务面板 | 模式、租户开关、模型与数据目的地；语义输出不提升工具权限 |
| R2 | 情绪建议、标准服务流程、知识改进与发布门禁 | 建议不自动修改优先级/SLA/负责人；流程受控；知识发布需获批评测与签名证据 |
| R3 | 企业业务数据契约、归属/时效验证、售前资料与商机交接扩展点 | 真实企业连接器单独接入；local/test 合成路径不是生产 ERP/CRM |

## 契约参考

1. [范围与实施契约](spec.md)
2. [语义架构设计](architecture-decision.md)
3. [验证协议](acceptance.md)
4. [语义数据集与标注规范](semantic-evaluation.md)
5. [情绪建议接口](r2-01-emotion-advice-contract.md)
6. [标准流程目录](r2-02-standard-flow-catalog.md)
7. [知识发布评测门禁](r2-03-knowledge-release-contract.md)
8. [企业业务数据契约](r3-business-data-contract.md)

早期规格包含当时的阶段约定；当前实现已扩展时，以现行能力矩阵、API/代码和绑定版本的报告综合核对，不从一个历史任务状态推断完整现状。

## 历史过程与证据

[交付记录](delivery-report.md)、[R2/R3 阶段记录](r2-r3-execution-plan.md)、[原任务单](tasks.md)、[原交接指令](workbuddy-handoff.md)、[基线差异](baseline-and-contract-delta.md) 保留原路径以兼容既有证据链接，不作为当前待办入口。

`evidence/` 只保留允许公开的脱敏摘要、环境指纹、命令和截图；客户原文、密钥、数据库快照和模型原始输出不得进入 Git。历史记录中的通过仅适用于其列明的 commit、环境和样本。
