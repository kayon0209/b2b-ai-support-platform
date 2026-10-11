import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet, apiPost } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useLang, type Lang } from "../lib/i18n";
import {
  allCollected as everyFieldFilled,
  collectKey,
  collectedFor,
  mayApply,
  nextSeq,
  type RequestSeq,
} from "../lib/taskPanelState";

/**
 * The conversation task panel (T07, R1).
 *
 * What this panel is careful about, and why each rule is here rather than in
 * the server's response:
 *
 * - **A blocked task says why, in words.** `needs_human` with no explanation
 *   is not actionable; an agent looking at "unsupported" with no reason cannot
 *   tell whether to escalate, to collect something, or to give up. The reason
 *   code is rendered as a sentence, not as a code.
 * - **Nothing here claims a business action happened.** A `succeeded` task is
 *   labelled as verified, and everything else is labelled as what it actually
 *   is. There is no code path in this file that renders "done" for a status
 *   the server did not report.
 * - **Model output is labelled 建议 (suggestion), tool output 已核验
 *   (verified).** The distinction is the whole point of the feature, and a
 *   grey box that looks the same in both cases would erase it.
 * - **A disabled button always carries its reason.** UX-02: a control the
 *   operator cannot use, with no explanation, reads as a broken page.
 * - **The task list is loaded from the server on every conversation change
 *   and on demand.** Nothing is derived from the customer's text locally, so
 *   a stale client cannot invent a task the server never recorded.
 */

export interface TaskSlot {
  name: string;
  origin:
    | "customer_stated"
    | "verified_receipt"
    | "verified_business_record"
    | "server_capability"
    | "inferred"
    | "agent_collected";
  confirmed: boolean;
  value?: unknown;
  value_withheld?: boolean;
  inferred?: boolean;
  verification_source?: string;
  selection_source?: string;
  authority_version?: string;
  collected_by?: string;
  collected_at?: number;
}

export interface ConversationTask {
  task_id: string;
  local_key: string;
  kind: "read" | "write" | "clarify";
  status:
    | "proposed"
    | "awaiting_input"
    | "ready"
    | "needs_human"
    | "awaiting_confirmation"
    | "executing"
    | "succeeded"
    | "failed"
    | "unknown"
    | "cancelled"
    | "manual_flow";
  sequence: number;
  version: number;
  action_revision: number;
  slots: TaskSlot[];
  missing_slots: string[];
  depends_on: string[];
  condition: { field: string; operator: string; value: unknown } | null;
  blocked_reason: string | null;
  dependency_blocked?: boolean;
  proposal_id: string | null;
  execution_id: string | null;
  source_turn_id: string;
  flow_key: string | null;
  flow_version: number | null;
  flow_title: string | null;
  flow_can_prepare_proposal: boolean;
  flow_can_query_order: boolean;
  updated_at: number;
}

interface TasksResponse {
  conversation_ref: string;
  items: ConversationTask[];
  limit: number;
  offset: number;
  demo_presales_enabled: boolean;
}

interface DemoPreSalesEvidence {
  product_ref: string;
  product_name: string;
  revision: string;
  product_kind: string;
  specifications: Record<string, string | number | boolean>;
  available_quantity: string;
  unit_code: string;
  warehouse_ref: string;
  indicative_unit_price_minor: number;
  currency: string;
  minimum_quantity: string;
  lead_time_business_days: number | null;
  quote_valid_until: string;
  product_source_version: string;
  inventory_source_version: string;
  quote_source_version: string;
  synthetic: true;
  customer_quote_allowed: false;
  handoff_required: true;
}

export type TaskCommand =
  | "collect_fields"
  | "execute_read"
  | "cancel"
  | "handoff"
  | "prepare_proposal"
  | "query_order_status";

interface DemoOrderStatusReceipt {
  order_id: string;
  status: string;
  nodes: { label: string; status: string; at: string | null }[];
  eta: string | null;
  source: "demo";
  fetched_at: string;
}

function asDemoOrderStatusReceipt(value: unknown): DemoOrderStatusReceipt | null {
  if (!value || typeof value !== "object") return null;
  const candidate = value as Partial<DemoOrderStatusReceipt>;
  if (
    typeof candidate.order_id !== "string" ||
    typeof candidate.status !== "string" ||
    candidate.source !== "demo" ||
    typeof candidate.fetched_at !== "string" ||
    !Array.isArray(candidate.nodes)
  ) {
    return null;
  }
  return candidate as DemoOrderStatusReceipt;
}

/** Status → label. A closed vocabulary: an unknown status renders as itself. */
const STATUS_LABEL: Record<string, string> = {
  proposed: "待确认需求",
  awaiting_input: "等待客户补充",
  ready: "可执行",
  needs_human: "待人工处理",
  awaiting_confirmation: "待坐席确认",
  manual_flow: "标准流程·人工接续",
  executing: "执行中",
  succeeded: "已核验完成",
  failed: "执行失败",
  unknown: "结果未知，待对账",
  cancelled: "已取消",
};
const STATUS_LABEL_EN: Record<string, string> = {
  proposed: "Needs confirmation",
  awaiting_input: "Waiting for customer details",
  ready: "Ready",
  needs_human: "Needs human review",
  awaiting_confirmation: "Waiting for agent confirmation",
  manual_flow: "Manual follow-up",
  executing: "In progress",
  succeeded: "Verified complete",
  failed: "Failed",
  unknown: "Unknown; reconciliation required",
  cancelled: "Cancelled",
};

/**
 * Blocked reason → a sentence an agent can act on.
 *
 * The codes come from `tasks.planner` and `semantic.arbitration`. Showing the
 * raw code would be honest and useless; showing nothing would be worse.
 */
const BLOCKED_LABEL: Record<string, string> = {
  SEMANTIC_NO_WRITE_CAPABILITY: "平台当前没有执行该写操作的连接器，需人工在业务系统处理。",
  SEMANTIC_NO_READ_CAPABILITY: "当前租户没有可用的只读连接器，无法自动查询。",
  SEMANTIC_MISSING_SLOTS: "缺少必填信息。",
  SEMANTIC_NEEDS_CLARIFICATION: "信息不明确，需要先向客户确认。",
  TASK_MISSING_FIELDS: "缺少必填信息。",
  TASK_WAITING_DEPENDENCY: "等待前置任务完成。",
  TASK_DEPENDENCY_MISSING: "未找到前置任务，已阻止执行。",
  TASK_DEPENDENCY_PENDING: "前置任务尚未核验完成。",
  TASK_DEPENDENCY_CONDITION_INVALID: "前置条件无效，已阻止执行。",
  TASK_DEPENDENCY_CONDITION_UNRESOLVED: "无法从已核验的业务读取结果确认前置条件。",
  TASK_DEPENDENCY_EXECUTION_RECHECK_REQUIRED: "条件当前满足；写入前仍需通过 Tool Gateway 重新查询。",
  TASK_HANDED_TO_HUMAN: "已转人工处理。",
  TASK_CONDITION_UNMET: "前置条件不满足，下游动作未执行。",
  TASK_TOOL_SELECTION_AMBIGUOUS: "多个已授权工具都匹配该任务；已停止自动选择，请由坐席确认工具。",
  TASK_TOOL_SELECTION_UNRESOLVED: "无法将已授权工具可靠匹配到该子任务；已停止执行，请人工核对。",
  FLOW_EXECUTOR_UNAVAILABLE: "该流程已绑定到当前会话并进入人工接续；流程专用执行器尚未接入，不会自动查询或写入业务系统。",
  FLOW_INTERNAL_CASE_ONLY: "当前流程只会准备平台内部 Case 提案，不会直接开票、维修、质量判定或调用外部工程系统。",
  FLOW_INTERNAL_CASE_PROPOSAL_UNAVAILABLE: "平台未能准备内部工单提案；请人工核对账户关联和当前工具权限。",
  FLOW_BUSINESS_RECORD_UNVERIFIED: "Demo 业务记录与当前会话账户不匹配，已阻止流程完成并转人工核对。",
  TOOL_EXECUTION_FAILED: "工具执行失败；请查看关联提案和审计记录后再决定如何处理。",
  TOOL_EXECUTION_UNKNOWN: "执行结果未知，不能视为完成；请先对账，禁止盲目重试。",
};
const BLOCKED_LABEL_EN: Record<string, string> = {
  SEMANTIC_NO_WRITE_CAPABILITY: "The platform cannot perform this write; handle it in the business system.",
  SEMANTIC_NO_READ_CAPABILITY: "This tenant has no available read tool for the requested lookup.",
  SEMANTIC_MISSING_SLOTS: "Required information is missing.",
  SEMANTIC_NEEDS_CLARIFICATION: "The request is unclear; confirm it with the customer first.",
  TASK_MISSING_FIELDS: "Required information is missing.",
  TASK_WAITING_DEPENDENCY: "Waiting for a prerequisite task.",
  TASK_DEPENDENCY_MISSING: "A prerequisite task was not found; execution is blocked.",
  TASK_DEPENDENCY_PENDING: "A prerequisite task has not been verified complete.",
  TASK_DEPENDENCY_CONDITION_INVALID: "The prerequisite condition is invalid; execution is blocked.",
  TASK_DEPENDENCY_CONDITION_UNRESOLVED: "The prerequisite cannot be confirmed from a verified business read.",
  TASK_DEPENDENCY_EXECUTION_RECHECK_REQUIRED: "The condition currently holds; the Gateway must recheck it before a write.",
  TASK_HANDED_TO_HUMAN: "This task has been handed to a person.",
  TASK_CONDITION_UNMET: "The prerequisite was not met; the dependent action was not run.",
  TASK_TOOL_SELECTION_AMBIGUOUS: "More than one authorized tool matches this task; an agent must choose.",
  TASK_TOOL_SELECTION_UNRESOLVED: "No authorized tool could be matched to this task safely; review it manually.",
  FLOW_EXECUTOR_UNAVAILABLE: "This flow requires manual follow-up and will not run automatically.",
  FLOW_INTERNAL_CASE_ONLY: "This flow can prepare an internal Case proposal only; it does not issue invoices or make external business changes.",
  FLOW_INTERNAL_CASE_PROPOSAL_UNAVAILABLE: "The internal Case proposal was not prepared; verify the account and tool permissions.",
  FLOW_BUSINESS_RECORD_UNVERIFIED: "The Demo record does not match this conversation's account; the flow was stopped for human review.",
  TOOL_EXECUTION_FAILED: "The tool failed. Review the proposal and audit record before deciding what to do next.",
  TOOL_EXECUTION_UNKNOWN: "The result is unknown. Reconcile it before retrying; do not assume it failed or succeeded.",
};

const SPECIFICATION_LABELS: Record<string, string> = {
  layer_count: "层数",
  board_thickness_um: "板厚（μm）",
  surface_finish: "表面处理",
};
const SPECIFICATION_LABELS_EN: Record<string, string> = {
  layer_count: "Layer count",
  board_thickness_um: "Board thickness (μm)",
  surface_finish: "Surface finish",
};

function specificationLabel(key: string, lang: Lang): string {
  const labels = lang === "zh" ? SPECIFICATION_LABELS : SPECIFICATION_LABELS_EN;
  return labels[key] ?? key.replace(/_/g, " ");
}

const KIND_LABEL: Record<string, string> = {
  read: "查询",
  write: "变更",
  clarify: "澄清",
};
const KIND_LABEL_EN: Record<string, string> = { read: "Read", write: "Write", clarify: "Clarification" };

const ORIGIN_LABEL: Record<string, string> = {
  customer_stated: "客户自述",
  agent_collected: "坐席录入",
  verified_receipt: "已核验回执",
  verified_business_record: "业务记录已核验",
  server_capability: "按已授权工具 schema 绑定",
  inferred: "推断（不可直接采信）",
};
const ORIGIN_LABEL_EN: Record<string, string> = {
  customer_stated: "Customer-provided",
  agent_collected: "Entered by agent",
  verified_receipt: "Verified receipt",
  verified_business_record: "Verified business record",
  server_capability: "Bound by the authorized tool schema",
  inferred: "Inferred (not verified)",
};

const FLOW_TITLE_EN: Record<string, string> = {
  order_status: "Order status",
  repair_quality_intake: "Quality intake",
  invoice_application: "Invoice request",
  technical_escalation: "Technical escalation",
};
const TASK_FIELD_LABELS: Record<string, string> = {
  order_id: "订单编号",
  product_ref: "产品编号",
  issue_summary: "问题描述",
  question_or_symptom: "技术问题或现象",
  expected_vs_actual: "期望与实际情况",
  lot_or_work_order_ref: "批次或工单号",
  customer_account_ref: "客户账户",
  tax_id: "税号",
  tool_result: "只读结果",
};
const TASK_FIELD_LABELS_EN: Record<string, string> = {
  order_id: "Order number",
  product_ref: "Product reference",
  issue_summary: "Issue description",
  question_or_symptom: "Technical issue or symptom",
  expected_vs_actual: "Expected versus actual",
  lot_or_work_order_ref: "Lot or work-order reference",
  customer_account_ref: "Customer account",
  tax_id: "Tax ID",
  tool_result: "Read result",
};

function taskFieldLabel(name: string, lang: Lang): string {
  const labels = lang === "zh" ? TASK_FIELD_LABELS : TASK_FIELD_LABELS_EN;
  return labels[name] ?? name.replace(/_/g, " ");
}

function taskToolResultLabel(value: unknown, lang: Lang): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return String(value ?? "—");
  }
  const result = value as Record<string, unknown>;
  if (typeof result.order_id === "string" && typeof result.status === "string") {
    return `${result.order_id} · ${result.status}`;
  }
  const toolName = typeof result.tool_name === "string" ? result.tool_name : "Tool Gateway";
  return copy(lang, `${toolName} · 已核验回执`, `${toolName} · verified receipt`);
}

function flowTitle(task: ConversationTask, lang: Lang): string {
  return lang === "en" && task.flow_key
    ? FLOW_TITLE_EN[task.flow_key] ?? task.flow_title ?? "Standard flow"
    : task.flow_title ?? (lang === "en" ? "Standard flow" : "标准流程");
}

function conditionFieldLabel(name: string, lang: Lang): string {
  if (name === "order.status") return lang === "zh" ? "订单状态" : "Order status";
  return taskFieldLabel(name, lang);
}

function conditionOperatorLabel(operator: string, lang: Lang): string {
  const labels: Record<string, readonly [string, string]> = {
    eq: ["等于", "is"],
    ne: ["不等于", "is not"],
    in: ["属于", "is one of"],
    not_in: ["不属于", "is not one of"],
  };
  const pair = labels[operator];
  return pair ? (lang === "zh" ? pair[0] : pair[1]) : operator;
}

function copy(lang: Lang, chinese: string, english: string): string {
  return lang === "zh" ? chinese : english;
}

function isTerminal(status: ConversationTask["status"]): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

export interface TaskPanelProps {
  conversationRef: string;
  leaseVersion: number;
  /** Whether this agent currently owns the conversation. */
  isOwner: boolean;
  leaseOwnerRef: string | null;
  /** Called after a command so the page can re-read the conversation. */
  onChanged?: () => void;
}

export function TaskPanel({
  conversationRef,
  leaseVersion,
  isOwner,
  leaseOwnerRef,
  onChanged,
}: TaskPanelProps) {
  const { lang } = useLang();
  const [tasks, setTasks] = useState<ConversationTask[] | null>(null);
  const [demoPresalesEnabled, setDemoPresalesEnabled] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyTaskIds, setBusyTaskIds] = useState<Set<string>>(() => new Set());
  const commandIdempotencyKeys = useRef(new Map<string, string>());
  // Keyed `${task_id}:${field}` rather than by field name. Two tasks on one
  // screen can both be waiting for `street`, and a shared key would put one
  // task's typed value into the other's input.
  const [collecting, setCollecting] = useState<Record<string, string>>({});
  const [announcement, setAnnouncement] = useState("");
  const collectRef = useRef<Record<string, HTMLInputElement | null>>({});
  const emptyRefreshes = useRef(0);
  // The request counter, held in a ref because advancing it must not
  // re-render. `mayApply` decides whether a response may still land: a
  // sequence number alone does not catch a response that was already in
  // flight when the operator navigated, because no second request has
  // started yet - which is the case the acceptance review reproduced.
  const seqRef = useRef<RequestSeq>({ current: 0 });

  const load = useCallback(async () => {
    const startedAt = nextSeq(seqRef.current);
    const forConversation = conversationRef;
    setLoadError(null);
    try {
      const data = await apiGet<TasksResponse>(
        `/v1/workbench/conversations/${forConversation}/tasks`,
      );
      if (!mayApply(seqRef.current, startedAt, forConversation, conversationRef)) return;
      setTasks(data.items);
      setDemoPresalesEnabled(data.demo_presales_enabled === true);
    } catch (reason) {
      if (!mayApply(seqRef.current, startedAt, forConversation, conversationRef)) return;
      setTasks([]);
      setDemoPresalesEnabled(false);
      setLoadError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [conversationRef]);

  useEffect(() => {
    // Invalidate anything in flight before clearing, so a late response for
    // the previous conversation cannot repopulate this one.
    nextSeq(seqRef.current);
    setTasks(null);
    setCollecting({});
    setAnnouncement("");
    emptyRefreshes.current = 0;
    void load();
  }, [load]);

  useEffect(() => {
    const refresh = (event: Event) => {
      const detail = (event as CustomEvent<{ conversationRef?: string }>).detail;
      if (detail?.conversationRef === conversationRef) void load();
    };
    window.addEventListener("workbench:tasks-changed", refresh);
    return () => window.removeEventListener("workbench:tasks-changed", refresh);
  }, [conversationRef, load]);

  useEffect(() => {
    if (tasks === null || tasks.length > 0 || emptyRefreshes.current >= 3) return;
    const timer = window.setTimeout(() => {
      emptyRefreshes.current += 1;
      void load();
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [load, tasks]);

  useEffect(() => {
    if (!tasks?.some((task) => task.status === "awaiting_confirmation" || task.status === "executing")) {
      return;
    }
    const timer = window.setTimeout(() => void load(), 3000);
    return () => window.clearTimeout(timer);
  }, [load, tasks]);

  const run = useCallback(
    async (task: ConversationTask, command: TaskCommand, fields?: Record<string, string>) => {
      setBusyTaskIds((current) => new Set(current).add(task.task_id));
      setActionError(null);
      const commandIdentity = `${task.task_id}:${task.version}:${leaseVersion}:${command}`;
      for (const identity of commandIdempotencyKeys.current.keys()) {
        if (identity.startsWith(`${task.task_id}:`) && identity !== commandIdentity) {
          commandIdempotencyKeys.current.delete(identity);
        }
      }
      const idempotencyKey = commandIdempotencyKeys.current.get(commandIdentity) ?? newIdempotencyKey();
      commandIdempotencyKeys.current.set(commandIdentity, idempotencyKey);
      try {
        const response = await apiPost<{ task: ConversationTask }>(
          `/v1/workbench/conversations/${conversationRef}/tasks/${task.task_id}/commands`,
          {
            command,
            expected_version: task.version,
            expected_lease_version: leaseVersion,
            fields: fields ?? {},
          },
          idempotencyKey,
        );
        commandIdempotencyKeys.current.delete(commandIdentity);
        setAnnouncement(
          command === "cancel"
            ? copy(lang, "任务已取消。", "Task cancelled.")
            : command === "handoff"
              ? copy(lang, "任务已转人工。", "Task handed to a person.")
              : command === "prepare_proposal"
                ? copy(lang, "任务已进入待确认，等待坐席确认后执行。", "The proposal is ready and waiting for agent confirmation.")
                : command === "execute_read"
                  ? response.task.status === "succeeded"
                    ? copy(lang, "只读工具结果已通过 Tool Gateway 核验并记录。", "The read result was verified through Tool Gateway and recorded.")
                    : copy(lang, "只读查询未核验，已记录实际执行状态。", "The read could not be verified; its actual execution status was recorded.")
                : command === "query_order_status"
                  ? response.task.status === "succeeded"
                    ? copy(lang, "Demo ERP 订单状态已核验；回执已记录在任务中。", "The Demo order status was verified and its receipt was added to the task.")
                    : response.task.status === "unknown"
                      ? copy(lang, "订单查询结果未知，已转人工核对。", "The order lookup result is unknown and needs human reconciliation.")
                      : copy(lang, "订单状态未核验，需人工处理。", "The order status could not be verified; human review is required.")
                : fields?.product_ref
                  ? copy(lang, "产品编号已通过本地 Demo 目录归属核验；内部 Case 提案仍需受控权限和人工确认。", "The product was verified in the local Demo catalog; an internal Case proposal still needs permission and human confirmation.")
                  : fields?.order_id
                    ? copy(lang, "订单编号已通过 Demo ERP 账户归属核验；请再明确执行只读查询。", "The order was matched to the Demo account. Explicitly run the read-only lookup when ready.")
                : task.flow_key
                  ? copy(lang, "已记录坐席整理的字段；流程仍需人工接续，不会自动执行。", "Agent-provided fields were recorded. This flow still requires manual follow-up and will not run automatically.")
                  : copy(lang, "已记录客户补充的信息。", "The additional information was recorded."),
        );
        await load();
        onChanged?.();
      } catch (reason) {
        setActionError(reason instanceof Error ? reason.message : String(reason));
        // A conflict may have advanced the task version. Refresh in place so
        // the operator sees the current action state without losing context.
        void load();
      } finally {
        setBusyTaskIds((current) => {
          const next = new Set(current);
          next.delete(task.task_id);
          return next;
        });
      }
    },
    [conversationRef, lang, leaseVersion, load, onChanged],
  );

  const collectTaskField = useCallback((taskId: string, field: string, value: string) => {
    for (const identity of commandIdempotencyKeys.current.keys()) {
      if (identity.startsWith(`${taskId}:`)) commandIdempotencyKeys.current.delete(identity);
    }
    setCollecting((previous) => ({
      ...previous,
      [collectKey(taskId, field)]: value,
    }));
  }, []);

  if (loadError) {
    return (
      <div className="wb-tasks" role="group" aria-label={copy(lang, "会话任务", "Conversation tasks")}>
        <p className="wb-tasks-error" role="alert">
          {copy(lang, "任务列表加载失败：", "Unable to load tasks: ")}{loadError}
        </p>
        <button type="button" className="wb-btn wb-btn-ghost" onClick={() => void load()}>
          {copy(lang, "重试", "Retry")}
        </button>
      </div>
    );
  }

  if (tasks === null) {
    return (
      <div className="wb-tasks" role="group" aria-label={copy(lang, "会话任务", "Conversation tasks")} aria-busy="true">
        <p className="wb-tasks-empty">{copy(lang, "正在加载任务…", "Loading tasks…")}</p>
      </div>
    );
  }

  if (tasks.length === 0) {
    return (
      <div className="wb-tasks" role="group" aria-label={copy(lang, "会话任务", "Conversation tasks")}>
        <DemoPreSalesPanel
          key={conversationRef}
          conversationRef={conversationRef}
          enabled={demoPresalesEnabled}
        />
        {actionError ? <p className="wb-tasks-error" role="alert">{copy(lang, "操作失败：", "Action failed: ")}{actionError}</p> : null}
        <p className="wb-tasks-empty">
          {copy(lang, "暂无待处理任务。新消息的需求识别在后台运行，稍后会自动刷新。", "No open tasks. New messages are being processed in the background; this list will refresh shortly.")}
        </p>
        <button
          type="button"
          className="wb-btn wb-btn-ghost"
          onClick={() => {
            emptyRefreshes.current = 0;
            void load();
          }}
        >
          {copy(lang, "刷新任务", "Refresh tasks")}
        </button>
      </div>
    );
  }

  const readable = tasks.filter((t) => !isTerminal(t.status));
  const done = tasks.filter((t) => isTerminal(t.status));

  return (
    <div className="wb-tasks" role="group" aria-label={copy(lang, "会话任务", "Conversation tasks")}>
      {/* Announced on change only, so a screen reader is not read the whole
          list on every poll. */}
      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>

      <DemoPreSalesPanel
        key={conversationRef}
        conversationRef={conversationRef}
        enabled={demoPresalesEnabled}
      />
      {actionError ? <p className="wb-tasks-error" role="alert">{copy(lang, "操作失败：", "Action failed: ")}{actionError}</p> : null}

      {!isOwner ? (
        <p className="wb-tasks-notice">
          {lang === "zh"
            ? `当前会话由${leaseOwnerRef ? "其他坐席" : "AI"}处理，只有当前负责人可以执行任务操作。`
            : `This conversation is owned by ${leaseOwnerRef ? "another agent" : "AI"}; only its current owner can act on tasks.`}
        </p>
      ) : null}

      {readable.length > 0 ? (
        <section aria-label={copy(lang, "进行中的任务", "Open tasks")}>
          <h3 className="wb-tasks-heading">{copy(lang, `进行中（${readable.length}）`, `In progress (${readable.length})`)}</h3>
          <ul className="wb-task-list">
            {readable.map((task) => (
              <TaskRow
                key={task.task_id}
                task={task}
                busy={busyTaskIds.has(task.task_id)}
                canCommand={isOwner}
                collecting={collecting}
                collectRef={collectRef}
                onCollect={(name, value) => collectTaskField(task.task_id, name, value)}
                onCommand={run}
              />
            ))}
          </ul>
        </section>
      ) : null}

      {done.length > 0 ? (
        <section aria-label={copy(lang, "已结束的任务", "Completed tasks")}>
          <h3 className="wb-tasks-heading">{copy(lang, `已结束（${done.length}）`, `Completed (${done.length})`)}</h3>
          <ul className="wb-task-list wb-task-list-done">
            {done.map((task) => (
              <TaskRow
                key={task.task_id}
                task={task}
                busy={false}
                canCommand={false}
                collecting={collecting}
                collectRef={collectRef}
                onCollect={() => undefined}
                onCommand={run}
              />
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

interface DemoPreSalesPanelProps {
  conversationRef: string;
  enabled: boolean;
}

function DemoPreSalesPanel({ conversationRef, enabled }: DemoPreSalesPanelProps) {
  const { lang } = useLang();
  const [productRef, setProductRef] = useState("PCB-DEMO-100");
  const [evidence, setEvidence] = useState<DemoPreSalesEvidence | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [announcement, setAnnouncement] = useState("");

  if (!enabled) return null;

  const loadEvidence = async () => {
    const candidate = productRef.trim();
    if (!candidate || candidate.length > 255) {
      setError(copy(lang, "请输入不超过 255 个字符的产品编号。", "Enter a product reference of 255 characters or fewer."));
      return;
    }
    setBusy(true);
    setError(null);
    setAnnouncement("");
    try {
      const response = await apiGet<{
        evidence: DemoPreSalesEvidence;
        demo_only: true;
      }>(
        `/v1/workbench/conversations/${conversationRef}/demo-presales/${encodeURIComponent(candidate)}`,
      );
      setEvidence(response.evidence);
      setAnnouncement(copy(lang, "已核验产品规格、账户库存和报价样例三份合成来源，必须由销售人工复核。", "Synthetic product, inventory, and quote sources were checked. Sales must review them before use."));
    } catch (reason) {
      setEvidence(null);
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(message);
      setAnnouncement("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      className="wb-task-slots"
      aria-labelledby={`demo-presales-heading-${conversationRef}`}
      aria-busy={busy}
    >
      <h3 id={`demo-presales-heading-${conversationRef}`}>{copy(lang, "合成售前资料（仅供销售复核）", "Synthetic pre-sales evidence (sales review only)")}</h3>
      <p id={`demo-presales-help-${conversationRef}`}>
        {copy(lang, "local/test Demo 样例；不是客户报价，不会创建 CRM 商机或发送消息。", "Local/test Demo data; not a customer quote. It will not create a CRM opportunity or send a message.")}
      </p>
      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>
      <label htmlFor={`demo-presales-product-${conversationRef}`}>{copy(lang, "产品编号", "Product reference")}</label>
      <input
        id={`demo-presales-product-${conversationRef}`}
        aria-describedby={`demo-presales-help-${conversationRef}`}
        value={productRef}
        maxLength={255}
        disabled={busy}
        onChange={(event) => {
          setProductRef(event.target.value);
          setEvidence(null);
          setError(null);
          setAnnouncement("");
        }}
      />
      <button
        type="button"
        className="wb-btn wb-btn-ghost"
        disabled={busy || productRef.trim().length === 0}
        onClick={() => void loadEvidence()}
      >
        {busy
          ? copy(lang, "核验 Demo 来源…", "Checking Demo sources…")
          : copy(lang, "查看合成预售依据", "View synthetic evidence")}
      </button>
      {error ? <p role="alert" className="wb-tasks-error">{error}</p> : null}
      {evidence ? (
        <section aria-label={copy(lang, "经来源校验的合成证据", "Source-verified synthetic evidence")}>
          <p>
            {evidence.product_name} / {evidence.product_ref} / {copy(lang, "版本", "Revision")} {evidence.revision}
          </p>
          <dl>
            <div>
              <dt>{copy(lang, "规格", "Specifications")}</dt>
              <dd>
                {Object.entries(evidence.specifications)
                  .map(([key, value]) => `${specificationLabel(key, lang)}${lang === "zh" ? "：" : ": "}${String(value)}`)
                  .join(" · ")}
              </dd>
            </div>
            <div>
              <dt>{copy(lang, "演示库存", "Demo inventory")}</dt>
              <dd>
                {evidence.available_quantity} {evidence.unit_code} · {evidence.warehouse_ref}
              </dd>
            </div>
            <div>
              <dt>{copy(lang, "合成参考报价", "Synthetic indicative price")}</dt>
              <dd>
                {lang === "zh"
                  ? `${(evidence.indicative_unit_price_minor / 100).toFixed(2)} ${evidence.currency} / 件；起订量 ${evidence.minimum_quantity}；示例交期 ${evidence.lead_time_business_days ?? "—"} 个工作日`
                  : `${(evidence.indicative_unit_price_minor / 100).toFixed(2)} ${evidence.currency} / unit; minimum quantity ${evidence.minimum_quantity}; sample lead time ${evidence.lead_time_business_days ?? "—"} business days`}
              </dd>
            </div>
            <div>
              <dt>{copy(lang, "报价样例有效期", "Sample quote valid until")}</dt>
              <dd>{evidence.quote_valid_until}</dd>
            </div>
            <div>
              <dt>{copy(lang, "来源版本", "Source versions")}</dt>
              <dd>
                {lang === "zh"
                  ? `产品 ${evidence.product_source_version} · 库存 ${evidence.inventory_source_version} · 报价 ${evidence.quote_source_version}`
                  : `Product ${evidence.product_source_version} · Inventory ${evidence.inventory_source_version} · Quote ${evidence.quote_source_version}`}
              </dd>
            </div>
          </dl>
          <p role="note">
            {lang === "zh"
              ? `必须转销售人工确认；客户报价${evidence.customer_quote_allowed ? "已获准" : "不允许"}。`
              : `Sales review is required; customer quoting is ${evidence.customer_quote_allowed ? "allowed" : "not allowed"}.`}
          </p>
        </section>
      ) : null}
    </section>
  );
}

interface TaskRowProps {
  task: ConversationTask;
  busy: boolean;
  canCommand: boolean;
  collecting: Record<string, string>;
  collectRef: React.MutableRefObject<Record<string, HTMLInputElement | null>>;
  onCollect: (name: string, value: string) => void;
  onCommand: (
    task: ConversationTask,
    command: TaskCommand,
    fields?: Record<string, string>,
  ) => Promise<void>;
}

function TaskRow({
  task,
  busy,
  canCommand,
  collecting,
  collectRef,
  onCollect,
  onCommand,
}: TaskRowProps) {
  const { lang } = useLang();
  const [expanded, setExpanded] = useState(false);
  const missing = task.missing_slots;
  const dependencyBlocked = task.dependency_blocked === true;
  const collectable =
    !dependencyBlocked &&
    (task.status === "awaiting_input" ||
      task.status === "ready" ||
      task.status === "manual_flow" ||
      (task.flow_key === "invoice_application" && task.status === "needs_human"));
  const mayPrepareFlowProposal =
    !dependencyBlocked &&
    (task.flow_key === "invoice_application" ||
      task.flow_key === "repair_quality_intake" ||
      task.flow_key === "technical_escalation") &&
    (task.status === "manual_flow" || task.status === "needs_human") &&
    missing.length === 0 &&
    task.flow_can_prepare_proposal;
  const mayQueryOrderStatus =
    !dependencyBlocked &&
    task.flow_key === "order_status" &&
    task.kind === "read" &&
    task.status === "manual_flow" &&
    missing.length === 0 &&
    task.flow_can_query_order;
  const mayExecuteSemanticRead =
    !dependencyBlocked &&
    task.flow_key === null &&
    task.kind === "read" &&
    task.status === "ready" &&
    task.slots.some(
      (slot) =>
        slot.name === "tool" &&
        slot.origin === "server_capability" &&
        slot.selection_source === "allowlisted_candidate_schema_match",
    );
  // Both decisions come from `lib/taskPanelState`, which `task-panel-state`
  // executes: per-task field keys, and only the fields this task is waiting
  // for. Inlined here they were correct but untested, and the acceptance
  // review had to read the component to find that.
  const allCollected = everyFieldFilled(collecting, task.task_id, missing);
  const collected = collectedFor(collecting, task.task_id, missing);
  const taskStatusLabel =
    task.status === "cancelled" && task.blocked_reason === "TASK_CONDITION_UNMET"
      ? copy(lang, "已跳过（前置条件不满足）", "Skipped; prerequisite condition was not met")
      : (lang === "zh" ? STATUS_LABEL : STATUS_LABEL_EN)[task.status] ?? task.status;

  return (
    <li className={`wb-task wb-task-${task.status}`}>
      {task.flow_title ? <p className="wb-task-flow-title">{copy(lang, "标准流程：", "Standard flow: ")}{flowTitle(task, lang)} (v{task.flow_version})</p> : null}
      <div className="wb-task-head">
        <span className={`wb-task-status wb-task-status-${task.status}`}>
          {taskStatusLabel}
        </span>
        <span className="wb-task-kind">{(lang === "zh" ? KIND_LABEL : KIND_LABEL_EN)[task.kind] ?? task.kind}</span>
        {task.status === "succeeded" ? (
          <span className="wb-task-verified">{copy(lang, "已核验", "Verified")}</span>
        ) : null}
      </div>

      {task.slots.length > 0 ? (
        <dl className="wb-task-slots">
          {task.slots.map((slot) => (
            <div key={slot.name} className="wb-task-slot">
              <dt>
                {slot.name === "order_status_receipt"
                  ? copy(lang, "订单状态回执", "Order status receipt")
                  : taskFieldLabel(slot.name, lang)}
              </dt>
              <dd>
                {slot.value_withheld ? (
                  <span className="wb-task-withheld">
                    {slot.inferred
                      ? copy(lang, "推断值，未采信", "Inferred value; not accepted")
                      : slot.origin === "agent_collected"
                        ? copy(lang, "坐席录入的敏感值未保存", "Sensitive agent-entered value was not stored")
                        : copy(lang, "已记录于会话，未在任务中展示", "Kept in the conversation; hidden from this task")}
                  </span>
                ) : (
                  <span>
                    {slot.name === "order_status_receipt"
                      ? copy(lang, "已核验；展开下方 Demo ERP 回执", "Verified; see the Demo ERP receipt below")
                      : slot.name === "tool_result"
                        ? taskToolResultLabel(slot.value, lang)
                      : String(slot.value ?? "—")}
                  </span>
                )}
                <em className="wb-task-origin">
                  {slot.verification_source === "demo"
                    ? slot.origin === "verified_receipt"
                      ? copy(lang, "Demo ERP 已核验回执", "Verified Demo ERP receipt")
                      : copy(lang, "本地 Demo 目录核验", "Verified in local Demo catalog")
                    : (lang === "zh" ? ORIGIN_LABEL : ORIGIN_LABEL_EN)[slot.origin] ?? slot.origin}
                </em>
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      {task.slots
        .filter((slot) => slot.name === "order_status_receipt")
        .map((slot) => {
          const receipt = asDemoOrderStatusReceipt(slot.value);
          if (!receipt) return null;
          return (
            <section
              key={`${task.task_id}:order-status-receipt`}
              className="wb-task-slots"
              aria-label={copy(lang, "订单状态查询回执", "Order status lookup receipt")}
            >
              <strong>{copy(lang, "合成 Demo ERP 回执", "Synthetic Demo ERP receipt")}</strong>
              <p>
                {copy(lang, "订单", "Order")} {receipt.order_id}: {receipt.status}
              </p>
              {receipt.eta ? <p>{copy(lang, "预计时间：", "Estimated time: ")}{receipt.eta}</p> : null}
              <ol>
                {receipt.nodes.map((node, index) => (
                  <li key={`${node.label}-${index}`}>
                    {node.label}{lang === "zh" ? "：" : ": "}{node.status}
                    {node.at ? ` · ${node.at}` : ""}
                  </li>
                ))}
              </ol>
              <p>{copy(lang, "来源：Demo · 查询时间：", "Source: Demo · Checked at: ")}{receipt.fetched_at}</p>
            </section>
          );
        })}

        {task.blocked_reason ? (
        <p className="wb-task-blocked">
          {(lang === "zh" ? BLOCKED_LABEL : BLOCKED_LABEL_EN)[task.blocked_reason] ??
            copy(lang, `原因：${task.blocked_reason}`, `Reason: ${task.blocked_reason}`)}
        </p>
      ) : null}

      {missing.length > 0 ? (
        <div className="wb-task-missing">
          <p className="wb-task-missing-label">{copy(lang, "待补充：", "Needed:")}</p>
          <ul>
            {missing.map((name) => (
              <li key={name}>
                <label htmlFor={`collect-${task.task_id}-${name}`}>
                  {name === "product_ref"
                    ? copy(lang, "产品编号（提交后由业务目录核验）", "Product reference (verified against the business catalog)")
                    : name === "order_id"
                      ? copy(lang, "订单编号（提交后由 Demo ERP 核验）", "Order number (verified by Demo ERP)")
                      : taskFieldLabel(name, lang)}
                </label>
                <input
                  id={`collect-${task.task_id}-${name}`}
                  ref={(el) => {
                    collectRef.current[`${task.task_id}:${name}`] = el;
                  }}
                  value={collected[name] ?? ""}
                  disabled={!canCommand || busy}
                  onChange={(e) => onCollect(name, e.target.value)}
                  placeholder={
                    name === "product_ref"
                      ? copy(lang, "例如 PCB-DEMO-100", "e.g. PCB-DEMO-100")
                      : name === "order_id"
                        ? copy(lang, "例如 SO-9001", "e.g. SO-9001")
                        : copy(lang, "客户的原话", "Customer's own words")
                  }
                />
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {task.condition ? (
        <p className="wb-task-condition">
          {copy(lang, "仅当", "Only when")} <code>{conditionFieldLabel(task.condition.field, lang)}</code> {conditionOperatorLabel(task.condition.operator, lang)}{" "}
          <code>{String(task.condition.value)}</code> {copy(lang, "时执行", "will this run")}
        </p>
      ) : null}

      <div className="wb-task-actions">
        <button
          type="button"
          className="wb-btn wb-btn-ghost"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
        >
          {expanded ? copy(lang, "收起详情", "Hide details") : copy(lang, "详情", "Details")}
        </button>

        {canCommand && collectable && missing.length > 0 ? (
          <button
            type="button"
            className="wb-btn wb-btn-primary"
            disabled={busy || !allCollected}
            onClick={() => void onCommand(task, "collect_fields", collected)}
          >
            {busy ? copy(lang, "提交中…", "Saving…") : copy(lang, "记录补充", "Save details")}
          </button>
        ) : null}
        {canCommand && mayExecuteSemanticRead ? (
          <button
            type="button"
            className="wb-btn wb-btn-primary"
            disabled={busy}
            onClick={() => void onCommand(task, "execute_read")}
            title={copy(
              lang,
              "仅执行服务端按只读 schema 绑定的 Tool Gateway 工具。",
              "Runs only the server-bound read-only Tool Gateway tool.",
            )}
          >
            {busy ? copy(lang, "查询中…", "Checking…") : copy(lang, "执行只读查询", "Run read-only query")}
          </button>
        ) : null}
        {canCommand && mayQueryOrderStatus ? (
          <button
            type="button"
            className="wb-btn wb-btn-primary"
            disabled={busy}
            onClick={() => void onCommand(task, "query_order_status")}
            title={copy(lang, "通过 Tool Gateway 查询本地合成 Demo 订单；回执会标明 Demo 来源", "Look up a synthetic local Demo order through the Tool Gateway; the receipt identifies its Demo source.")}
          >
            {busy ? copy(lang, "查询中…", "Checking…") : copy(lang, "查询 Demo 订单状态", "Check Demo order status")}
          </button>
        ) : null}
        {canCommand &&
        task.flow_key === "order_status" &&
        task.status === "manual_flow" &&
        missing.length === 0 &&
        !task.flow_can_query_order ? (
          <p className="wb-task-disabled-reason">
            {copy(lang, "标准流程查询仅在 local/test Demo 模式并具备订单读取权限时开放；没有可用的 orders_read connector 时需人工核对。", "Order lookup is available only in local/test Demo mode with read permission. Without an orders-read capability, reconcile the order manually.")}
          </p>
        ) : null}
        {(task.flow_key === "invoice_application" ||
          task.flow_key === "repair_quality_intake" ||
          task.flow_key === "technical_escalation") &&
        (task.status === "manual_flow" || task.status === "needs_human") &&
        missing.length === 0 &&
        !task.flow_can_prepare_proposal ? (
          <p className="wb-task-disabled-reason">
            {copy(lang, "需要受控写入权限；质量和技术流程还必须在 local/test Demo 模式并完成产品归属核验。", "Controlled write permission is required. Quality and technical flows also require local/test Demo mode and verified product ownership.")}
          </p>
        ) : null}

        {canCommand && !isTerminal(task.status) ? (
          <>
            {task.kind === "write" &&
            !dependencyBlocked &&
            ((task.status === "ready" && !task.flow_key) || mayPrepareFlowProposal) ? (
              <button
                type="button"
                className="wb-btn"
                disabled={busy}
                onClick={() => void onCommand(task, "prepare_proposal")}
                title={
                  task.flow_key === "invoice_application"
                    ? copy(lang, "准备创建平台内部申请工单的提案；不会开票，也不会自动执行", "Prepare an internal request proposal; this will not issue an invoice or execute automatically.")
                    : task.flow_key === "repair_quality_intake"
                      ? copy(lang, "准备由坐席确认的平台内部质量受理 Case；使用本地 Demo 产品目录", "Prepare an internal quality Case for agent confirmation using the local Demo catalog.")
                      : task.flow_key === "technical_escalation"
                        ? copy(lang, "准备由坐席确认的平台内部工程 Case；使用本地 Demo 产品目录", "Prepare an internal engineering Case for agent confirmation using the local Demo catalog.")
                        : copy(lang, "生成待确认提案；不会自动执行", "Prepare a proposal for confirmation; it will not execute automatically.")
                }
              >
                {task.flow_key === "invoice_application"
                  ? copy(lang, "准备内部申请提案", "Prepare internal request")
                  : task.flow_key === "repair_quality_intake"
                    ? copy(lang, "准备质量受理提案", "Prepare quality intake")
                    : task.flow_key === "technical_escalation"
                      ? copy(lang, "准备技术升级提案", "Prepare technical escalation")
                      : copy(lang, "准备提案", "Prepare proposal")}
              </button>
            ) : null}
            {task.status !== "needs_human" ? <button
              type="button"
              className="wb-btn"
              disabled={busy}
              onClick={() => void onCommand(task, "handoff")}
            >
              {task.proposal_id && task.status === "awaiting_confirmation"
                ? copy(lang, "撤回提案并转人工", "Withdraw proposal and hand off")
                : copy(lang, "转人工", "Hand off")}
            </button> : null}
            <button
              type="button"
              className="wb-btn wb-btn-danger"
              disabled={busy}
              onClick={() => void onCommand(task, "cancel")}
            >
              {task.proposal_id && task.status === "awaiting_confirmation"
                ? copy(lang, "撤回提案并取消", "Withdraw proposal and cancel")
                : copy(lang, "取消", "Cancel")}
            </button>
          </>
        ) : null}

        {!canCommand && !isTerminal(task.status) ? (
          <p className="wb-task-disabled-reason">{copy(lang, "你不是当前会话的负责人，无法执行任务操作。", "Only the current conversation owner can act on this task.")}</p>
        ) : null}
      </div>

      {expanded ? (
        <dl className="wb-task-meta">
          <div>
            <dt>{copy(lang, "任务标识", "Task key")}</dt>
            <dd>{task.local_key}</dd>
          </div>
          <div>
            <dt>{copy(lang, "版本", "Version")}</dt>
            <dd>
              {lang === "zh"
                ? `${task.version}（动作修订 ${task.action_revision}）`
                : `${task.version} (action revision ${task.action_revision})`}
            </dd>
          </div>
          {task.depends_on.length > 0 ? (
            <div>
              <dt>{copy(lang, "依赖", "Dependencies")}</dt>
              <dd>{task.depends_on.join("、")}</dd>
            </div>
          ) : null}
          {task.proposal_id ? (
            <div>
              <dt>{copy(lang, "关联提案", "Proposal")}</dt>
              <dd>
                {task.proposal_id} · <Link to={`/admin/approvals?proposal_id=${encodeURIComponent(task.proposal_id)}`}>{copy(lang, "在审批页查看", "View in approvals")}</Link>
              </dd>
            </div>
          ) : null}
        </dl>
      ) : null}
    </li>
  );
}
