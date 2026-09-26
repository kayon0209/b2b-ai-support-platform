# Feature list 编号索引

代码注释里引用了 20 处「feature list N.N」编号（例如 `agent_runtime/intent.py` 的
「Which product line the question belongs to (feature list 3.2)」）。**那份编号表本身不在本
仓库里**，也从未在 git 历史中出现过（核实于 2026-09-26：`git log --all --name-only` 无任何
匹配文件）。

它不是公开文档，因此无法从网上取得。它是本项目立项期针对**华秋智联**这个客户写的产品规划，
内容是该客户的业务流程、内部难点评估与交付路线——公开网络上不存在，也不应该存在。

本文档是那类悬空引用的替代品：把每个编号映射到**仓库内确实存在的实现、测试与决策记录**。
查得到的给出路径；原始编号的措辞无法核对的，如实说明，而不是猜一个。

## 怎么用这份索引

- 想知道「7.3 为什么这样设计」→ 找到该行，看「决策记录」列指向的 ADR，那是有日期、有正文的
  决策文档，比一个编号可查得多
- 想确认某编号对应的功能**是否真的实现了**→ 看「实现」列是否非空。空缺意味着注释在声称一个
  本仓库没有的功能
- 想确认某功能**有没有测试**→ 看「测试」列。数值为 0 且备注「间接覆盖」表示没有直接导入该
  模块的测试，但通过表名或端点被测到

## 编号 → 实现映射

| 编号 | 主题 | 实现 | 行数 | 测试 | 决策记录 |
|---|---|---|---|---|---|
| 1.3 | 附件最小化（板图等敏感字段剥离） | `support_bridge/minimize.py` | 100 | 2 | — |
| 1.5 | 会话↔联系人关联持久化 | `support_bridge/continuity_models.py` | 38 | 间接 5 | — |
| 2.5 | 读工具须能回答账号相关问题 | `integrations/demo_erp.py` | 177 | 1 | ADR 0006 |
| 3.2 | 业务线识别（问题的第二轴） | `agent_runtime/intent.py` | 1201 | 8 | **ADR 0007** |
| 4A.3 | 读工具结果转卡片（非自由文本） | `agent_runtime/tool_card.py` | 275 | 3 | ADR 0005 |
| 4A.4 | 同上（与 4A.3 合并引用） | `agent_runtime/tool_card.py` | 275 | 3 | ADR 0005 |
| 4B | 确定性 PCB 报价 | `pricing/engine.py` | 199 | 1 | — |
| 4B.2 | PCB 工艺能力矩阵 | `pricing/capability.py` | 182 | 1 | — |
| 6.1 / 6.2 | 承诺红线（不承诺做不到的事） | `agent_runtime/orchestrator.py` | 3280 | 24 | `docs/agent.md` § Evidence policy |
| 7.1 | 转人工的第七个触发条件 | `agent_runtime/emotion.py` | 127 | 2 | — |
| 7.2 | 情感检测 | `agent_runtime/emotion.py` | 127 | 2 | `docs/agent.md` § Routing classes |
| 7.3 | 转人工的团队归属 | `agent_runtime/routing.py` | 112 | 2 | — |
| 7.5 | 人工是否真的在（工作时间） | `agent_runtime/hours.py` | 128 | 3 | — |
| 7.8 | 答案纠正持久化 | `knowledge/correction_models.py` | 37 | 间接 3 | — |
| 7.10 | 客户满意度持久化 | `support_bridge/csat_models.py` | 37 | 间接 3 | — |
| 8.1 | 质量指标（哪些问题仍需人工） | `evaluation/metrics.py` | 547 | 5 | ADR 0009 |
| 8.2 | 分渠道量与自动化率 | `evaluation/channels.py` | 154 | 1 | — |
| 8.3 | 会话回放 | `agent_runtime/replay.py` | 422 | 间接 12 | — |
| 8.5 | 坐席绩效与回复采纳率 | `evaluation/agent_metrics.py` | 298 | 1 | — |
| 8.6 | A/B 实验定义 | `evaluation/ab_models.py` | 46 | 间接 3 | — |
| 8.7 | 各意图轴计数与趋势 | `evaluation/metrics.py` | 547 | 5 | — |
| 9.2 | 影子模式（只产出不发送） | `agent_runtime/orchestrator.py` | 3280 | 24 | — |
| 10.2 | 外部系统不可用 | `agent_runtime/qa_path.py` | 1524 | 18 | ADR 0006 |
| 11.2 | 模型调用为何存在 | `llm/factory.py` | 92 | 2 | — |

全部 27 处引用、21 个不同编号（4A 与 4A.3/4A.4、6.1 与 6.2 存在合并引用）均已收录。
**每一行的实现文件都真实存在**，行数为实测值。

## 原始编号无法核对的部分

上表能映射「这个编号对应仓库里哪个东西」，但**不能**核对「原始规划里这个编号具体是怎么表述的」。
例如：

- 注释说「feature list 6.1/6.2」是「red-line guard」，我据 `docs/agent.md` 的
  § Evidence policy 与 § Abstention rules 推断二者对应——**这是推断，不是核对**
- 注释里「华秋 research difficulty 4」这一维度（见 `orchestrator.py:1432`）在本仓库内
  **没有任何对应记录**，无法追溯

如果需要完全准确的原始表述，得由持有那份规划文档的人提供。在此之前，本索引的定位是
**可核对的替代品**，而不是原始文档的还原：它能告诉你代码在哪、测没测、为什么这样设计，
但不能替代原文。

## 维护

新增 feature list 编号引用时，同步在本表加一行。若某个编号在本仓库**没有实现**，
如实写「未实现」而不是留空——留空会被读成「还没查」，而「未实现」是一个更值得追的结论。
