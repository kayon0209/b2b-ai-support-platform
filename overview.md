# b2b-ai-support-plan — 本轮开发报告

**提交**：`9d4a384`（连接器健康/重授权）、`2e4ee13`（排序修复）、`f03bcd5`（P1 摄取回归 + 死信）
**验证基线**：全量 **776 passed, EXIT=0**（754 → 776）；真实 MinIO 端到端绿；`ruff check` / `ruff format --check` / `mypy`（105 文件，0 错）全绿；alembic 在 `0025`
**环境**：Docker Desktop 已恢复；`ai-postgres:5435` / `ai-redis:6380` / `b2b-e2e-minio:19000` 可用

---

## 一、本轮最重要的一件事：一个 P1 摄取回归，是端到端跑出来的

Docker 恢复后**第一件事就是跑真实 MinIO 端到端**，它立刻失败了：

```
IngestionError: unsupported content type for parsing: "text/markdown"
```

注意值**外面的引号**。`parse_document` 是精确匹配内容类型的，`"text/markdown"` 带引号就什么都匹配不上。

根因（改任何东西之前先在活库上取证）：

```sql
'{"content_type": "text/markdown"}'::jsonb ->  'content_type' ::text  => "text/markdown"   -- 带引号
'{"content_type": "text/markdown"}'::jsonb ->> 'content_type'         => text/markdown     -- 干净
```

`->` 返回 **JSON** 值，把 JSON 字符串转 `text` 会保留两侧双引号。迁移 `0024` 用了 `->`。

**后果**：**每一个经 API 上传的文档都必然入库失败**——因为只有 API 路径会在 `metadata` 里写 `content_type`。

**为什么测试全绿还是漏了**：`test_ingestion_worker.py` 的 `_make_version` 播种的版本**根本没有** `content_type`。`->` 取不存在的键，两个算子都返回 SQL NULL，worker 走 markdown 兜底，于是全过。**fixture 没有复现生产那一行**——这就是这类覆盖盲区的形状。

修复：迁移 `0025` 改 `->>`（签名不变，故 `CREATE OR REPLACE`），`EXPECTED_MIGRATIONS` 24 → 25；`_make_version` 支持按 API 的写法播种；新增三个测试，其中一个**断言精确字符串**（`in` 会放过带引号的值），另一个把 e2e 路径钉进套件，使它不可能再退回「只有 e2e 能发现」。

## 二、Phase 3：连接器健康、重授权、凭证轮换

Phase 3 验收标准「OAuth 重新授权可见且可操作」此前**不可能成立**——三个缺陷同属「存在但从不被消费」：

| 缺陷 | 证据 |
|---|---|
| `health_check()` 零调用方 | 定义于 4 处，grep 调用方 = 0 |
| `NEEDS_REAUTH` / `DEGRADED` / `last_health_at` 从不被写 | 枚举与列已定义，无生产赋值 |
| 无轮换路径 | `credentials.py` 只解析 `env://` |

**最值得留存的设计决策**：`health_check()` 走的是**无鉴权**的 `GET /health`，所以探测成功**不能**证明凭证有效。由此推出两条规则：

1. 探测**永远不能**清除 `NEEDS_REAUTH`——否则一个已知被拒的凭证会被静默重新武装，而平台对 `active` 连接器接下来的动作是**执行写操作**。
2. 清除 `NEEDS_REAUTH` 只能靠显式运维动作，且该动作还必须证明凭证**可解析**——这条拦住的是「运维把引用指向了忘记设置的变量」，否则 API 会对一个仍无法鉴权的连接器报告 `active`。

另：`{"api_token": ""}` 视为**缺失**，因为 `Authorization: Bearer ` 不是凭证；无 scheme 的引用被拒，因为那通常是粘贴进来的密钥，而该列被每个请求读取并进备份。

一个由**集成测试**（不是评审）暴露的排序缺陷：两个条件同时失败时我先报「不可达」。运维真正能修的是自己那个缺失的密钥，先报网络会让他们白跑一轮——已交换顺序并加测试钉住。

## 三、Phase 3：死信从「只有模型没有生产者」变成可见工作

`DeadLetterItem` 有模型、有保留期清扫，**没有生产者**：重试耗尽的连接器操作除了一个没人列出的失败 `ToolExecution` 行之外什么也不留。

- **载荷永不落库**。行里存 `"{tool}:{sha256(params)[:32]}"`，仍能回答「同一个操作是不是失败了 40 次」，同时让客户派生的值不进入一个被运维端点读取、被备份复制的表。`sort_keys=True` 是承重的：少了它，插入顺序不同就得到不同摘要，这个归组会**静默失效**。
- 鉴权拒绝**排除在外**（它有 NEEDS_REAUTH）；其余全部记录，且**歧义优先于错误码**，行上写 `AMBIGUOUS_OUTCOME`，因为运维的第一个问题是「到底有没有写进去」——这是错误码回答不了的。
- **刻意不做重放端点**：载荷不在行里（设计如此），而歧义行在有人确认第一次是否落库之前不能重试——那是运维判断，不是循环。

## 四、顺带修的与记录的

- `AuthReportingExecutor` 改名 **`ConnectorOutcomeExecutor`**：它现在还产出死信，旧名字会主动误导——正是本仓库专门审计的那类缺陷。
- 写 fixture 时踩到已知坑：`document_versions.metadata` 是 NOT NULL 且有 `'{}'` 默认值，**显式传 NULL 会覆盖默认值** → `NotNullViolation`。
- 库里 2026-09-17 那条 `'list' object has no attribute 'vectors'` 失败记录是**历史残留**（当时脚本已改），当前 e2e 走同一路径且通过，非活动缺陷。

## 五、剩余待办（按既定顺序）

1. **Phase 3 收尾**：`SyncCursor` 写入/读取（增量同步位置）、连接器 webhook 摄取端点
2. **Phase 4**：入站限流中间件、备份/恢复演练脚本、**特性开关运行时消费**（开关可定义、可审计、可预览，但**不改变任何行为**，故灰度当前不可执行）
3. **Phase 5**：自定义域名 Host→tenant 路由、SAML/SCIM、`infra/kubernetes/` HA 模板
4. **最终**：整体验证与端到端（Postgres + Redis + MinIO 已通；Chatwoot + Keycloak 待起）

已知且**有意保留**的缺口：`ambiguous-refund-eligibility`（`must_abstain=True`）。正确解法是检索前做完账户身份解析，而非正则匹配问题文本；不得为让评测变绿而放宽该用例。
