# Round 6 — 收敛修复报告

日期：2026-09-19。规则：一批 ≤3 缺陷；每批改后跑受影响验证；**反向验证强制**（撤回修复确认缺陷重现）；不顺手重构。

## 批次① — 三个页面全部写操作漏 Idempotency-Key（15 个死交互，Round 2 缺陷 6）

**改动**：三个页面的共享 `act()` 把幂等键传给 `apiPost` 第三参；各文件内新增与 Cases.tsx 同款的本地 `idem()`（沿既有文件级模式，不新建共享模块）。
- `apps/admin-web/src/pages/FeatureFlags.tsx:29-33`（act 签名不变，调用点 :63/:117/:143 自动受益）
- `apps/admin-web/src/pages/PromptRelease.tsx`（idem() + apiPost 第三参）
- `apps/admin-web/src/pages/GapQueue.tsx`（同上）

**验证**：tsc 通过；浏览器实测 Define flag（200，旗标出现且可 Enable：`enabled: true`）、Create draft（"Draft created" + 版本列表出现）。
**反向验证**：还原 FeatureFlags 的 `idem()` 传参 → 浏览器再点 Define → 错误横幅原样重现（"every write command must carry an Idempotency-Key header"）→ 重新应用修复。

## 批次② — 工单状态词表前后端脱节（Round 2 缺陷 1）

**改动**：`apps/admin-web/src/pages/Cases.tsx` STATUSES 对齐后端 `CaseStatus` 全枚举（9 态），并注释说明流转合法性仍由服务端裁决（不把 TRANSITIONS 复制进前端）。

**验证**：tsc 通过；浏览器实测 new→triaged 成功（"Command applied"）；非法 triaged→resolved 被服务端以可读文案拒绝（"triaged -> resolved not allowed"）。
**反向验证**：临时还原旧 6 项词表 → "open" 选项重现 → 选择 open 提交 → 原始缺陷重现（"unknown case status: open"，API 确认 status 未变）→ 重新应用修复。

## 批次③ — 成员角色变更/移除传错 ID（Round 2 缺陷 4）

**改动**：
- `apps/api/src/platform_core/identity/router.py`：`MemberOut` 增加 `membership_id`（后端路由本就按 Membership 主键寻址，列表却只给 user_id——契约缺口在响应侧）。
- `apps/admin-web/src/lib/types.ts`：Member 增加 `membership_id`。
- `apps/admin-web/src/pages/Members.tsx`：改角色与移除改用 `m.membership_id` 寻址。

**验证**：identity 单测 110 项全过；浏览器实测改角色（提交保持 support_admin）+ 移除（成员 2→1）成功。
**反向验证（API 级）**：用旧寻址（user_id）POST → 404 重现；新寻址（membership_id）→ 进入角色校验层（400 = 拒绝分配 tenant_owner，服务端守卫正确）。

## 批次④ — 两个后端 500（Round 2 缺陷 2 + Round 4 S1）

**改动 A（assign 超长打崩 DB 层）**：`apps/api/src/platform_core/cases/router.py::_validate_parameters` 对 `assignee_ref`/`team_ref` 增加长度校验（≤255，与列定义一致），非字符串同样拒绝 → 400 带明确文案。
**改动 B（别名 API 出生即死）**——修复过程中揭出**三层语义打架**（这是本批最深的发现）：
1. 路由 upsert 用 `ON CONFLICT ON CONSTRAINT uq_aliases_tenant_alias`，但 0033 建的是 **UNIQUE INDEX** → 任何写入 UndefinedObject 500；
2. ORM `KnowledgeAlias.weight: Mapped[int]`（注释还写着 percent，default=100）vs 迁移 `numeric(4,2) DEFAULT 1.00 CHECK (0,2]`（倍数制）→ int 强转把 1.8 写成 2；
3. GET 返回 `weight/100.0`（percent→倍数换算）——第三种口径。

修法（对齐已部署的迁移真相=倍数制，免迁移）：
- `knowledge/router.py` upsert 改 `index_elements=["tenant_id","alias"]`（对唯一索引与约束双兼容）；
- `knowledge/models.py` weight 改 `Mapped[float] = mapped_column(Numeric(4,2), default=1.0)`（镜像迁移）；
- `knowledge/router.py` AliasIn 改 `weight: float = Field(default=1.0, gt=0, le=2)`，GET 原样返回倍数。

**验证**：ruff/mypy 全绿；实测 create(1.5)→ok、upsert 同名 alias(1.8)→ok（单行更新）、越界 3.0→422、list 精确回显 1.8（强转消失）；assign 5000 字符 → 400（此前 500）。
**反向验证（别名）**：临时还原 `constraint=` 写法 → 重启 → 500 重现 → 重新应用修复。（assign 的反向 = Round 2/4 的 500 实录，与修复后 400 对照。）

## 批次⑤ — GapQueue "[object Object]"（Round 2 缺陷 7）

**改动**：`lib/types.ts` 的 `GapStats` 从错误的 `Record<string, number>` 改为真实形状（by_status 嵌套 + 两个总数）；`GapQueue.tsx` 渲染改为 Total Gaps / Total Occurrences 两张卡 + by_status 逐状态卡片。

**验证**：tsc 通过；浏览器实测 "[object Object]" 消失，总数字段正常渲染，空 by_status 不产生多余卡片。
**反向验证**：修复前渲染（整包 entries）在 Round 2 已留实证；类型错误与渲染错的因果由 tsc+DOM 前后对照锁定。

## 全量回归

| 检查 | 结果 |
|---|---|
| pytest 单元套件（apps/api/tests + packages/contracts/tests + tests/evals，-m "not integration"） | **804 passed，exit 0**（docs/acceptance/pytest-unit.log） |
| ruff check + format --check（4 个改动后端文件） | 全绿 |
| mypy --strict（4 个改动后端文件） | 0 错 |
| 前端 tsc --noEmit + vite build | 通过（253KB / 79.14KB gzip） |
| 受影响集成面 | 以活 API 探针覆盖（成员/别名/cases 命令），未跑容器集成套件——本机 worker 会与套件竞抢队列行（overview.md 已有此警告），如实标注 |

## 发现但**不**在本轮修的（按纪律入册）

1. **P2** `tests/e2e/e2e_chatwoot_loop.py` 清理逻辑不认识迁移 0034 新表 `conversation_turns` → 删租户时 FK 崩溃、留孤儿租户行（Round 3 发现）。
2. **P2** 文档上传失败的恢复路径（canonical_uri 烧毁 + IntegrityError 误报 STORAGE_UNAVAILABLE + 无删除端点）（Round 3 发现）。
3. **P2** 知识空间无创建 API/页面（Round 2 发现）。
4. **P2** Quality 零运行时 Citation coverage 0/0 显示 100%；401 冗余横幅；禁用按钮无解释；侧栏移动端不折叠；422 先于 403（Round 4/5）。
5. **P2** 邀请邮箱无格式校验（Round 2 缺陷 3）——涉及数据兼容决策（存量行），留给数据面专项。
6. 观察项：FeatureFlags/GapQueue/Members 多数写操作成功时无显式 success 文案（仅状态刷新），与 Cases/Usage 的成功横幅不一致——一致性小项。

## 测试数据清理说明

- admin-demo 租户内的验收数据（cases×4、1 个 alias、1 个文档版本、flag `acceptance.probe.flag`、prompt draft）保留可查——它们同时是修复的活证据；账本净额已用反向更正归零。
- 两个 e2e 孤儿租户（e2e-chatwoot-*）因脚本自身清理缺陷（上述第 1 条）留存，需脚本修复后清理。
