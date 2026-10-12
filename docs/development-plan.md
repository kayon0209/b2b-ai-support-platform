# 交付范围与版本管理

当前架构为自有客户入口、会话租约工作台与证据驱动 AI Runtime。代码注释中的早期 Phase 编号为历史定位，不代表仍有同名开发任务。

## 当前产品范围

客户入口、坐席接待、工单/SLA、知识检索、受控工具、审批、运营质量与租户安全见 [能力矩阵](capabilities.md)。运行环境要求、企业系统接入和实现限制统一见 [生产接入条件](production-readiness.md)。

## 已有实现记录

- [四阶段实现摘要](implementation/production-agent-roadmap.md)
- [AI 增强契约](implementation/ai-support-v2/README.md)
- [架构决策](adr/README.md)
- [发布记录](releases/README.md)

## 版本交付原则

每次变更同步维护 API/schema、授权/RLS、幂等、审计、指标、失败恢复、开关/回滚和验证范围。产品版本只承诺已完成并有证据支持的能力；企业环境验证、模型质量与生产容量不得由本地合成结果代替。

本路径保留供旧代码注释引用，不再维护与当前文档重复的阶段待办。
