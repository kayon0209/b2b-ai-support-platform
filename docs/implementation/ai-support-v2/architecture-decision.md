# ADR 草案：语义建议与确定性业务控制协作

日期：2026-09-26  
状态：Proposed，待开发入口评审；尚未取代任何已接受 ADR。  
关联：[ADR 0007](../../adr/0007-no-multi-agent-scene-dispatch.md)、[ADR 0012](../../adr/0012-remove-chatwoot.md)、[ADR 0015](../../adr/0015-conversation-first-workbench.md)。

## 背景

当前 `intent.classify`、`emotion.detect_emotion` 与 `tool_gateway.selector` 依靠确定性规则。它们能给出可解释的匹配依据，但跨轮指代、复杂表达、多意图拆解和参数提取覆盖有限。现有主次意图没有形成逐任务处理记录，工作台“刷新建议”也需要实际模型生成能力支撑。

本改动引入语义分类建议与有限任务协调器。运行时的权限、租户隔离、工具执行和会话发送控制仍由平台应用接口负责。ADR 0007 关于通用多 Agent 的限制继续有效；其中历史渠道描述按 ADR 0012 解释。

## 拟采用的决策

1. 规则、语义模型和最终路由分别留存版本化结果，允许对比和回放。语义模型输出是非可信输入。
2. 采用 `off / shadow / assist / semantic_read` 模式；默认 off。shadow 不改变客户回复、队列归属、工单或工具动作，也不创建任何业务提案。
3. 客户消息先解析身份、最小化内容并执行现有硬规则；显式转人工、敏感请求、索赔争议和已有人工所有权不能被语义建议撤销。
4. 模型可建议场景、意图、槽位和有限工具候选。服务端先按租户连接器、当前角色和场景缩小能力集合；模型输出后再次授权。
5. R1 语义任务图为有界 DAG，最多 5 个任务、深度 3。无循环、自主递归或跨租户子任务；业务写动作不由任务协调器自动执行。
6. R1 的写请求进入现有提案/审批流程，或形成待人工处理任务。历史已授权的确定性低风险写路径遵循原契约；语义增强不扩大其权限范围。
7. 会话任务使用稳定业务幂等标识，跨消息重试、Worker 重启和新 AgentRun 保持一致；修改动作参数必须撤销旧确认并生成新动作版本。
8. 新增数据归属现有模块：任务/语义结果归 agent_runtime，确认与执行归 tool_gateway，租约归 identity，Case/SLA 归 cases。跨模块经应用接口调用。
9. 复用现有模型 provider、PostgreSQL、Worker 和观测组件。新增基础设施必须另有测量证据和 ADR。
10. 不保存模型思维链。普通日志和指标不携带消息原文、槽位值、引用正文、令牌或高基数客户标识。

## 替代方案与取舍

| 方案 | 优点 | 成本或不足 |
|---|---|---|
| 持续扩充词表 | 成本低、行为可复现 | 长尾表达与跨轮信息需要持续人工维护 |
| 全量交给自由 Agent | 多步任务灵活 | 难以限制动作、回放和确定失败责任，不作为本期路线 |
| 分层语义建议 + 确定性控制 | 能逐步提高理解覆盖并保留执行边界 | 多一层推理与评估，需要成本/延迟预算、标注样本和降级策略 |

## 批准与落地

T00 由 WorkBuddy 将本草案与代码现状对齐，填入评审人、日期、具体批准范围。用户的路线授权允许准备和实现关闭/影子模式；架构评审可在 PR 内由 Codex完成，不要求再询问一次路线选择。进入影响客户路由的 semantic_read 模式前，本决策须正式接受并满足验收协议。正式 ADR 编号在提交时按仓库实际占用分配，不能覆盖历史文件。

批准记录：待填写。  
实现版本：待填写。  
启用记录：待填写。

## 调研依据

以下沿用本次会话 2026-09-26 已查阅的官方资料，作为方案来源；产品版本与可用性应在供应商选型时重新核对。本项目不继承厂商营销准确率或安全承诺。

- [Google Dialogflow CX 意图匹配](https://docs.cloud.google.com/dialogflow/cx/docs/concept/intent)：训练短语、分类阈值与 no-match。
- [Amazon Lex 候选意图及置信值](https://docs.aws.amazon.com/lexv2/latest/dg/using-intent-confidence-scores.html)：候选比较和降级；分数不能当作绝对概率。
- [Microsoft Copilot Studio 编排](https://learn.microsoft.com/en-us/microsoft-copilot-studio/guidance/generative-orchestration)：工具、主题与知识的多步组合。
- [Amazon Bedrock 用户确认](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-userconfirmation.html)：动作执行前的确认边界。
- [腾讯任务流](https://cloud.tencent.com/document/product/679/116597)：智能追问与标准业务流程。
- [Amazon Connect 会话分析规则](https://docs.aws.amazon.com/connect/latest/adminguide/build-rules-for-contact-lens.html)：时间窗口内的情绪与其他业务条件组合。

由这些资料推导出的本项目选择是有界语义建议、确定性裁决、可恢复任务和独立验收；它属于项目设计决策，不代表上述厂商均采用同样的内部实现。
