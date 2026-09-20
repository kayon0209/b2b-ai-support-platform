# 交接指令（给接手的 agent）

你要接手一个 B2B 企业 AI 客服平台的开发。仓库根目录：

```
D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan
```

**你的第一件事不是写代码，是按下面的顺序读完上下文。** 这个仓库有大量"看起来在跑、
实际上打不到"的陷阱，以及一份已经被验证过的项目计划；不看就动手，你会重复别人
（包括我）踩过的坑。

---

## 1. 先读这些（按顺序，约 20 分钟）

1. `AGENTS.md` —— 仓库铁律（10 条架构规则 + 完成定义 + 禁止的走捷径）。**这是最高约束。**
2. `.workbuddy-ai/memory/MEMORY.md` —— 长期项目记忆（环境、验证命令、发布门禁、
   反复出现的缺陷族）。**这是最省时间的一份。**
3. `.workbuddy-ai/memory/REFERENCE.md` —— 长尾细节（含 Windows 环境陷阱、浏览器验收方法）。
4. `.workbuddy-ai/memory/2026-09-19.md` —— 当天工作日志，记录了这一轮所有判断的依据与错误。
5. `docs/research/huaqiu-research.md` —— **项目计划本体**：华秋（客户）业务调研、
   六场景、可控性分层、阶段 0–4 上线节奏、指标与风险登记册，
   以及附录 A–G（实施进展 + 每一轮的落地记录与**被推翻的设计**）。
6. `docs/agent.md`、`docs/domain-model.md`、`docs/api-contracts.md` —— 三份契约文档。

**不要删 `.workbuddy-ai/`**：它不是缓存，是项目数据与记忆。

---

## 2. 当前状态（事实，不是估计）

| 项 | 值 |
|---|---|
| 计划阶段 0 / 1 / 2 | **已完成** |
| 计划阶段 3 / 4 | **未开始**（阶段 3 按计划是 +6–8 周的量） |
| 全量测试 | **1488 收集 / 0 失败 / 0 错误 / 8 跳过**（本轮最干净的一次） |
| ruff / ruff format / mypy | 全绿（317 文件 / 139 源文件） |
| `release_check --evidence-only` | exit 0，零容忍计数 15 / 4 / 3 |
| 管理台浏览器验收 | `.workbuddy-ai/acceptance/approvals.mjs`，**36 项断言全过** |
| 改动文件 | **54 个，全部未提交** |
| 迁移 | 最新 `0038_eq_confirm_human_approval`，`EXPECTED_MIGRATIONS = 38` |

**54 个改动文件里混着三个会话的工作**（我的、zcode 的、另一个会话的）。
**不要整体 commit，也不要 reset**——拆 commit 需要人来定，先问。

阶段 2 的最终形态：客户在会话里说"确认" → 智能体**识别并转人工**
（`EQ_CONFIRMATION_REQUIRES_HUMAN`）→ `tenant_owner` 在管理台记录确认。
**智能体不提议这个工具**，因为 `case.eq_confirm` 是 `human_approval`——
这是计划的要求，不是我加的保守。

---

## 3. 这个仓库的工作纪律（血泪换来的，请照做）

### 3.1 这个仓库的标志性缺陷是"有读无写"

已发现十几处"被声明、被测试、但生产路径永远打不到"的东西：
`AgentRun.started_at`、`token_usage`、`SyncCursor`、`CaseConversation`（有读者无写者，
导致优先认领一直空转）、`expected_route`（默认值说谎、无人读）、
`Route.CASE_STATUS`（文档有、分类器不产出）……

**规矩：任何字段/函数/枚举，先 grep 它的写入者和调用者。**
一个直接调用它的测试会掩盖"生产里没有调用者"。

### 3.2 改启发式之前，先测量，再双向加守卫

改分类器（`ACTION_VERBS`、路由、正则）的失败模式是**某条问题悄悄换了路由**。

1. 用 `.workbuddy-ai/acceptance/measure_action_verbs.py` 对**全量数据集**测量改动的影响；
2. **正向 + 反向都加守卫用例**——语料里没有的句式，"没移动"只是弱证据；
3. 反向守卫经常当场抓到真实假阳性（我这轮就抓到一个早于改动就存在的）。

### 3.3 无法观察到失败的守卫不构成证据

每加一条守卫，**做一次变异验证**：故意破坏被守护的行为，确认测试真的红。
本轮所有守卫都做过，报告里都写了"变异验证：……"。

### 3.4 过宽的守卫比没有守卫更糟

我第一次写"两个测试文件不得共用 tenant id"，扫出 14 处共用——**说明共用是常态**，
真正致命的是"共用 id 且 slug 不同"。**收窄到真正的不变量**，否则 CI 永久变红。

### 3.5 当"这样更方便"与"文档/策略明确要求"冲突时，**先假设文档是对的**

**这是我这一轮犯的最大的错。** 计划五处写明 EQ 确认必须是 `HUMAN_APPROVAL`，
`packages/policy/engine.py` 也写明该类"must be unreachable by the agent at every
stage, propose included"；我却因为"那样智能体就够不到了"改成了 `confirmed_write`，
还基于它建了一整套智能体提议逻辑。**智能体够不到是设计目的，不是缺陷。**
后来全部改回，并补了迁移（见 3.6）。

**可达性是便利论证，不是安全论证。动手前先读计划给的理由。**

### 3.6 改工具风险级必须配数据迁移

`ensure_tool_definitions` **只增不改**（刻意：租户可能自己改过定义），
而网关读的是 `tool_definitions` 行里的 `risk`。
所以**改 `TOOL_CATALOG` 里的风险级只对新租户生效**——不迁移，严格等级只是一句注释。
参考 `apps/api/migrations/versions/0038_eq_confirm_human_approval.py`。

### 3.7 测试夹具：清理漏子表会毒害下一次运行

判据：**单跑也失败 = 数据库脏；单跑通过 = 需要进一步归因**。
删父表前先删子表（`chunks` → `document_versions`、`case_conversations` → `cases`）。
本轮三个不同的文件犯过这个错（包括我自己新写的测试）。

### 3.8 不留死代码

我撤掉一个设计时，把连带的选择器参数、参数提取分支、透传参数**全部删掉**。
留"以后可能用得上"的代码，就是给下一个人制造"有读无写"。

### 3.9 真 4xx，不要 200 带错误体

FastAPI 把返回的 **dict** 渲染成 200。域拒绝必须返回
`error_response()` / `domain_error_response()`（`JSONResponse`）。
症状：UI 说"成功"但什么都没变 → `curl -i` 对比。

---

## 4. 环境陷阱（每一个都真实浪费过时间）

### 4.1 端口：旧进程杀不掉，而日志会说"启动成功"

- **旧进程绑 `127.0.0.1:PORT`、新进程绑 `0.0.0.0:PORT` 会同时"成功"**，
  但 loopback 流量走更具体的绑定 → 打到**旧代码**上，
  于是新加的端点回 405/404，看起来像路由没注册。
- `taskkill /F /PID` **不一定能杀掉**（可能是别人的进程）。
- **新实例 bind 失败时，日志前面已经打印过 "Application startup complete"**——
  只看到那句会以为启动成功。
- **判断依据永远是 `netstat -ano` 的绑定地址 + PID**；冲突就换端口（我用过 8021→8022）。

### 4.2 可能有宿主机 worker 在跑，容器全是 Exited

它会抢全局 inbox/outbox 队列，导致 `test_billing_ledger` / `test_inbox_reclaim` /
`test_ingestion_worker` / `test_connector_webhook` 间歇失败，
以及 `test_knowledge_gaps` 的 `chunks` 外键错误（worker 在处理 ingestion，
异步插入 chunks 与测试清理竞争）。

**探针，不要猜**：插一条 `queued` 的 outbox 行，几秒后回读——
`attempts` 涨了就说明有消费者活着（我测到 13 秒内 0 → 4724，状态仍是 `queued`）。
用完删掉探针。同时看 `pg_stat_activity` 有没有 `idle in transaction`。

### 4.3 curl 与本地调用

- **一律加 `--noproxy '*'`**，否则环境代理会回 502。
- **输出写文件（`-o`），不要接管道**：`head -c` 提前关闭管道会让 curl exit 23，body 丢空。

### 4.4 发布门禁证据：全量测试必须是最后一次 pytest

`pytest_plugins_release/gate_evidence.py` **无条件写**证据文件，
所以任何**定向** pytest 都会用部分证据覆盖它 → `release_check` exit 2。

- 跑全量前先 `mv tests/artifacts/release_gate_evidence.json /tmp/`（否则
  `pytest_configure` 的 `unlink()` 会触发沙箱批量删除守卫，抛 `SystemExit`，
  而插件只捕 `OSError`，会话在收集前就死）。
- **不要改那个插件，它写得是对的。**

### 4.5 pytest 其他

- summary 走 **stderr**；管道后取 `${PIPESTATUS[0]}`。
- 用 `--junitxml` 拿权威计数（沙箱守卫会截断 summary）。
- `pyproject.toml` 的 `testpaths` 已修正为覆盖两个测试根。

### 4.6 迁移

```bash
cd apps/api/migrations
APP_ALLOW_BOOTSTRAP_TOKENS=true ../../.venv/Scripts/python.exe -m alembic -c alembic.ini upgrade head
```
漏了 `-c alembic.ini` 会报 `No 'script_location' key found`；漏了环境变量会报
`no authentication configured`。加迁移要同步 bump
`apps/api/tests/integration/test_migration_and_performance.py` 的 `EXPECTED_MIGRATIONS`。

### 4.7 起 API：绝不用裸 uvicorn

**必须 `python -m platform_core.main`。** 裸 uvicorn 硬编码 `ProactorEventLoop`，
psycopg async 会拒绝 → 每个 DB 请求都在 connect 处死掉，
而中间件把它渲染成光秃秃的 `401 AUTH_UNRESOLVED`。
任何碰 DB 的脚本需要 `asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)`。

### 4.8 浏览器验收

`agent-browser` 不支持 Windows。用系统 Chrome + `playwright-core`：

- `playwright-core` 装在 `~/.workbuddy-ai/binaries/node/workspace/node_modules`，
  而 **ESM 不认 `NODE_PATH`** → 把 `.mjs` 拷到该 workspace 的 `acceptance/` 下再跑。
- 长脚本**后台跑 + 重定向到文件**，前台会被 SIGTERM。
- **Chrome 的 `innerText` 返回渲染后文本**：`.section-title` 有 `text-transform: uppercase`，
  断言要忽略大小写。
- 故意触发的 4xx/5xx 会被记为 console error，统计页面错误时要过滤。
- **断言要限定作用域**：页面级的 banner 可能是上一步留下的陈旧失败，
  我用面板内 banner 断言失败才暴露出一个真实 UI 缺陷。
- 验收脚本必须**自己造夹具**（提议 15 分钟过期；固定引用第二次运行会因歧义被拒）。

---

## 5. 不要擅自决定的事（留给人类）

1. **`Route.CASE_STATUS`**：文档有、分类器从不产出（case 状态问题走
   `BUSINESS_READ` + `case.read`）。两种解法——**产出它**（我倾向：`BUSINESS_READ`
   文档定义是"查外部系统"，而 `case.read` 读平台自己的表，失败模式不同，
   `observability_metrics` 的白名单也已预留 `case_status`）或**从词汇表删掉它**。
   `docs/agent.md` 里我已明确标注，没有改分类器。
2. **宿主机那个 worker 进程**：不是我的，杀它需要人确认。
3. **54 个改动文件怎么拆 commit**。

---

## 6. 建议的下一步

计划的阶段 3 第一块：**`case.create`**（质量投诉半自动化，
计划原文"`case.create` + 证据附件（MinIO 预签名 URL）→ 人工裁定"）。

**但先做 3.5 那件事**：读计划对它的要求。计划的风险分级清单里
`HUMAN_APPROVAL` 只列了"EQ 放行、赔付、退款"，创建工单不在其中，
所以 `confirmed_write`（智能体提议、坐席按冻结参数批准）是对得上的——
**但这是你的判断，请自己核对一遍再动手。**

建议顺序：先写设计理由（为什么是这个风险级、谁提议谁批准、参数从哪来），
再写代码；每一步加守卫并做变异验证。

---

## 7. 验证命令（照抄）

```bash
cd "D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan"
R="$(cygpath -w "$(pwd)")"
export PYTHONPATH="$R/apps/api/src;$R/packages/contracts/src;$R/packages/policy/src;$R/packages/observability/src;$R/apps/worker/src"

./.venv/Scripts/python.exe -m ruff check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m ruff format --check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m mypy

# 全量必须是最后一次 pytest（先移走证据文件）
mv tests/artifacts/release_gate_evidence.json /tmp/evidence.bak 2>/dev/null
./.venv/Scripts/python.exe -m pytest --junitxml=tests/artifacts/junit-final.xml
./.venv/Scripts/python.exe -m platform_core.evaluation.release_check --evidence-only
```

`PYTHONPATH` 必须是**绝对路径且用 `;` 连接**（Git Bash 里用 `cygpath -w`），
否则报 `No module named 'platform_core'`。
