# ADR 0009 · 弃答率的适用域：把「语料语言不可达」从 false abstention 中分离

- 状态：提议（待评审）
- 日期：2026-09-19
- 关联：附录 I（中文意图分类）、`docs/research/chinese-intent-measurement.md` 第 5 节
- 影响面：`apps/api/src/platform_core/evaluation/runner.py`、`tests/evals/`

---

## 1. 问题

`abstention_correct_rate` 用一条布尔式判定"这条用例的弃答行为对不对"：

```python
# runner.py:288-297（现状）
abstention_correct = sum(
    1 for r, c in zip(results, cases)
    if (c.must_abstain and r.abstained) or (not c.must_abstain and not r.abstained)
)
abstention_false = sum(
    1 for r, c in zip(results, cases)
    if (c.must_abstain and not r.abstained) or (not c.must_abstain and r.abstained)
)
```

即：**可答用例一旦弃答 = `abstention_false`**。

这个定义在一个前提下才是对的——**"可答"意味着语料里有能回答它的内容，
且检索路径能够到达**。中文问句打在英文语料上违反的是后半句：
语料内容确实存在（英文版），但**检索因语言不匹配而无法到达**。

于是把中文路由用例放进 dataset 时，门禁报
`abstention_correct_rate: 0.8293 vs 0.9`。**这 7 条失败全部是
`abstained=True` + `NO_AUTHORIZED_EVIDENCE`**，不是模型答错了。

## 2. 为什么不能简单地把它们排除掉

三条"偷懒解"都不成立：

| 解法 | 为什么不成立 |
|---|---|
| 把它们标成 `must_abstain=True` | **语义撒谎**：中文"退款政策是什么"是**可答**的，只是语料没有中文版。标成"应当弃答"等于把检索能力缺陷写成设计意图。而且会让中文检索缺口**永远不被测量** |
| 加进 `KNOWN_GAPS` 白名单 | 门禁的豁免口一旦为"已知缺陷"打开，就再也不会关上。`KNOWN_GAPS` 现在**是空的**，这是资产不是巧合 |
| 干脆不放中文用例进 dataset | 等于承认中文路由契约不需要门禁保护——但它正是本轮**安全级修复**的契约 |

## 3. 决策

**给 `abstention_correct_rate` 划一个显式的、可审计的适用域**：
弃答**由什么原因产生**决定它是否计入分母。

新增 `CaseResult.abstention_attributable: bool`（默认 `True`）。

弃答**不计入** `abstention_correct_rate` 的分母，**当且仅当**下面两条同时成立：

1. 弃答原因是 **语言不可达**（`NO_AUTHORIZED_EVIDENCE`）；**且**
2. 该用例**被显式标注**为 `cross_lingual=True`。

两条都必须满足。原因码单独不够（`NO_AUTHORIZED_EVIDENCE` 也可能因为语料真的没有），
标注单独也不够（标注自己不携带原因）。

### 3.1 关键约束：它不是"免死金牌"

被排除的用例**不是被忽略**——它们仍然：

- 计入 `total` / `passed` / `failed`（**失败仍然失败**，仍会让套件变红）；
- 仍受 `expected_route` 契约断言（路由错了照样 `ROUTE_MISMATCH`）；
- 仍受 `citation_violations` / `forbidden_claim_hits` 约束；
- **另外**累加进一个新的显式指标 `cross_lingual_unreachable`，**并被断言不为零**。

最后一条是关键：**如果一个缺口被排除在分母外，就必须有一个数专门记住它存在。**
否则"排除"就退化成"隐藏"，而门禁最该防的就是这个。

### 3.2 为什么默认是 `True`（fail-closed）

`abstention_attributable` 默认 `True`，意味着**没有任何标注的弃答照旧计入分母**。
新的豁免必须由用例**主动声明** `cross_lingual=True`。

这个默认值方向是刻意的：**漏标不会放松门禁，只会让门禁继续按旧规矩严格计数。**
反之如果默认 `False`，任何新增用例都会自动获得豁免——那是 fail-open，不可接受。

## 4. 与"不要擅自决定"的关系

交接文档列的三个不擅自决定项里，没有这一条；但它属于同类（门禁语义变更）。
因此本 ADR 的定位是：**给出最小、可审计、fail-closed 的改法 + 完整的取舍记录**，
而不是悄悄把阈值调松。

**明确拒绝的替代方案**：

| 方案 | 拒绝理由 |
|---|---|
| 调低 `min_abstention_correct_rate` | 这是**全局**放松：它同时放松了敏感请求、越权提问、注入攻击等全部弃答契约。用全局阈值去补一个局部适用域错误，是拿最重要的安全门去换一个语言问题 |
| 让检索支持跨语言（翻译查询） | **正确但更大**：属于新基础设施能力（要 ADR + benchmark），不是本轮范围。而且它**不解决**"弃答率口径"这个度量问题——即使翻译做好了，语料真的没有的内容仍会弃答 |
| 每个用例单独配 `min_abstention` | 过度拟合。适用域应当由**原因**决定，而不是由用例逐个投票决定 |

## 5. 实施要点

1. `EvalCase` 增加 `cross_lingual: bool = False`；
2. `CaseResult` 增加 `abstention_attributable: bool = True`；
3. `run_case` 在弃答分支设置它：仅当 `case.cross_lingual and reason_code == NO_AUTHORIZED_EVIDENCE` 时为 `False`；
4. `_summarise` 的分母过滤加 `r.abstention_attributable`；
5. `EvalReport` 增加 `cross_lingual_unreachable: int`（计数**所有**被排除的用例）；
6. 新增门禁 `cross_lingual_unreachable`：**要求它等于被标注的用例数**，
   防止"标注了但原因码不匹配"的静默失效；
7. 中文用例（写意图 6 条 + 政策守卫 6 条）以 `cross_lingual=True` 进 dataset，
   路由断言恢复由门禁覆盖——**这正是本次修复要拿回的东西**。

## 6. 验证要求

- 变异验证：把 `abstention_attributable` 的判定反转 / 移除标注 / 移除原因码条件，
  必须分别被**正确的**测试捕获；
- 全量套件 + `release_check --evidence-only` 必须与交接基线四项数字**逐项一致**
  （cross-tenant 15 / unauthorized-writes 4 / duplicate-replies 3）；
- **反向验证**：确认一个真实的 false abstention（非语言原因）**仍然**让门禁变红。
  这一条比正向验证更重要——它证明豁免没有溢出适用范围。
