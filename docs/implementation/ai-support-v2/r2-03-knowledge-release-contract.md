# R2-03 知识版本评测与发布门禁契约

状态：固定数据集比较、双人批准和回归判定的共享契约已实现并有纯契约测试；尚未持久化 release evidence，也未接入知识发布 API、灰度开关或回滚 UI。

## 固定评测证据

实现位置：`packages/contracts/src/platform_contracts/knowledge_release.py`。

`KnowledgeEvalRun` 将每次结果绑定到：tenant、知识空间、文档版本、知识内容 SHA-256、评测集 SHA-256、检索配置 SHA-256、evaluator 版本、代码 commit、UTC 执行时间和指标。批准 fingerprint 对完整候选运行结果做规范化 JSON SHA-256，评价人不能批准一个结果后再替换成不同内容/指标。

前后比较必须同租户、同空间、同评测集、同检索配置、同 evaluator、同代码 commit 和相同样本数；知识版本或内容必须改变。否则返回 `EVAL_INPUT_MISMATCH` / `KNOWLEDGE_VERSION_UNCHANGED` 并阻止发布。

## 发布判定

- 至少两名不同审核人批准同一个候选 fingerprint，审核人必须不同于草稿作者；单人、多次点击、作者自批或旧版 fingerprint 均不通过。
- 候选语料出现任一 unsafe answer 即阻止发布。
- 已声明 grounded answer、citation support 与 retrieval recall 三个指标；任一相对基线下降超过 2 个百分点即阻止发布。
- 2 个百分点目前是开发期保守默认值，尚未由企业知识/安全负责人签字；不得直接将 `eligible` 当作生产授权。
- 当前 gate 仅比较活动基线与候选版本的发布前证据。发布后的同版本 post-test 比较、灰度状态及回滚目标尚未建模；线上 post-test 未完成或退化时不得宣称发布成功，应恢复上一活动知识版本。

## 当前限制与下一步

本提交只提供不可变契约和确定性判定函数；还没有数据库表保存 artifact、批准和活动版本，没有审计写入、发布锁、并发冲突控制、CI artifact storage、Feature Flag 灰度、回滚命令和 Workbench 展示。因此这项仍标记为部分完成，不能把契约单测当成知识发布闭环验收。

下一步需要把 run fingerprint / approval / release state 落入 tenant-RLS 表，在 `knowledge_drafts` 发布路径内事务性要求 gate，通过锁或版本 CAS 防止并发发布；部署期间保留上一个已验证版本，发布后由同一固定评测集完成 post-test，并允许一键回滚到保存的知识版本。

## 本地验收

`packages/contracts/tests/test_knowledge_release.py` 的 11 项测试覆盖同一评测输入、同一审批 fingerprint、双人独立批准、作者自批拒绝、unsafe 样本拒绝、阈值边界/回归、知识内容版本未变和 UTC 时间边界。它验证门禁函数，不验证数据库 RLS、发布事务或 UI 回滚。
