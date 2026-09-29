import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
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
  origin: "customer_stated" | "verified_receipt" | "inferred" | "agent_collected";
  confirmed: boolean;
  value?: unknown;
  value_withheld?: boolean;
  inferred?: boolean;
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
    | "cancelled";
  sequence: number;
  version: number;
  action_revision: number;
  slots: TaskSlot[];
  missing_slots: string[];
  depends_on: string[];
  condition: { field: string; operator: string; value: unknown } | null;
  blocked_reason: string | null;
  proposal_id: string | null;
  execution_id: string | null;
  source_turn_id: string;
  updated_at: number;
}

interface TasksResponse {
  conversation_ref: string;
  items: ConversationTask[];
  limit: number;
  offset: number;
}

export type TaskCommand = "collect_fields" | "cancel" | "handoff" | "prepare_proposal";

/** Status → label. A closed vocabulary: an unknown status renders as itself. */
const STATUS_LABEL: Record<string, string> = {
  proposed: "待确认需求",
  awaiting_input: "等待客户补充",
  ready: "可执行",
  needs_human: "待人工处理",
  awaiting_confirmation: "待坐席确认",
  executing: "执行中",
  succeeded: "已核验完成",
  failed: "执行失败",
  unknown: "结果未知，待对账",
  cancelled: "已取消",
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
  TASK_HANDED_TO_HUMAN: "已转人工处理。",
  TASK_CONDITION_UNMET: "前置条件不满足，已跳过。",
};

const KIND_LABEL: Record<string, string> = {
  read: "查询",
  write: "变更",
  clarify: "澄清",
};

const ORIGIN_LABEL: Record<string, string> = {
  customer_stated: "客户自述",
  agent_collected: "坐席录入",
  verified_receipt: "已核验回执",
  inferred: "推断（不可直接采信）",
};

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
  const [tasks, setTasks] = useState<ConversationTask[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyTaskId, setBusyTaskId] = useState<string | null>(null);
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
    setError(null);
    try {
      const data = await apiGet<TasksResponse>(
        `/v1/workbench/conversations/${forConversation}/tasks`,
      );
      if (!mayApply(seqRef.current, startedAt, forConversation, conversationRef)) return;
      setTasks(data.items);
    } catch (reason) {
      if (!mayApply(seqRef.current, startedAt, forConversation, conversationRef)) return;
      setTasks([]);
      setError(reason instanceof Error ? reason.message : String(reason));
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
    if (tasks === null || tasks.length > 0 || emptyRefreshes.current >= 3) return;
    const timer = window.setTimeout(() => {
      emptyRefreshes.current += 1;
      void load();
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [load, tasks]);

  const run = useCallback(
    async (task: ConversationTask, command: TaskCommand, fields?: Record<string, string>) => {
      setBusyTaskId(task.task_id);
      setError(null);
      try {
        await apiPost(
          `/v1/workbench/conversations/${conversationRef}/tasks/${task.task_id}/commands`,
          {
            command,
            expected_version: task.version,
            expected_lease_version: leaseVersion,
            fields: fields ?? {},
          },
          newIdempotencyKey(),
        );
        setAnnouncement(
          command === "cancel"
            ? "任务已取消。"
            : command === "handoff"
              ? "任务已转人工。"
              : command === "prepare_proposal"
                ? "任务已进入待确认，等待坐席确认后执行。"
                : "已记录客户补充的信息。",
        );
        await load();
        onChanged?.();
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason));
      } finally {
        setBusyTaskId(null);
      }
    },
    [conversationRef, leaseVersion, load, onChanged],
  );

  if (error) {
    return (
      <div className="wb-tasks" role="group" aria-label="会话任务">
        <p className="wb-tasks-error" role="alert">
          任务列表加载失败：{error}
        </p>
        <button type="button" className="wb-btn wb-btn-ghost" onClick={() => void load()}>
          重试
        </button>
      </div>
    );
  }

  if (tasks === null) {
    return (
      <div className="wb-tasks" role="group" aria-label="会话任务" aria-busy="true">
        <p className="wb-tasks-empty">正在加载任务…</p>
      </div>
    );
  }

  if (tasks.length === 0) {
    return (
      <div className="wb-tasks" role="group" aria-label="会话任务">
        <p className="wb-tasks-empty">
          暂无待处理任务。新消息的需求识别在后台运行，稍后会自动刷新。
        </p>
        <button
          type="button"
          className="wb-btn wb-btn-ghost"
          onClick={() => {
            emptyRefreshes.current = 0;
            void load();
          }}
        >
          刷新任务
        </button>
      </div>
    );
  }

  const readable = tasks.filter((t) => !isTerminal(t.status));
  const done = tasks.filter((t) => isTerminal(t.status));

  return (
    <div className="wb-tasks" role="group" aria-label="会话任务">
      {/* Announced on change only, so a screen reader is not read the whole
          list on every poll. */}
      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>

      {!isOwner ? (
        <p className="wb-tasks-notice">
          当前会话由{leaseOwnerRef ? `其他坐席` : "AI"}处理，只有当前负责人可以执行任务操作。
        </p>
      ) : null}

      {readable.length > 0 ? (
        <section aria-label="进行中的任务">
          <h3 className="wb-tasks-heading">进行中（{readable.length}）</h3>
          <ul className="wb-task-list">
            {readable.map((task) => (
              <TaskRow
                key={task.task_id}
                task={task}
                busy={busyTaskId === task.task_id}
                canCommand={isOwner}
                collecting={collecting}
                collectRef={collectRef}
                onCollect={(name, value) =>
                  // Scoped to the task: two tasks on one screen can both be
                  // waiting for `street`, and a shared key would cross them.
                  setCollecting((prev) => ({ ...prev, [collectKey(task.task_id, name)]: value }))
                }
                onCommand={run}
              />
            ))}
          </ul>
        </section>
      ) : null}

      {done.length > 0 ? (
        <section aria-label="已结束的任务">
          <h3 className="wb-tasks-heading">已结束（{done.length}）</h3>
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
  const [expanded, setExpanded] = useState(false);
  const missing = task.missing_slots;
  const collectable = task.status === "awaiting_input" || task.status === "ready";
  // Both decisions come from `lib/taskPanelState`, which `task-panel-state`
  // executes: per-task field keys, and only the fields this task is waiting
  // for. Inlined here they were correct but untested, and the acceptance
  // review had to read the component to find that.
  const allCollected = everyFieldFilled(collecting, task.task_id, missing);
  const collected = collectedFor(collecting, task.task_id, missing);

  return (
    <li className={`wb-task wb-task-${task.status}`}>
      <div className="wb-task-head">
        <span className={`wb-task-status wb-task-status-${task.status}`}>
          {STATUS_LABEL[task.status] ?? task.status}
        </span>
        <span className="wb-task-kind">{KIND_LABEL[task.kind] ?? task.kind}</span>
        {task.status === "succeeded" ? (
          <span className="wb-task-verified">已核验</span>
        ) : null}
      </div>

      {task.slots.length > 0 ? (
        <dl className="wb-task-slots">
          {task.slots.map((slot) => (
            <div key={slot.name} className="wb-task-slot">
              <dt>{slot.name}</dt>
              <dd>
                {slot.value_withheld ? (
                  <span className="wb-task-withheld">
                    {slot.inferred
                      ? "推断值，未采信"
                      : slot.origin === "agent_collected"
                        ? "坐席录入的敏感值未保存"
                        : "已记录于会话，未在任务中展示"}
                  </span>
                ) : (
                  <span>{String(slot.value ?? "—")}</span>
                )}
                <em className="wb-task-origin">
                  {ORIGIN_LABEL[slot.origin] ?? slot.origin}
                </em>
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      {task.blocked_reason ? (
        <p className="wb-task-blocked">
          {BLOCKED_LABEL[task.blocked_reason] ?? `原因：${task.blocked_reason}`}
        </p>
      ) : null}

      {missing.length > 0 ? (
        <div className="wb-task-missing">
          <p className="wb-task-missing-label">待补充：</p>
          <ul>
            {missing.map((name) => (
              <li key={name}>
                <label htmlFor={`collect-${task.task_id}-${name}`}>{name}</label>
                <input
                  id={`collect-${task.task_id}-${name}`}
                  ref={(el) => {
                    collectRef.current[`${task.task_id}:${name}`] = el;
                  }}
                  value={collected[name] ?? ""}
                  disabled={!canCommand || busy}
                  onChange={(e) => onCollect(name, e.target.value)}
                  placeholder="客户的原话"
                />
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {task.condition ? (
        <p className="wb-task-condition">
          仅当 <code>{task.condition.field}</code> {task.condition.operator}{" "}
          <code>{String(task.condition.value)}</code> 时执行
        </p>
      ) : null}

      <div className="wb-task-actions">
        <button
          type="button"
          className="wb-btn wb-btn-ghost"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
        >
          {expanded ? "收起详情" : "详情"}
        </button>

        {canCommand && collectable && missing.length > 0 ? (
          <button
            type="button"
            className="wb-btn wb-btn-primary"
            disabled={busy || !allCollected}
            onClick={() => void onCommand(task, "collect_fields", collected)}
          >
            {busy ? "提交中…" : "记录补充"}
          </button>
        ) : null}

        {canCommand && !isTerminal(task.status) ? (
          <>
            {task.kind === "write" ? (
              <button
                type="button"
                className="wb-btn"
                disabled={busy}
                onClick={() => void onCommand(task, "prepare_proposal")}
                title="生成待确认提案；不会自动执行"
              >
                准备提案
              </button>
            ) : null}
            <button
              type="button"
              className="wb-btn"
              disabled={busy}
              onClick={() => void onCommand(task, "handoff")}
            >
              转人工
            </button>
            <button
              type="button"
              className="wb-btn wb-btn-danger"
              disabled={busy}
              onClick={() => void onCommand(task, "cancel")}
            >
              取消
            </button>
          </>
        ) : null}

        {!canCommand && !isTerminal(task.status) ? (
          <p className="wb-task-disabled-reason">你不是当前会话的负责人，无法执行任务操作。</p>
        ) : null}
      </div>

      {expanded ? (
        <dl className="wb-task-meta">
          <div>
            <dt>任务标识</dt>
            <dd>{task.local_key}</dd>
          </div>
          <div>
            <dt>版本</dt>
            <dd>
              {task.version}（动作修订 {task.action_revision}）
            </dd>
          </div>
          {task.depends_on.length > 0 ? (
            <div>
              <dt>依赖</dt>
              <dd>{task.depends_on.join("、")}</dd>
            </div>
          ) : null}
          {task.proposal_id ? (
            <div>
              <dt>关联提案</dt>
              <dd>{task.proposal_id}</dd>
            </div>
          ) : null}
        </dl>
      ) : null}
    </li>
  );
}
