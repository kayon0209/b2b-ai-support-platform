# 代码功能编号索引

代码注释中的 `feature list N.N` 来自早期规划。原始规划不在本仓库，也不是公开文档，因此无法核对原始编号措辞。本索引只映射可检查的当前源码与决策，不冒充原始需求文档。

实现路径以 `apps/api/src/platform_core/` 为根。测试覆盖以当前测试套件为准，不维护会随改动失效的行数或测试数量。

## 编号 → 实现映射

| 编号 | 主题 | 实现 | 决策记录 |
|---|---|---|---|
| 1.3 | 附件最小化（板图等敏感字段剥离） | `support_bridge/minimize.py` | — |
| 1.5 | 会话↔联系人关联持久化 | `support_bridge/continuity_models.py` | — |
| 2.5 | 读工具须能回答账号相关问题 | `integrations/demo_erp.py` | ADR 0006 |
| 3.2 | 业务线识别（问题的第二轴） | `agent_runtime/intent.py` | **ADR 0007** |
| 4A.3 | 读工具结果转卡片（非自由文本） | `agent_runtime/tool_card.py` | ADR 0005 |
| 4A.4 | 同上（与 4A.3 合并引用） | `agent_runtime/tool_card.py` | ADR 0005 |
| 4B | 确定性 PCB 报价 | `pricing/engine.py` | — |
| 4B.2 | PCB 工艺能力矩阵 | `pricing/capability.py` | — |
| 6.1 / 6.2 | 承诺红线（不承诺做不到的事） | `agent_runtime/orchestrator.py` | `docs/agent.md` § Evidence policy |
| 7.1 | 转人工的第七个触发条件 | `agent_runtime/emotion.py` | — |
| 7.2 | 情感检测 | `agent_runtime/emotion.py` | `docs/agent.md` § Routing classes |
| 7.3 | 转人工的团队归属 | `agent_runtime/routing.py` | — |
| 7.5 | 人工是否真的在（工作时间） | `agent_runtime/hours.py` | — |
| 7.8 | 答案纠正持久化 | `knowledge/correction_models.py` | — |
| 7.10 | 客户满意度持久化 | `support_bridge/csat_models.py` | — |
| 8.1 | 质量指标（哪些问题仍需人工） | `evaluation/metrics.py` | ADR 0009 |
| 8.2 | 分渠道量与自动化率 | `evaluation/channels.py` | — |
| 8.3 | 会话回放 | `agent_runtime/replay.py` | — |
| 8.5 | 坐席绩效与回复采纳率 | `evaluation/agent_metrics.py` | — |
| 8.6 | A/B 实验定义 | `evaluation/ab_models.py` | — |
| 8.7 | 各意图轴计数与趋势 | `evaluation/metrics.py` | — |
| 9.2 | 影子模式（只产出不发送） | `agent_runtime/orchestrator.py` | — |
| 10.2 | 外部系统不可用 | `agent_runtime/qa_path.py` | ADR 0006 |
| 11.2 | 模型调用为何存在 | `llm/factory.py` | — |

## 原始编号无法核对的部分

上表可以定位代码，不能还原未提供的原始规划措辞。原始需求由其合法持有人确认；当前产品能力以 [能力矩阵](capabilities.md) 为准。Demo adapter 只表示本地路径存在，不表示真实业务系统已经接入。

## 维护

新增或移除源码编号时同步更新映射。`test_feature_list_index.py` 检查编号覆盖、路径与原文范围声明；不要用空白映射或统计数字代替实际实现说明。
