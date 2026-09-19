# Round 2 — 真链路走查 + 假按钮专项

日期：2026-09-19。对象：admin-web（dev server :5173 → API :8000），租户 admin-demo（tenant_owner）。
方法：真实浏览器（Chromium，Browser Use）。**环境限制如实声明**：本会话 IAB 输入管线对真实鼠标/键盘事件全部失效（导航链接也点不动，`tab.back()`/fill/evaluate 正常）——已用「页面内 evaluate 触发点击 + 真实 fill/导航/快照」替代，所有断言基于真实 DOM/网络/存储反馈；这不是产品缺陷的来源（同一函数在 API 层可直接复现）。

## 核心任务结论

**运维者核心旅程无法端到端完成。** 能完成：接入令牌 → 看质量看板 → 查工单（首次响应/改优先级/new→closed）→ 邀请成员 → 配额管理 → 品牌管理。
**卡死步骤**（全部有明确卡点证据）：定义功能开关（卡在 400）、Prompt 发布全流程（卡在 400）、缺口队列处理（卡在 400）、成员改角色/移除（卡在 404）。
**客户→AI 回答闭环**：UI 无入口（无知识上传页、无对话视图），属产品范围缺口；后端闭环在 Round 3 以 API 验证。

## 逐交互点结果表

| 位置 | 操作 | 期望 | 实际 | 阻塞? | 证据 |
|---|---|---|---|---|---|
| TokenDialog | 无 token 首次打开 | 弹出 | ✅ 弹出 | 否 | DOM group "Access token" |
| TokenDialog | 空值提交 | 内联报错 | ✅ "Paste the access token issued for your account." | 否 | .prompt-error |
| TokenDialog | 无效 token | 报错且不保存 | ✅ "HTTP 401"，localStorage 保持 | 否 | evaluate localStorage |
| TokenDialog | 有效 token | 保存+重载 | ✅ dialog 关闭、token=present、页面已登录 | 否 | localStorage + 重载 |
| Layout | Token 按钮 | 重开弹窗 | ✅ | 否 | DOM |
| Quality | 1h/24h/30d 切换 | 数据刷新 | ✅（零态渲染+解释文案） | 否 | main text |
| Cases | 列表/详情/SLA | 展示 | ✅（空态 "No cases yet."；详情时钟齐全） | 否 | DOM |
| Cases | Record first response | 成功反馈 | ✅ `Command "record_first_response" applied.` | 否 | role=status |
| Cases | Transition→open | 流转 | ❌ 红色横幅 "unknown case status: open"，API 确认 status 仍 new | **是** | 见缺陷 1 |
| Cases | Transition→closed | 流转 | ✅ `Command "transition" applied.` | 否 | role=status |
| Cases | Change priority→p1 | 成功 | ✅ | 否 | role=status |
| Cases | Assign 正常/空参 | 成功/清晰 400 | ✅ 正常 200；空参 400 VALIDATION_FAILED | 否 | API |
| Cases | Assign 5000 字符 | 4xx 校验 | ❌ **500 裸文本 "Internal Server Error"**（无信封无 trace_id） | **是** | 见缺陷 2 |
| Cases | XSS/unicode assign | 安全存储 | ✅ 存储后转义渲染，无脚本执行 | 否 | DOM 无 script |
| Members | 邀请（正常邮箱） | 令牌展示+复制 | ✅ "invite created"+token+Copy | 否 | .serving |
| Members | 邀请 "not-an-email" | 拒绝 | ❌ 接受并签发令牌 | 否（安全向） | 见缺陷 3 |
| Members | 改角色→确认 | 成功 | ❌ "membership not found"（404），选择回弹 | **是** | 见缺陷 4 |
| Members | Remove→确认 | 成功 | ❌ 同上 404 | **是** | 缺陷 4 |
| Members | owner 行 Remove | 不可移除 | ✅ 按钮禁用（无解释文案，P2） | 否 | disabled 属性 |
| Usage | 配额空值 | 语义明确 | ✅ "Quota cleared — this tenant is now unlimited." | 否 | role=status |
| Usage | 配额 abc / -5 | 拒绝 | ✅ "Quota must be a whole number of runs, or empty for unlimited." | 否 | role=alert |
| Usage | 配额 100 | 成功 | ✅ "Quota set to 100 runs per calendar month." | 否 | role=status |
| Usage | 更正零增量 | 拒绝 | ✅ "A correction must change at least one token count." | 否 | role=alert |
| Usage | 更正伪造 run_id | 拒绝或明确语义 | ❓ **"Correction recorded."**——不校验 run 存在性（疑似，需确认） | 否 | 见缺陷 5 |
| Branding | 正常保存 | 成功 | ✅ "Branding saved." | 否 | role=status |
| Branding | logo_url=javascript: | 拒绝 | ✅ "logo_url must be an http(s) URL" | 否 | role=alert |
| Branding | display_name=XSS | 惰性渲染 | ✅ 文本转义，0 个注入 img | 否 | DOM |
| Flags | Define（任意 key） | 创建 | ❌ **全部失败**：400 IDEMPOTENCY_KEY_REQUIRED | **是** | 缺陷 6 |
| Flags | Enable/Disable、Set rollout | 生效 | ❌ 同上（全部写路径死） | **是** | 缺陷 6 |
| Prompts | Create draft | 创建 | ❌ 同 400（实测复现） | **是** | 缺陷 6 |
| Prompts | Candidate/Promote/Reject/Rollback | 生效 | ❌ 同 400（同一 act() 无键） | **是** | 缺陷 6 |
| Gaps | Claim/Dismiss/Draft/Approve/Reject/Publish | 生效 | ❌ 同 400（同一 act() 无键） | **是** | 缺陷 6 |
| Gaps | 统计卡片渲染 | 数字 | ❌ **"[object Object]" 直出**（"By Status" 卡片） | **是** | 缺陷 7 |
| Gaps | Tabs/状态筛选/空态 | 正常 | ✅ "No gaps in this state." | 否 | DOM |
| 全站 | Retry 按钮 | 重载 | ✅（useAsync.reload 接线） | 否 | 代码+401 场景 |

## 缺陷清单

**缺陷 1（P1）工单状态词表前后端不匹配**：前端 `STATUSES=["new","open","pending_customer","escalated","resolved","closed"]`（Cases.tsx:20-26），后端枚举 `{new,triaged,in_progress,waiting_customer,waiting_internal,waiting_vendor,resolved,closed,reopened}`（cases/models.py:30-39，TRANSITIONS :43-61）。UI 提供的 6 项中 3 项后端不存在；后端 9 态中 4 项 UI 无法到达。从 new 出发 UI 唯一合法目标是 closed——**new→triaged→in_progress 主工作流 UI 走不通**。
**缺陷 2（P1）超长输入打崩服务端**：`assign` 的 assignee_ref=5000 字符 → 500 裸文本（无 JSON 信封），绕过全部校验直达数据库层未处理异常。500 字符以下正常；违反 api-contracts「Errors use stable machine-readable codes」。
**缺陷 3（P2）邀请不校验邮箱格式**："not-an-email" 被接受并签发单次令牌（identity/router.py invite，AcceptInviteIn 用 str 而非 EmailStr）。
**缺陷 4（P1）成员角色变更/移除传错 ID**：前端传 `m.user_id`（Members.tsx:79,91），后端按 Membership 主键查（identity/router.py:458-461）→ 404 "membership not found"。成员管理 3 项操作中 2 项永久失败。
**缺陷 5（P2，疑似需确认）账本更正不校验 run 存在性**：伪造 run_id 的更正被接受（"Correction recorded."）；owner-only+审计在，但账本可引用不存在的 run。测试数据已用反向更正清零（净额 0）。
**缺陷 6（P1）三个页面全部写操作漏 Idempotency-Key**：共享 helper `act()` 调 `apiPost(path, body)` 不传第三个参数 —— FeatureFlags.tsx:29-32（:63/:117/:143 三处调用）、PromptRelease.tsx:38-44（:167/:180/:194/:227）、GapQueue.tsx:65（:158/:172/:190/:249/:268/:301）。后端一律 400 IDEMPOTENCY_KEY_REQUIRED。**15 个写交互全死**。对照组：Cases/Members/Usage/Branding 的写调用显式传键（正常工作）。
**缺陷 7（P1）"[object Object]" 直出**：`/v1/knowledge/gaps/stats` 返回 `by_status:{}`（对象），前端 `Object.entries(stats.data).map(...int(v))`（GapQueue.tsx:99-106）把嵌套对象喂给 `int()` → 渲染 "[object Object]"。

## 其他观察（P2，详见 Round 5）
- 401 时错误横幅与 TokenDialog 同时出现，横幅文案 "HTTP 401" 技术性且冗余。
- Quality 零运行时 "Citation coverage 100.0%"（0/0 显示为 100%，误导）。
- Cases 空态无引导动作；UI 无创建工单、无知识上传入口（这两项仅 API 可达）。
- 邀请接收（accept）无 UI 页面，受邀者必须用原始 HTTP 调用。
- 375px 视口无横向滚动但侧栏不折叠，内容区被挤压。

## 假按钮专项（代码侧 × 运行态交叉比对）

代码侧（Round 1 全量 grep + handler 追踪）：所有 onClick 均可追到 API/状态/导航终点；无空函数、无 console.log-only、无 TODO 占位、无 div 冒充按钮。
运行态：**没有发现「点了完全没反应」的真死按钮**——每个交互要么成功反馈、要么错误横幅、要么原生 disabled。假交互的真面目是「必然报错的按钮」（缺陷 6 的 15 个 + 缺陷 4 的 2 个）与「报错后用户无法继续」（缺陷 1）。
键盘可达性：全部交互元素为原生 button/a/input/select（静态判定可达）；本会话键盘事件受输入管线限制未做走查。移动端 375px 可用但侧栏不折叠。
