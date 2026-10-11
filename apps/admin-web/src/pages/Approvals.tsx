import { useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useAction } from "../lib/useAction";
import { useAsync } from "../lib/useAsync";
import type { ToolCatalogEntry, ToolProposal, ToolProposalExecution } from "../lib/types";
import {
  ActionFeedback,
  Badge,
  Card,
  EmptyState,
  ErrorBanner,
  ListTotal,
  PageHeader,
  SkeletonRows,
  Stat,
  type BadgeTone,
} from "../components/ui";
import { LoadError } from "../components/LoadError";
import { Pagination } from "../components/Pagination";
import { usePrompt } from "../components/Prompt";
import { useLang, type DictKey } from "../lib/i18n";
import { dateFromEpochSeconds } from "../lib/format";

const PAGE_SIZE = 50;

/**
 * The statuses a human works through, in the order they care about them.
 * `authorized` is first because it is the only one that means "this is
 * waiting on you", and it is the default filter.
 *
 * The API's `status` filter matches `effective_status`, so `authorized` here
 * means "still approvable" - an overdue proposal is reported (and filtered)
 * as `expired`, never as pending.
 */
const FILTERS = [
  "authorized",
  "confirmed",
  "verified",
  "unknown",
  "failed",
  "expired",
] as const;

function statusTone(status: string): BadgeTone {
  if (status === "verified" || status === "executed") return "good";
  if (status === "authorized" || status === "confirmed" || status === "executing") return "warn";
  // An unresolved write is the alarming one, not the failed one: a failure is
  // known to have not happened, while `unknown` may or may not have.
  if (status === "unknown" || status === "failed") return "bad";
  return "neutral";
}

function riskTone(risk: string | null): BadgeTone {
  if (risk === "prohibited") return "bad";
  if (risk === "confirmed_write" || risk === "human_approval") return "warn";
  if (risk === "low_write") return "info";
  return "neutral";
}

/** Whole minutes left, or null once the proposal is past its expiry. */
function minutesLeft(expiresAt: number): number | null {
  const remaining = expiresAt - Math.floor(Date.now() / 1000);
  return remaining <= 0 ? null : Math.ceil(remaining / 60);
}

/**
 * Whether the confirmation flag says anything the risk badge has not already
 * said.
 *
 * "No approval needed" is always news: it explains why Execute is enabled
 * without anything having been approved. "Needs approval" is news only when
 * the risk class does not already mean it - `confirmed_write` and
 * `human_approval` require confirmation by definition, and rendering the
 * badge beside them showed the same two words twice, which reads as two
 * separate facts rather than one.
 */
function confirmationIsNotable(p: ToolProposal): boolean {
  if (!p.required_confirmation) return true;
  return p.risk !== "confirmed_write" && p.risk !== "human_approval";
}

interface ProposalDetail {
  proposal: ToolProposal;
  executions: ToolProposalExecution[];
  reconciliations: ToolProposalReconciliation[];
  compensations: ToolProposalCompensation[];
}

interface ToolProposalReconciliation {
  reconciliation_id: string;
  execution_id: string;
  decision: "applied" | "not_applied" | "unresolved";
  evidence_reference: string;
  actor_id: string;
  created_at: number;
}

type ToolCompensationReason = "created_in_error" | "duplicate_case" | "incorrect_customer";

interface ToolProposalCompensation {
  compensation_id: string;
  execution_id: string;
  action: string;
  outcome: "succeeded" | "failed";
  reason_code: ToolCompensationReason;
  case_id: string | null;
  result: Record<string, unknown>;
  created_at: number;
}

/**
 * Raise a proposal for a write that did not come from a conversation.
 *
 * The queue alone was only half a workflow: it could approve and execute
 * proposals that already existed, and a support rep with a confirmation to
 * record - an EQ confirmed by phone, say - had to reach for `curl`. The
 * proposal this raises is an ordinary one: same table, same policy check, same
 * approval gate, and the `confirmed_write` rules still apply, so nothing here
 * shortcuts the control.
 */
function ProposePanel({
  onCreated,
}: {
  onCreated: (proposalId: string, message: string) => void;
}) {
  const { t } = useLang();
  const catalog = useAsync<{ items: ToolCatalogEntry[]; total: number }>(
    () => apiGet<{ items: ToolCatalogEntry[]; total: number }>("/v1/tools"),
    [],
  );
  const [tool, setTool] = useState("");
  const [args, setArgs] = useState("{}");
  const action = useAction();

  const items = catalog.data?.items ?? [];

  /**
   * Prefill from the tool's own schema.
   *
   * Starting from `{}` guarantees a 400 for any tool with required arguments,
   * and the server's answer names a JSON pointer rather than the field a
   * person would recognise. Starting from the shape the API validates against
   * makes the form a prompt instead of a quiz.
   */
  function pick(name: string) {
    setTool(name);
    const entry = items.find((i) => i.name === name);
    const required = entry?.input_schema?.required ?? [];
    const declared = Object.keys(entry?.input_schema?.properties ?? {});
    const keys = required.length > 0 ? required : declared;
    setArgs(JSON.stringify(Object.fromEntries(keys.map((k) => [k, ""])), null, 2));
  }

  async function submit() {
    if (!tool) {
      action.fail(t("approvals.toolRequired"));
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(args);
    } catch {
      action.fail(t("approvals.argsNotJson"));
      return;
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      action.fail(t("approvals.argsNotObject"));
      return;
    }

    let created: string | null = null;
    // No success message here. Raising a proposal closes this panel, so a
    // banner rendered inside it is unmounted in the same commit that sets it -
    // the operator saw the form disappear with no acknowledgement at all. The
    // message is handed to the page, whose feedback region outlives the panel.
    // The panel keeps its own error region, because a *failure* should appear
    // where the operator is still looking.
    const ok = await action.run(async () => {
      const res = await apiPost<{ proposal: ToolProposal }>(
        "/v1/tool-proposals",
        { tool_name: tool, arguments: parsed },
        // A retry after a timeout must not raise a second proposal: the
        // server keys on this, and two proposals for one confirmation is
        // exactly the confusion the queue exists to prevent.
        newIdempotencyKey(),
      );
      created = res.proposal.proposal_id;
    });
    if (ok && created) onCreated(created, t("approvals.proposalRaised"));
  }

  const selected = items.find((i) => i.name === tool);

  return (
    <Card title={t("approvals.raiseTitle")}>
      <p className="muted">{t("approvals.raiseDetail")}</p>
      <LoadError
        error={catalog.error}
        status={catalog.errorStatus}
        onRetry={catalog.reload}
      />
      {catalog.loading ? (
        <SkeletonRows rows={5} />
      ) : items.length === 0 ? (
        // The catalog is filtered by what this caller may propose, and the
        // write grants are narrow - a support agent holds none. Saying so beats
        // an empty dropdown the operator has to interpret.
        <EmptyState message={t("approvals.noTools")} />
      ) : (
        <>
          <label className="field">
            {t("approvals.tool")}
            <select value={tool} onChange={(e) => pick(e.target.value)}>
              <option value="">{t("approvals.toolPick")}</option>
              {items.map((i) => (
                <option key={i.name} value={i.name}>
                  {i.name} · {t(`proposal.risk.${i.risk}` as DictKey)}
                </option>
              ))}
            </select>
          </label>
          {selected ? (
            <p className="muted">
              {selected.requires_confirmation
                ? t("approvals.needsApproval")
                : t("approvals.noApprovalNeeded")}
            </p>
          ) : null}
          <label className="field">
            {t("approvals.raiseArgs")}
            <textarea rows={8} value={args} onChange={(e) => setArgs(e.target.value)} />
          </label>
          <div className="row-actions">
            <button
              className="btn btn-primary"
              onClick={() => void submit()}
              disabled={action.busy}
            >
              {t("approvals.raiseSubmit")}
            </button>
          </div>
        </>
      )}
      <ActionFeedback error={action.error} notice={action.notice} />
    </Card>
  );
}


export function Approvals() {
  const { t } = useLang();
  const [status, setStatus] = useState<string>("authorized");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(
    () => new URLSearchParams(window.location.search).get("proposal_id"),
  );
  const [raising, setRaising] = useState(false);
  // Bumped on a timer so the "time left" on a proposal counts down instead of
  // freezing at whatever it read when the page loaded.
  const [, setClockTick] = useState(0);

  useEffect(() => {
    const id = window.setInterval(() => setClockTick((n) => n + 1), 30_000);
    return () => window.clearInterval(id);
  }, []);

  const query = `limit=${PAGE_SIZE}&offset=${offset}${status ? `&status=${status}` : ""}`;
  const list = useAsync<{ items: ToolProposal[]; total: number }>(
    () => apiGet<{ items: ToolProposal[]; total: number }>(`/v1/tool-proposals?${query}`),
    [status, offset],
  );
  // The count that answers "what is waiting on me", fetched separately so it
  // stays visible while the operator is looking at executed history. One
  // `limit=1` call: the envelope's `total` is the number, and the screen that
  // needs it most is the one where that filter is not applied.
  const waiting = useAsync<{ total: number }>(
    () => apiGet<{ total: number }>("/v1/tool-proposals?limit=1&status=authorized"),
    [],
  );
  const detail = useAsync<ProposalDetail>(
    () =>
      selected
        ? apiGet<ProposalDetail>(`/v1/tool-proposals/${selected}`)
        : Promise.resolve(null as unknown as ProposalDetail),
    [selected],
  );
  const action = useAction();
  const prompt = usePrompt();

  function refresh() {
    list.reload();
    waiting.reload();
    detail.reload();
  }

  async function approve(p: ToolProposal) {
    const ok = await prompt.confirm(
      t("approvals.approveTitle"),
      t("approvals.approve"),
      t("approvals.approveDetail"),
    );
    if (!ok) return;
    const ran = await action.run(async () => {
      await apiPost(`/v1/tool-proposals/${p.proposal_id}/confirm`, {});
    }, t("approvals.approved"));
    if (ran) refresh();
  }

  async function execute(p: ToolProposal) {
    const ok = await prompt.confirm(
      t("approvals.executeTitle"),
      t("approvals.execute"),
      t("approvals.executeDetail"),
    );
    if (!ok) return;
    // Captured from the response rather than assumed: `run` reports that the
    // HTTP call succeeded, which is not the same question as whether the
    // write happened.
    let verification: string | null = null;
    const ran = await action.run(async () => {
      const res = await apiPost<{ execution: ToolProposalExecution }>(
        `/v1/tool-proposals/${p.proposal_id}/execute`,
        {},
        newIdempotencyKey(),
      );
      verification = res.execution.verification_status;
    });
    if (!ran) return;
    if (verification === "verified") action.succeed(t("approvals.executed"));
    else if (verification === "unknown") action.fail(t("approvals.executedUnverified"));
    else action.fail(t("approvals.executedFailed"));
    refresh();
  }

  async function reconcile(
    p: ToolProposal,
    decision: ToolProposalReconciliation["decision"],
    evidenceReference: string,
  ) {
    const promptKey = {
      applied: "approvals.reconcileAppliedDetail",
      not_applied: "approvals.reconcileNotAppliedDetail",
      unresolved: "approvals.reconcileUnresolvedDetail",
    }[decision] as DictKey;
    const ok = await prompt.confirm(
      t("approvals.reconcileTitle"),
      t("approvals.reconcileConfirm"),
      t(promptKey),
    );
    if (!ok) return;
    const ran = await action.run(
      async () => {
        await apiPost(
          `/v1/tool-proposals/${p.proposal_id}/reconcile`,
          { decision, evidence_reference: evidenceReference.trim() },
          newIdempotencyKey(),
        );
      },
      t("approvals.reconcileRecorded"),
    );
    if (ran) refresh();
  }

  async function compensate(p: ToolProposal, reasonCode: ToolCompensationReason) {
    const ok = await prompt.confirm(
      t("approvals.compensateTitle"),
      t("approvals.compensate"),
      t("approvals.compensateDetail"),
    );
    if (!ok) return;
    let outcome: ToolProposalCompensation["outcome"] | null = null;
    let errorCode: string | null = null;
    const ran = await action.run(async () => {
      const response = await apiPost<{ compensation: ToolProposalCompensation }>(
        `/v1/tool-proposals/${p.proposal_id}/compensate`,
        { reason_code: reasonCode },
        newIdempotencyKey(),
      );
      outcome = response.compensation.outcome;
      const code = response.compensation.result.error_code;
      errorCode = typeof code === "string" ? code : null;
    });
    if (!ran) return;
    if (outcome === "succeeded") action.succeed(t("approvals.compensationSucceeded"));
    else action.fail(t("approvals.compensationFailed", { code: errorCode ?? "unknown" }));
    refresh();
  }

  const waitingCount = waiting.data?.total ?? 0;

  return (
    <div className="page">
      <PageHeader title={t("approvals.title")} subtitle={t("approvals.subtitle")} />

      <div className="stat-grid-sm">
        <Stat
          label={t("approvals.waiting")}
          value={waitingCount}
          tone={waitingCount > 0 ? "warn" : "good"}
        />
      </div>

      <ActionFeedback error={action.error} notice={action.notice} />
      <LoadError error={list.error} status={list.errorStatus} onRetry={list.reload} />

      {/* Behind a toggle: this page's job is the queue, and a form sitting
          above it by default would push the thing the operator came for below
          the fold. */}
      <div className="row-actions">
        <button className="btn" onClick={() => setRaising((v) => !v)}>
          {raising ? t("approvals.raiseHide") : t("approvals.raise")}
        </button>
      </div>
      {raising ? (
        <ProposePanel
          onCreated={(proposalId, message) => {
            setRaising(false);
            // Show the thing that was just created rather than leaving the
            // operator to hunt for it in the list they were already on.
            setStatus("authorized");
            setOffset(0);
            setSelected(proposalId);
            action.succeed(message);
            refresh();
          }}
        />
      ) : null}

      <Card title={t("approvals.queue")}>
        <label className="field">
          {t("approvals.filter")}
          <select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              // A new filter is a new result set; keeping the old offset lands
              // the operator on an empty page, which reads as "nothing here".
              setOffset(0);
            }}
          >
            <option value="">{t("approvals.filter.all")}</option>
            {FILTERS.map((value) => (
              <option key={value} value={value}>
                {t(`proposal.status.${value}` as DictKey)}
              </option>
            ))}
          </select>
        </label>

        {list.loading && !list.data ? (
          <SkeletonRows rows={5} label={t("approvals.loading")} />
        ) : null}
        {list.data && list.data.items.length === 0 ? (
          <EmptyState
            message={status === "authorized" ? t("approvals.empty") : t("approvals.emptyFiltered")}
          />
        ) : null}

        {list.data && list.data.items.length > 0 ? (
          <ul className="case-list">
            {list.data.items.map((p) => {
              const left = minutesLeft(p.expires_at);
              return (
                <li key={p.proposal_id}>
                  <button
                    className={`case-row${selected === p.proposal_id ? " active" : ""}`}
                    onClick={() => setSelected(p.proposal_id)}
                  >
                    <span className="case-subject">{p.tool_name ?? p.proposal_id}</span>
                    <span className="case-meta">
                      <Badge tone={riskTone(p.risk)}>
                        {t(`proposal.risk.${p.risk ?? "read"}` as DictKey)}
                      </Badge>
                      <Badge tone={statusTone(p.effective_status)}>
                        {t(`proposal.status.${p.effective_status}` as DictKey)}
                      </Badge>
                      {p.effective_status === "authorized" && left !== null ? (
                        <span className="muted">
                          {t("approvals.expiresIn", { minutes: left })}
                        </span>
                      ) : null}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        ) : null}
        <ListTotal shown={list.data?.items.length ?? 0} total={list.data?.total ?? 0} />
        <Pagination
          offset={offset}
          limit={PAGE_SIZE}
          total={list.data?.total ?? 0}
          busy={list.loading}
          onChange={setOffset}
        />
      </Card>

      {prompt.element}

      {!selected ? (
        <EmptyState message={t("approvals.select")} />
      ) : detail.loading && !detail.data ? (
        <SkeletonRows rows={4} label={t("approvals.loading")} />
      ) : detail.error ? (
        <ErrorBanner message={detail.error} onRetry={detail.reload} />
      ) : detail.data ? (
        <ProposalPanel
          detail={detail.data}
          busy={action.busy}
          onApprove={approve}
          onExecute={execute}
          onReconcile={reconcile}
          onCompensate={compensate}
          onClose={() => setSelected(null)}
        />
      ) : null}
    </div>
  );
}

function ProposalPanel({
  detail,
  busy,
  onApprove,
  onExecute,
  onReconcile,
  onCompensate,
  onClose,
}: {
  detail: ProposalDetail;
  busy: boolean;
  onApprove: (p: ToolProposal) => void;
  onExecute: (p: ToolProposal) => void;
  onReconcile: (
    p: ToolProposal,
    decision: ToolProposalReconciliation["decision"],
    evidenceReference: string,
  ) => void;
  onCompensate: (p: ToolProposal, reasonCode: ToolCompensationReason) => void;
  onClose: () => void;
}) {
  const { t } = useLang();
  const [evidenceReference, setEvidenceReference] = useState("");
  const [compensationReason, setCompensationReason] =
    useState<ToolCompensationReason>("created_in_error");
  const p = detail.proposal;
  const left = minutesLeft(p.expires_at);
  const approvable = p.effective_status === "authorized" && p.required_confirmation;
  // A proposal the catalog marks as needing no confirmation is executable
  // directly - that risk class exists precisely to run unattended, and a
  // human raising one in the console should not have to approve their own
  // request to themselves.
  const executable =
    p.effective_status === "confirmed" ||
    (p.effective_status === "authorized" && !p.required_confirmation);
  const latest = detail.executions[0] ?? null;

  return (
    <Card title={p.tool_name ?? t("approvals.detail")}>
      <div className="toolbar wrap">
        <Badge tone={riskTone(p.risk)}>{t(`proposal.risk.${p.risk ?? "read"}` as DictKey)}</Badge>
        <Badge tone={statusTone(p.effective_status)}>
          {t(`proposal.status.${p.effective_status}` as DictKey)}
        </Badge>
        {/* Shown only when it is not already implied by the risk class.
            `confirmed_write` is *defined* as needing approval, so a second
            badge saying so is noise; the case worth flagging is a low-risk
            tool that asks for approval anyway. */}
        {confirmationIsNotable(p) ? (
          <Badge tone={p.required_confirmation ? "warn" : "neutral"}>
            {p.required_confirmation
              ? t("approvals.needsApproval")
              : t("approvals.noApprovalNeeded")}
          </Badge>
        ) : null}
        <button className="btn btn-ghost" onClick={onClose}>
          {t("common.close")}
        </button>
      </div>

      <ul className="kv">
        <li>
          <span>{t("approvals.tool")}</span>
          <span>
            {p.tool_name ?? "—"}
            {p.tool_version !== null ? ` v${p.tool_version}` : ""}
          </span>
        </li>
        <li>
          <span>{t("approvals.permission")}</span>
          <span>
            {p.permission_decision}
            {p.permission_reason ? ` (${p.permission_reason})` : ""}
          </span>
        </li>
        <li>
          <span>{t("approvals.expiresLabel")}</span>
          <span>
            {left !== null
              ? t("approvals.expiresIn", { minutes: left })
              : dateFromEpochSeconds(p.expires_at)}
          </span>
        </li>
        <li>
          <span>{t("approvals.actionHash")}</span>
          {/* Truncated for reading, full value in the title: the hash is the
              thing an approval binds to, so it has to be inspectable. */}
          <span className="cell-code" title={p.action_hash}>
            {p.action_hash.slice(0, 16)}…
          </span>
        </li>
      </ul>

      <h3 className="section-title">{t("approvals.arguments")}</h3>
      <pre className="code-block">{JSON.stringify(p.arguments, null, 2)}</pre>

      <div className="toolbar wrap">
        <button className="btn" disabled={!approvable || busy} onClick={() => onApprove(p)}>
          {t("approvals.approve")}
        </button>
        <button className="btn" disabled={!executable || busy} onClick={() => onExecute(p)}>
          {t("approvals.execute")}
        </button>
      </div>

      {latest ? (
        <>
          <h3 className="section-title">{t("approvals.output")}</h3>
          <div className="toolbar wrap">
            <Badge tone={statusTone(latest.status)}>
              {t(`proposal.status.${latest.status}` as DictKey)}
            </Badge>
            {/* The honest field. `executed` means the call returned; only
                `verified` means the postcondition was confirmed, and
                `unknown` means it could not be determined at all. */}
            <Badge tone={statusTone(latest.verification_status ?? "unknown")}>
              {t(`proposal.status.${latest.verification_status ?? "unknown"}` as DictKey)}
            </Badge>
            {latest.error_code ? (
              <Badge tone="bad">
                {t("approvals.errorCode")}: {latest.error_code}
              </Badge>
            ) : null}
          </div>
          <pre className="code-block">{JSON.stringify(latest.output ?? {}, null, 2)}</pre>
        </>
      ) : null}

      {latest && ["executing", "unknown"].includes(latest.status) ? (
        <section>
          <h3 className="section-title">{t("approvals.reconcileTitle")}</h3>
          <p className="muted">{t("approvals.reconcileHelp")}</p>
          <label className="field">
            {t("approvals.evidenceReference")}
            <input
              value={evidenceReference}
              onChange={(event) => setEvidenceReference(event.target.value)}
              maxLength={255}
              autoComplete="off"
            />
          </label>
          <div className="toolbar wrap">
            <button
              className="btn"
              disabled={busy || !evidenceReference.trim()}
              onClick={() => onReconcile(p, "applied", evidenceReference)}
            >
              {t("approvals.reconcileApplied")}
            </button>
            <button
              className="btn"
              disabled={busy || !evidenceReference.trim()}
              onClick={() => onReconcile(p, "not_applied", evidenceReference)}
            >
              {t("approvals.reconcileNotApplied")}
            </button>
            <button
              className="btn"
              disabled={busy || !evidenceReference.trim()}
              onClick={() => onReconcile(p, "unresolved", evidenceReference)}
            >
              {t("approvals.reconcileUnresolved")}
            </button>
          </div>
        </section>
      ) : null}

      {detail.reconciliations.length > 0 ? (
        <>
          <h3 className="section-title">{t("approvals.reconciliationHistory")}</h3>
          <ul className="kv">
            {detail.reconciliations.map((row) => (
              <li key={row.reconciliation_id}>
                <span>
                  {t(`approvals.reconcileDecision.${row.decision}` as DictKey)}
                </span>
                <span className="cell-code">{row.evidence_reference}</span>
              </li>
            ))}
          </ul>
        </>
      ) : null}

      {p.tool_name === "case.create" &&
      latest?.status === "executed" &&
      latest.verification_status === "verified" &&
      detail.compensations.length === 0 ? (
        <section>
          <h3 className="section-title">{t("approvals.compensationTitle")}</h3>
          <p className="muted">{t("approvals.compensationHelp")}</p>
          <label className="field">
            {t("approvals.compensationReason")}
            <select
              value={compensationReason}
              onChange={(event) =>
                setCompensationReason(event.target.value as ToolCompensationReason)
              }
            >
              <option value="created_in_error">{t("approvals.compensationReason.createdInError")}</option>
              <option value="duplicate_case">{t("approvals.compensationReason.duplicateCase")}</option>
              <option value="incorrect_customer">{t("approvals.compensationReason.incorrectCustomer")}</option>
            </select>
          </label>
          <button
            className="btn"
            disabled={busy}
            onClick={() => onCompensate(p, compensationReason)}
          >
            {t("approvals.compensate")}
          </button>
        </section>
      ) : null}

      {detail.compensations.length > 0 ? (
        <>
          <h3 className="section-title">{t("approvals.compensationHistory")}</h3>
          <ul className="kv">
            {detail.compensations.map((row) => (
              <li key={row.compensation_id}>
                <span>{t(`approvals.compensationOutcome.${row.outcome}` as DictKey)}</span>
                <span>
                  {t(`approvals.compensationReason.${camelReason(row.reason_code)}` as DictKey)}
                  {typeof row.result.error_code === "string" ? ` · ${row.result.error_code}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </Card>
  );
}

function camelReason(reason: ToolCompensationReason): string {
  if (reason === "duplicate_case") return "duplicateCase";
  if (reason === "incorrect_customer") return "incorrectCustomer";
  return "createdInError";
}
