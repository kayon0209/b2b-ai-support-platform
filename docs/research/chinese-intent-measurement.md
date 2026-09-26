# 影响测量：中文话术在意图分类器上的姿态

日期：2026-09-19　范围：`apps/api/src/platform_core/agent_runtime/intent.py` + `qa_path.py`

## 为什么要先测量

附录 H 发现「中文写请求到不了写路径」。但**"缺中文支持"和"缺中文写动词"是两回事**，
修法完全不同：前者要动整个词表架构，后者是加词。所以先测量全貌，再决定动不动。

## 结论（三条，按严重度）

### 1. 中文意图分类**整体**未实现 —— 这是安全级问题，不只是可达性

实测 14 句华秋场景下的中文话术，**全部**落入 `knowledge_qa` / `answer_from_knowledge`：

| 中文话术 | route | action |
|---|---|---|
| 帮我建一张工单 | `knowledge_qa` | `answer_from_knowledge` |
| 请开一张工单 | `knowledge_qa` | `answer_from_knowledge` |
| 我要投诉质量问题 | `knowledge_qa` | `answer_from_knowledge` |
| 帮我取消这个订单 | `knowledge_qa` | `answer_from_knowledge` |
| 我要退款 | `knowledge_qa` | `answer_from_knowledge` |
| 请把这个订单取消 | `knowledge_qa` | `answer_from_knowledge` |
| 帮我改一下收货地址 | `knowledge_qa` | `answer_from_knowledge` |
| **我要投诉** | `knowledge_qa` | `answer_from_knowledge` |
| 麻烦升级给技术 | `knowledge_qa` | `answer_from_knowledge` |
| **把这张单转给人工** | `knowledge_qa` | `answer_from_knowledge` |
| 我要申请退款 | `knowledge_qa` | `answer_from_knowledge` |
| 这个订单我要退掉 | `knowledge_qa` | `answer_from_knowledge` |

对照英文全部正确：

| utterance | route | action |
|---|---|---|
| please raise a ticket for this | `business_write` | `propose_write` |
| I want a refund | `business_write` | `propose_write` |
| talk to a human | `human_required` | `handoff` |
| please cancel my order | `business_write` | `propose_write` |

**「我要投诉」和「把这张单转给人工」是最严重的两条**：它们本该是
`complaint` 场景 / `human_required` 路由，实际却进了知识问答。
也就是说客户明确要求退款或要求人工时，平台会**检索语料并引用政策文档回答**。

这不是"擦肩而过"，是 `docs/agent.md` 明文禁止的行为：
- `RESTRICTED_TERMS`/`SENSITIVE` 的价值在于"无论有什么证据都不回答"；
- `_HUMAN_REQUEST` 的注释写着"Honoured immediately and unconditionally"——
  但那只对英文生效，中文客户要求人工时平台会**越权回答**；
- 退款请求被当成政策问题回答，正是模块 docstring 开篇列举的头号失败模式
  （"a refund *request* and a refund *policy question* both contain refund"），
  只不过中文下两者都成了政策问题。

### 2. 根因：分类器是**英文单语**的，中文只有一处例外

`intent.py` 的全部词汇表实测均为拉丁词：

| 词表 | 中文 | 备注 |
|---|---|---|
| `_SCENE_PATTERNS`（8 组，覆盖 6 场景） | ❌ 无 | complaint/after_sales/technical/billing 等全部 |
| `_LIVE_DATA` / `_CASE_RECORD` | ❌ 无 | |
| `_HUMAN_REQUEST` | ❌ 无 | 但英文版注释承诺"unconditionally" |
| `_SENSITIVE` | ❌ 无 | |
| `_INTERROGATIVE` | ❌ 无 | 中文疑问靠 `?` 兜底 |
| `ACTION_VERBS`（`qa_path`） | ❌ 无 | 附录 E 补的 6 个词也全是拉丁词 |
| **`_QUOTE_REQUEST`** | **✅ 有**（`多少钱｜怎么收费｜报个价｜价格是多少｜给我报`） | **唯一的例外** |

（此表是 2026-09-19 的测量快照。此后补全的进展见第二节（写意图、人工请求、场景词）
与第三节（读路径 / `_LIVE_DATA`）。）

### 3. 但措辞要收窄：中文**知识问答**是**被设计过**的，不是遗漏

`tests/evals/dataset.py` 的 `MULTILINGUAL` 分类里有 **5 条华秋中文用例**，
且注释明确记录了设计意图：

> Chinese question against an English corpus: the honest outcome is abstention
> unless cross-lingual retrieval is implemented. Recorded as `must_abstain`
> so the gap is visible rather than quietly failed.

这 5 条（发票变体对、赔付、EQ 交期、库存、`退款期限是多久`）的
`expected_route` 都是 `knowledge_qa`，**全部不含写意图**。

**所以准确结论是**：中文**读**路径（知识问答 + 诚实的弃权）已被设计并覆盖；
缺的是**写意图识别**与**人工请求识别**。附录 H 说"无任何中文动词"要修正为
"除 `_QUOTE_REQUEST` 外无中文动词"。

## 关键的机会点：`_QUOTE_REQUEST` 是可行性的证据

`多少钱` **正确**路由到 `human_required`：

| 中文话术 | route |
|---|---|
| 这个板子多少钱 | `human_required` ✅ |
| 怎么收费 | `human_required` ✅ |
| 给我报个价 | `human_required` ✅ |

这条路径**已经跑通**：往 regex 里加中文词、由既有场景/类型机制消费，
不需要新架构、不需要模型、不破坏确定性（模块 docstring 明确要求"No model call"）。
**加中文词是本仓库已验证有效的做法**，不是探索。

## 基线（必须守住，否则改动不可评价）

对 `tests/evals/dataset.py` 全部 `expected_route` 用例跑分类器：

```
with expected_route: 16    MISMATCHES: 0
```

**16/16 全对。** 任何改动都必须保持 0 不符——这正是附录 E 的纪律
（"Added after measuring every candidate against the whole evaluation dataset"）。

## 风险评估（决定改动范围）

加中文写动词有**真实的误触发风险**，且比英文更棘手：

- 中文**没有词边界**。`\b` 对中文无效，正则只能子串匹配，
  而 `_NOUN_HIT`/`_stem` 那套是为拉丁词设计的。
- 中文写动词极易在**政策问句**里出现：
  `退款` 既是"我要退款"（写）也是"退款期限是多久"（已有用例，必须留在 `knowledge_qa`）；
  `取消` 既在"帮我取消订单"（写）也在"取消政策是什么"（读）。
- 英文用 `object_markers` + imperative 形状做区分，中文没有对等的形态线索，
  需要**新造**中文的判别机制（如"我要/帮我/请+动词"的请求框架），
  而不是把词塞进 `ACTION_VERBS` 了事——塞进去按现有逻辑不会触发
  （`is_action_request` 要求 `rest[0] in object_markers`，中文宾语不在该集合里）。

**即：单纯加词不够，必须同时给中文写一版判别逻辑。** 这是本轮最重要的技术判断。

## 建议顺序（不擅自实施）

1. **先在评估数据集里补中文写意图用例**（含必须留在 `knowledge_qa` 的
   中文政策问句作为反向守卫），让缺口**先变成可观察的失败**；
2. 再实现中文请求框架判别（`我要/帮我/麻烦/请` + 写动词 + 中文宾语），
   与 `is_action_request` 并列而非混入；
3. 每步对全量 dataset 跑影响测量，保持 16/16；
4. 中文人工请求（`转人工`/`找客服`）单独一条，优先于写动词——
   它是最严重的越权回答，且最容易验证（只需正确 handoff）。

---

# 二、实施结果（测量已完成，改动已落地）

## 实际采用的机制

实现**不是**往 `ACTION_VERBS` 加中文词——上面第 2 节已证明那样不会触发。
新增的是一条**中文专有判别链**，与英文链**并列且最后求值**：

| 判别器 | 抓的形状 | 例子 |
| --- | --- | --- |
| `_CN_TICKET_REQUEST` | 建/开/创建/提/发起/生成 + `\S{0,3}` + 工单\|单\|问题单 | `帮我建一张工单` |
| `_CN_BA_CONSTRUCTION` | 把 + 宾语 + 写动词（宾语前置句） | `把这张单转给人工` |
| `_CN_REQUEST_FRAME` | 帮我\|麻烦\|我要\|我想\|请 + 写动词，**且**其后紧跟中文宾语 | `我要退款` |
| `_CN_HUMAN_REQUEST` | 人工客服\|人工\|真人\|专员\|转人工\|找个人 | `转人工` |

两道**否决守卫**（问句优先于请求）：
`_CN_PROCEDURE`（怎么/如何/什么/哪些/是否/多久/多少/什么时候）
与 `_CN_QUESTION_PARTICLE`（句尾 `吗呢吧`）。

`_CN_REQUEST_FRAME` 带**条件满足**：`我要退款` 因以 `我要` 开局、后面没有宾语也成立；
`帮我退款` 必须后面还有中文宾语才算——否则 `帮我取消订单是什么政策` 会被误判。

## 求值顺序为什么放在最后

`elif _is_cn_action_request(question)` 排在 `_looks_like_a_write` **之后**。
两种词表零重叠，实践中无歧义；放在最后使**英文所有既有用例的信号字符串逐字节不变**，
这是 16/16 基线可守住的前提。

## 结果

| 项 | 改动前 | 改动后 |
| --- | --- | --- |
| 中文写意图（8 条） | 全部落 `knowledge_qa` | **全部 `business_write`** |
| 中文人工请求（`转人工`） | `knowledge_qa` | **`human_required`** |
| 中文政策问句（7 条守卫） | `knowledge_qa` | **`knowledge_qa`（未退化）** |
| 英文用例 | — | **信号逐字节不变** |
| `test_intent.py` | 41 | **47** |

## 变异验证 4/4 全被捕获

| 变异 | 移除的东西 | 被哪个测试抓到 |
| --- | --- | --- |
| A | 中文问句否决守卫 | `test_chinese_questions_that_carry_a_full_action_frame_are_still_questions` |
| B | `_CN_HUMAN_REQUEST` 调用点 | `test_chinese_request_for_a_person_is_honoured` |
| C | `_is_cn_action_request` 调用点 | `test_chinese_action_requests_reach_the_write_path` 等 3 条 |
| D | `_CN_BA_CONSTRUCTION` | `test_the_ba_construction_is_detected_on_its_own` |

**变异 A、D 第一次都没被抓到**——A 因为早期用例碰巧仍留在知识路径，D 因为
`把这张单转给人工` 恰好也命中 `_CN_HUMAN_REQUEST`。两次漏网各自补了**专为它而写**的
守卫用例（`我要退款吗？`；`把这个订单取消`）。这正是变异验证的价值：
它测的不是"代码在不在"，而是"测试是否真的锁住了行为"。

## 门禁耦合（新发现 → 已修复，ADR 0009）

我最初把 12 条中文用例加进 `tests/evals/dataset.py` 的 `MULTILINGUAL` 类，
评估门禁立刻红：`abstention_correct_rate: 0.8293 vs 0.9`（阈值 0.90）。

**根因是架构耦合，不是用例写错**：`runner.py` 的 `elif decision.abstain:` 把
**任何**非 `must_abstain` 的弃答都记为 `abstention_false`，而中文问句打在英文语料上
**必然弃答**（`NO_AUTHORIZED_EVIDENCE`）。`expected_route` 断言的是**路由契约**，
弃答率统计的是**检索契约**——同一条用例同时断言两件事，中文路由用例天然会
污染检索统计。

**当时的处理**：把 dataset 临时回退，中文路由断言改放进 `test_intent.py`，
并明确记录"中文路由契约暂不在评估门禁覆盖范围内"。

**该缺口已由 ADR 0009 关闭**（用户明确授权改门禁语义）。做法不是放松阈值、
也不是把中文用例标成 `must_abstain`（那是把能力缺口记录成设计意图），而是
给 `EvalCase` 加 `cross_lingual` 声明，并让豁免**四条子句全中**才生效：

```python
if (decision.abstain
        and not case.must_abstain          # 本该弃答的案例不参与豁免
        and case.cross_lingual             # 必须显式声明
        and decision.reason_code == ABSTAIN_NO_EVIDENCE):  # 只有"没检索到"算语言缺口
    result.abstention_attributable = False
```

关键设计约束（每条都有对应测试）：
- `CaseResult.abstention_attributable` **默认 `True`**（fail-closed）：
  未声明的弃答照旧计入，避免"每条新用例都悄悄豁免自己"。
- `EVIDENCE_BELOW_THRESHOLD` **不豁免**：证据检索到了但分数低，是检索器调参问题，
  必须保持可见。
- `must_abstain` **不豁免**：它的弃答本来就记作 correct，豁免只会把一条**通过**的
  用例从分母里删掉（只能让分数变好）——这是本实现第一版的真实漏洞，被自己写的
  `test_a_must_abstain_case_is_never_exempt` 抓到并修复。
- 两扇新门禁兜底，防豁免被逐步扩大成"分母为空"：
  `cross_lingual_exclusions_match`（声明与实得必须一致）+
  `cross_lingual_exclusions_bounded`（豁免必须严格少数）。

豁免现在有**活消费端**：`cn-answerable-warranty-period` 与
`cn-answerable-after-sales-process` 两条中文问句，语料确实答得出但检索不到，
它们既未被标 `must_abstain`（那会把能力缺口固化），也正是豁免实际生效的地方。
（没有活消费端的豁免比没有豁免更糟——它看起来像覆盖。）

**实测（真实数字）**：`total=40`、`counted=38`、`excluded=2`、`exemptible=2`、
`abstention_correct_rate=1.0000`（在 38 条**非空**分母上，不是空分母的假 1.0）、
`failed=2`（两条真实能力缺口仍红并登记在 `KNOWN_GAPS`）、
bounded 门禁 `0.05 vs 0.5`。会计恒等式已断言：`counted + excluded == total`。

**反向验证**（ADR 自己要求"比正向更重要"的一条）：三条仅声明不同、其余全同的
案例逐案跑 `EvaluationRunner`，豁免恰好只落在声明了 `cross_lingual` 的那条；
注入一个与语言无关的真实 `false abstention` 后门禁照旧变红。

### ⚠️ 本次发现并修复的第二个漏洞：门禁"空洞通过"

`release_check._report_from_artifact` 原用 `raw.get(field, 0)` 读三个新字段。
磁盘上那份**旧**产物（`tests/artifacts/eval_report.json`，`total=23`，不含新字段）
于是把三个字段都读成 0 → `cross_lingual_exclusions_match` 比较 `0 == 0` → **绿灯**，
`release_check --evidence-only` 照常 **exit 0**。这正是 ADR 0009 自己要防的
"没有活消费端的豁免"，却复现在了本该防它的门禁里。

修法：`release_check` 改用 `-1` 作"该字段未上报"哨兵，`gates.py` 遇哨兵直接判 FAIL
并给出可操作指引。**旧产物现在会被拒**（实测）：

```
[FAIL] cross_lingual_exclusions_match: -1 vs -1
       (artifact predates ADR 0009 ... regenerate the eval report with `scripts/run_eval.py`)
[FAIL] cross_lingual_exclusions_bounded: -1.0 vs 0.5
```

对应旧测试 `test_an_artifact_without_the_new_fields_is_not_treated_as_matching`
**曾把这个 bug 断言成期望行为**，已重写为断言失败，并改走真实反序列化路径。

## 测试与门禁实测

ADR 0009 落地**之后**的复测：

| 检查 | 结果 |
| --- | --- |
| `tests/evals` | **67 passed**（含新增的 `test_abstention_scope.py` 15 条） |
| 全量 pytest | 失败数随预存 flaky 波动；隔离运行全过（见下） |
| `release_check --evidence-only` | **exit 0**；`1533 collected` / cross-tenant 15 / unauthorized-writes 4 / duplicate-replies 3 |
| `ruff` + `mypy`（改动文件） | 通过 |

四项数字与交接基线**逐项一致**。ADR 0009 落地前的同口径数字为
1510 passed / 1518 collected，差额是新增的中文用例与豁免测试。

### ⚠️ 已知：全量套件存在**预存 flaky**（与本次改动无关，已证明）

全量套件偶发 1–2 条失败，集中在
`tests/integration/test_ingestion_worker.py`
（`test_a_claimed_version_is_not_claimed_twice` /
`test_an_ingested_document_is_retrievable_by_hybrid_search` /
`test_one_poison_document_does_not_block_the_batch`）
与 `tests/integration/test_billing_ledger.py`
（`test_run_once_without_commit_does_not_persist` /
`test_malformed_payload_fails_rather_than_silently_dropping`）。

**特征**：单独跑全绿、全量跑偶发红、**失败用例每次不同** → 测试间共享状态/顺序依赖。
失败形态 `IngestStats(claimed=1, ready=0, failed=0, deferred=1)` 与 `assert 0 >= 1`
指向 claim-lease 的时间/残留依赖。

**证明与本次改动无关（已执行）**：把 `intent.py` 临时切回 HEAD（我的 196 行改动全部移除）
后连跑三次，仍出现 `1 failed / 2 failed / clean` 交替。
两个失败文件也**都不 import `intent`**。

**未修**——属预存问题，且不属本轮范围。交接时基线记的是 "1488 tests / 0 failures"，
与当前 `1510 passed` 的差异来自本轮新增测试；**由于 flaky 的存在，
"0 failures" 需要跑多次才能观察到**。

### 环境陷阱（本轮踩到，值得记）

- **项目解释器是 `.venv/Scripts/python.exe`**，不是 managed python
  （后者缺 `sqlalchemy`，会以 `ModuleNotFoundError` 假失败）。
- `release_check` 需 `PYTHONPATH` 前缀
  （`apps/api/src;apps/worker/src;packages/policy/src;packages/contracts/src;packages/observability/src;.`），
  裸跑报 `No module named 'platform_core'`。
- `tests/artifacts/release_gate_evidence.json` 会被**任何** pytest 运行覆写。
  单文件跑会把 `tests_collected` 压到个位数并让门禁 **exit 2**。
  **必须全量套件跑完立刻跑门禁**。
- **绝不要在本环境用 `git stash`**：它被 SIGTERM 打断会导致**仓库级损坏**
  （`.git/refs/` 消失、pack 丢失）。已实测发生并恢复，步骤见
  `~/.workbuddy/skills/git-repo-recovery`。临时切版本用 `cp` + `git show`。

## 退役的 expected-failure pin

`test_write_tools.py::test_chinese_case_requests_do_not_reach_the_write_path_at_all`
是我在测量阶段写的**缺口钉子**（断言中文**不**进写路径）。缺口补齐后它必然失败，
已按它 docstring 里预告的方式**翻转**为
`test_chinese_case_requests_reach_the_write_path`。
一个会因修复而变红的测试，是缺口存在过的证据。

## 未决

- 本报告的测量是**我手工构造的华秋话术**，不是客户真实语料。
  在拿到真实中文工单语料前，词表覆盖度只能算"合理起点"而非"已覆盖"。
- 跨语言检索（中文问句 / 英文语料）是独立且更大的问题。dataset 现在用
  `cross_lingual` **显式声明**标注它（不再是 `must_abstain`——那会把能力缺口
  记录成设计意图），但**检索能力本身仍未实现**：两条 `cn-answerable-*`
  用例仍红并登记在 `KNOWN_GAPS`。这需要的是一项新能力（跨语言检索），
  不是一次豁免，ADR 0009 只保证这个缺口**可见**而非**消失**。
- **中文结果补语/趋向补语**（`退掉`/`退回来`/`关掉`/`把订单退掉`）仍未覆盖：
  `_CN_ACTION_VERBS` 只有词干 `退`/`取消`，而 `退` 后接 `掉` 时
  `_CN_REQUEST_FRAME` 的"紧跟中文宾语"条件会被补语吃掉。
  这是**有意划的边界**（补语形态繁多），不是疏漏。
- **`我要投诉` 未被断言为 `human_required`**：英文 `I want to complain` 同样落
  `knowledge_qa`（`complain` 在 `_SCENE_PATTERNS` 里是名词、不在 `ACTION_VERBS` 里）。
  这是**跨语言**缺口，不是中文缺口；在这里断言更强的路由等于把一次更大的改动
  夹带进一次词表修复。记录为未决。
- ~~中文投诉话术的 `scene` 是 `unspecified`（`COMPLAINT` 场景词表同样全英文）。
  影响的是场景亲和度排序，不影响路由，故本轮未动。~~
  **已修（2026-09-20）**：`_SCENE_PATTERNS` 的**每一组**都补了中文备选
  （`\b(?:en)\b|(?:cn)`——CJK 不能用 `\b` 包，这一点本文件第 623 行的注释已经写过）。
  九条中文用例现在都拿到场景（投诉→`complaint`、报错→`technical_support`、
  发票→`billing`、发货→`order_fulfilment`、登录→`account_security`、报价→`pre_sales`），
  英文侧未动、`tests/evals` 仍全绿。
  **同时更正原判断的幅度**：这条不只影响"场景亲和度排序"——
  `orchestrator._top_k_for_scene(detection.scene)` 也吃它，即**检索广度**。
  而试点的客户说中文，所以这个缺口压在每一次真实会话上。
  `human_required` 那半（`我要投诉` 的路由）**仍未动**，见上一条。
- **`scripts/run_eval.py` 的入库停滞是一个独立的预存 bug（新发现，未修）**：
  `RuntimeError: ingesting refund-policy-v3 left ingestion_status='uploaded'
  (stats=IngestStats(claimed=8, ready=8, ...))`，稳定复现，与 ADR 0009 无关
  （该文件在 HEAD 上就是 `batch=10` + "每条目只 drain 一次"的循环，`CORPUS` 也一直是 13 条）。
  根因是**测量脚本与全局 FIFO 认领队列的容量不匹配**：
  `claim_ingestion_versions` 是**全局、跨租户、无过滤**的 FIFO
  （`ORDER BY created_at LIMIT p_batch`，migration 0018 注释明确承认"一个 bulk worker
  服务所有租户"）。确定性实测：播 5 条陈旧 + 13 条新行 → `claim(10)` 返回
  **5 陈旧 + 5 新**，新行 5~12 全被排除；真实运行时插桩也观察到
  `claimable=13` 却只返回 9 行、且**最老那行不在其中**（`FOR UPDATE SKIP LOCKED`
  被锁跳过的特征）。
  即 `batch=10 < 13`，一旦队列头部有其它可认领行（上次中断运行遗留、或并发 worker
  正在处理），目标行在这轮永远轮不到。**修法需你定**：改成"drain 到本条目 ready
  为止"（带上限防死循环），或把 `batch` 提到 ≥ 队列水位。属评测基础设施，
  且另一会话可能正在动它，故未擅自改。

---

# 三、中文读路径补全（2026-09-22）

## 缺口：读路径的入站信号也是英文单语的

第一节的表把 `_LIVE_DATA` / `_CASE_RECORD` 标为「❌ 无」，第二节只补了**写意图**与**人工请求**，
读路径一直没补。实测（2026-09-22）：

| 中文话术 | 修复前 route | 修复前 tools |
|---|---|---|
| 我的订单 SO-9001 到哪了 | `knowledge_qa` | `[]` |
| SO-9001 什么时候发货 | `knowledge_qa` | `[]` |
| 帮我查一下订单 SO-9001 的状态 | `knowledge_qa` | `[]` |
| 订单 SO-9001 现在什么状态 | `knowledge_qa` | `[]` |
| SO-9001 发货了吗 | `knowledge_qa` | `[]` |

对照英文 `What is the status of order SO-9001?` / `Where is my order SO-9001?` →
`business_read` + `order.get_status`。

**代价比"没有卡片"大**：`business_read` 是**进入身份门（功能 2.2/2.5）的唯一路径**。
中文客户问自己的订单时，连"请先证明这单是你的"这句提示都拿不到 ——
整条「验证 → 读取」闭环在**试点客户真正使用的语言**下不可达。
ADR 0006 从另一面说了同一件事：实时数据问题**不该**由语料回答，
所以即使诚实结果是弃答，`knowledge_qa` 也是错的路由。

## 修法：给 `_LIVE_DATA` 补一条中文帧，并配一条**窄**否决守卫

新增 `_CN_LIVE_DATA`（五类帧）与 `_CN_HOW`（否决守卫），调用点排在英文之后
（与第二节同一条纪律：英文信号逐字节不变）：

```python
elif _CN_LIVE_DATA.search(question) and not _CN_HOW.search(question):
```

两个性质保证它不吞掉既有守卫用例保护的政策问句：

1. **每一帧说的是"正在进行的状态"，不是话题名词。** `到哪了` / `什么时候发货` / `发货了吗` /
   `查…状态`。只提名词不算：「PCB 订单的增值税专用发票怎么开？」与「ADS1110 现在有货吗？货期几天？」
   都点了记录，都必须留在知识路径，而两者都不带帧。
2. **ETA 帧里故意没有 `多久` / `几天`。** 它们问的是**流程**耗时 ——
   「退款多久到账？」正是既有守卫用例里的一条，语料答得出 —— 而 `什么时候` 问的是**这一单**。

`_CN_HOW` 是**窄**守卫，不能复用 `_CN_PROCEDURE`：后者含 `什么` / `哪` / `什么时候`，
恰恰就是上面的帧（"现在什么状态"、"到哪了"、"什么时候发货"），复用会把真阳性全部否决。
真正需要拦的是**怎么做**类问句（客户要的是流程，不是自己的记录）：
「怎么查订单状态」命中查找帧，但它是知识问题。

## 结果（实测）

| 项 | 修复前 | 修复后 |
|---|---|---|
| 中文实时数据问句（5 条） | 全部 `knowledge_qa` / `tools=[]` | **全部 `business_read` + `order.get_status`** |
| 中文政策问句（10 条守卫，含既有 5 条） | `knowledge_qa` | **`knowledge_qa`（未退化）** |
| 中文写意图 / 人工请求 | — | 未动（`business_write` / `human_required` 照旧） |
| 评估集 `expected_route` | 27 条 / 0 不符 | **27 条 / 0 不符** |
| `test_intent.py` | 57 | **61** |
| `tests/evals` | 67 | **67 passed** |
| `ruff check` / `ruff format --check` | — | 全绿（424 files formatted） |

## 变异验证 3/3 全被捕获

| 变异 | 移除/改动的东西 | 被哪个测试抓到 |
| --- | --- | --- |
| A | 调用点的 `_CN_HOW` 守卫 | `test_the_how_to_guard_is_the_only_thing_keeping_this_on_the_knowledge_path` |
| B | 往 ETA 帧加 `多久` | `test_the_eta_frame_asks_when_not_how_long` + 既有 `test_chinese_policy_questions_stay_on_the_knowledge_path` |
| C | 整个中文分支 | `test_chinese_live_data_questions_reach_the_read_path` 等 3 条 |

**A 与 B 各配了一对"只差一个词"的用例**（`怎么查订单状态` / `查订单状态`；
`退款多久到账？` / `退款什么时候到账`）—— 第二节写侧变异 A、D 两次漏网的教训是：
守卫用例必须**只有它**能决定。

## 有意划的边界（record, don't smuggle）

- **`_CASE_RECORD` 未补中文**：中文「工单」同时是"建工单"（写）与"我的工单"（读），
  与 `_CN_TICKET_REQUEST` 的写路径相邻，改动面大于本次问题。
- **只加 `什么时候`，不加 `多久` / `几天`**：见上，这是设计不是遗漏。
- **中文实时数据问句仍不进评估门禁**：`expected_route` 断言的是路由契约，
  而中文问句打在英文语料上必然弃答，同一条用例会污染弃答率
  （ADR 0009 已把这类声明收进 `cross_lingual`）。本次断言放在 `test_intent.py`，
  与第二节的处理一致。

## 本次发现的独立问题（未修，不属本次范围）

**CI 的 mypy 门禁在 HEAD 上是红的。** `fb92487` 新增了 `typecheck` job，注释写着
"mypy strict is clean across the source roots"，但实测 `mypy` 报 **9 条错误 / 6 个文件**
（`review_sampling.py`、`csat.py`、`replay.py`、`queue_status.py`、`registry.py`），
且这 5 个文件**都由 `fb92487` 最后修改** —— 即"加门禁的那次提交自己没让门禁变绿"。
本次改动未引入任何一条（改动文件 0 错误）。**未擅自修**：与本次读路径改动无关，
夹带进来会让这次改动无法评价。
