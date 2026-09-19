# ADR 0007: 不做通用 multi-agent，按 Scene 做子流程分派

日期：2026-09-19　状态：已接受

## 背景

"何时拆多 Agent" 是 Agent 平台的常规问题。当前单 Agent 确定性流水线在三个位置
依赖"单一决策点"：

1. **授权**：Principal → PolicyEngine → 网关复核，一条链路；
2. **幂等**：每个 run 一个幂等键空间，工具执行靠 (tenant, idempotency_key) 去重；
3. **审计**：一次 run 一个 trace_id，事件按 run 聚合。

拆成多 Agent 意味着这三个点都变成分布式一致性问题：两个 Agent 各自提议、各自
写审计、共享同一个幂等键空间时会互相覆盖。收益只有并行度——而当前瓶颈是检索
与模型延迟（见延迟治理），不是编排。

## 决策

**不实现通用 multi-agent。** 按 Scene 做**子流程分派**：同一进程、同一 Case、
同一控制租约、共享 `ConversationMemory`（压缩后的上下文），**不共享对话原文**
——原文按需从 Chatwoot 拉取，避免多份副本各自脱敏造成的漂移。

Scene 切换（BILLING → TECHNICAL_SUPPORT）时：memory 保留（话题转移检测已有，
`topic_shifted` 会阻止旧话题污染新检索），Case 不变，租约不变。

## 重新评估的触发条件

- 出现**独立的审批型 Agent**（需要与主 Agent 不同的授权身份）；
- 单一 Scene 的编排复杂度超过可维护阈值（该 Scene 的分支逻辑超过一个模块的
  承载能力，且 Scene 间开始互相调用）。

出现任一情况时，先写新 ADR，再动代码。
