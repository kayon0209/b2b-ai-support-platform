# 产品与工程文档

本平台提供自有客户入口、坐席工作台、受控 AI 运行链路与运营管理能力。当前文档按产品、部署、接口、安全和验证组织；开发过程与历史评估不作为当前功能说明。

## 产品与能力

- [项目概览](../README.md)
- [能力矩阵](capabilities.md)
- [工作台规格](workbench-redesign-spec.md)
- [工作台实现与接口边界](workbench-redesign-implementation.md)

## 架构与接入

- [系统架构](architecture.md)
- [领域模型](domain-model.md)
- [AI Runtime](agent.md)
- [API 与事件契约](api-contracts.md)
- [渠道与业务系统集成](integrations.md)
- [安全与信任边界](security.md)
- [架构决策索引](adr/README.md)

## 部署与验证

- [部署与运维](deployment-and-operations.md)
- [生产接入条件与已知限制](production-readiness.md)
- [测试与评估](testing-and-evaluation.md)
- [发布检查与事件处置](launch-checklist-and-runbook.md)
- [Kubernetes 配置](../infra/kubernetes/README.md)

## 版本与工程参考

- [发布记录](releases/README.md)
- [AI 增强能力契约](implementation/ai-support-v2/README.md)
- [四阶段实现摘要](implementation/production-agent-roadmap.md)
- [代码功能编号索引](feature-list-index.md)
- [历史资料与证据](archive/README.md)
- [文档维护规范](documentation-policy.md)

**状态解释**：代码实现、自动化测试、本地合成验证和企业环境验证是不同范围。历史报告中的结果只适用于报告列明的提交与环境；当前文档不把本地结果升级为生产认证。
