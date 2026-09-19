import { useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useAction } from "../lib/useAction";
import { useAsync } from "../lib/useAsync";
import type { Case } from "../lib/types";
import {
  ActionFeedback,
  Badge,
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  Spinner,
  ListTotal,
} from "../components/ui";
import { LoadError } from "../components/LoadError";
import { Pagination } from "../components/Pagination";
import { usePrompt } from "../components/Prompt";
import { useLang, type DictKey } from "../lib/i18n";
import { dateFromEpochSeconds } from "../lib/format";

const PAGE_SIZE = 50;

const PRIORITIES = ["p0", "p1", "p2", "p3"];
// Mirrors platform_core.cases.models.CaseStatus exactly. The transition
// legality itself stays server-side (docs/domain-model.md case states);
// offering a status the backend does not know turns the dropdown into an
// error generator instead of a control.
const STATUSES = [
  "new",
  "triaged",
  "in_progress",
  "waiting_customer",
  "waiting_internal",
  "waiting_vendor",
  "resolved",
  "closed",
  "reopened",
];

function slaTone(due: number | null, done: number | null): "good" | "warn" | "bad" | "neutral" {
  if (due === null) return "neutral";
  if (done !== null) return done <= due ? "good" : "bad";
  const remaining = due - Math.floor(Date.now() / 1000);
  if (remaining < 0) return "bad";
  if (remaining < 3600) return "warn";
  return "good";
}

export function Cases() {
  const { t } = useLang();
  const [selected, setSelected] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  // SLA badges are "time until due", so they go stale on their own. A minute
  // is coarse enough not to churn renders and fine enough that a case does
  // not sit showing green after it has breached.
  const [, setClockTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setClockTick((n) => n + 1), 60_000);
    return () => window.clearInterval(id);
  }, []);

  const list = useAsync<{ items: Case[]; total: number }>(
    () =>
      apiGet<{ items: Case[]; total: number }>(
        `/v1/cases?limit=${PAGE_SIZE}&offset=${offset}`,
      ),
    [offset],
  );
  // No case is selected on mount (and none after "Close"), and the loader
  // used to run anyway against `/v1/cases/null`, which the API answers with
  // a 400 on every visit and again on every close.
  const detail = useAsync<{ case: Case }>(
    () =>
      selected
        ? apiGet<{ case: Case }>(`/v1/cases/${selected}`)
        : Promise.resolve({ case: null as unknown as Case }),
    [selected],
  );
  const action = useAction();

  async function command(command: string, parameters: Record<string, unknown> = {}) {
    if (!selected) return;
    const ok = await action.run(
      async () => {
        await apiPost(
          `/v1/cases/${selected}/commands`,
          { command, parameters, reason: "" },
          newIdempotencyKey(),
        );
      },
      t("cases.commandApplied", { command }),
    );
    if (ok) {
      detail.reload();
      list.reload();
    }
  }

  return (
    <div className="page">
      <PageHeader
        title={t("cases.title")}
        subtitle={t("cases.subtitle")}
      />

      <ActionFeedback error={action.error} notice={action.notice} />
      <LoadError error={list.error} status={list.errorStatus} onRetry={list.reload} />
      {list.loading ? <Spinner label={t("cases.loadingCases")} /> : null}
      {list.data && list.data.items.length === 0 ? (
        <EmptyState message={t("cases.emptyList")} />
      ) : null}

      <div className="grid-case">
        <Card title={t("cases.caseList")}>
          {list.data && list.data.items.length > 0 ? (
            <ul className="case-list">
              {list.data.items.map((c) => (
                <li key={c.case_id}>
                  <button
                    className={`case-row${selected === c.case_id ? " active" : ""}`}
                    onClick={() => setSelected(c.case_id)}
                  >
                    <span className="case-subject">{c.subject}</span>
                    <span className="case-meta">
                      <Badge tone="info">{c.priority}</Badge>
                      <Badge tone={c.status === "resolved" || c.status === "closed" ? "good" : "warn"}>
                        {t(`case.status.${c.status}` as DictKey)}
                      </Badge>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          {/* Inside the card, not a sibling of it: `.grid-case` is two
              columns, so a third child took the detail column and pushed
              the detail panel onto a second row. */}
          <ListTotal shown={list.data?.items.length ?? 0} total={list.data?.total ?? 0} />
          <Pagination
            offset={offset}
            limit={PAGE_SIZE}
            total={list.data?.total ?? 0}
            busy={list.loading}
            onChange={setOffset}
          />
        </Card>

        <div>
          {!selected ? (
            <EmptyState message={t("cases.selectCase")} />
          ) : detail.loading ? (
            <Spinner label={t("cases.loadingCase")} />
          ) : detail.error ? (
            <ErrorBanner message={detail.error} onRetry={detail.reload} />
          ) : detail.data ? (
            <CaseDetail
              c={detail.data.case}
              onCommand={command}
              onClose={() => setSelected(null)}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}

function CaseDetail({
  c,
  onCommand,
  onClose,
}: {
  c: Case;
  onCommand: (command: string, parameters?: Record<string, unknown>) => void;
  onClose: () => void;
}) {
  const { t } = useLang();
  const prompt = usePrompt();
  return (
    <Card title={c.subject}>
      <div className="toolbar">
        <Badge tone="info">{c.priority}</Badge>
        <Badge tone={c.status === "resolved" || c.status === "closed" ? "good" : "warn"}>
          {t(`case.status.${c.status}` as DictKey)}
        </Badge>
        <button className="btn btn-ghost" onClick={onClose}>
          {t("common.close")}
        </button>
      </div>

      <ul className="kv">
        <li>
          <span>{t("cases.category")}</span>
          <span>{c.category}</span>
        </li>
        <li>
          <span>{t("common.version")}</span>
          <span>{c.version}</span>
        </li>
        <li>
          <span>{t("cases.assignee")}</span>
          <span>{c.assignee_ref ?? "—"}</span>
        </li>
        <li>
          <span>{t("cases.team")}</span>
          <span>{c.team_ref ?? "—"}</span>
        </li>
        <li>
          <span>{t("cases.opened")}</span>
          <span>{dateFromEpochSeconds(c.opened_at)}</span>
        </li>
        <li>
          <span>{t("cases.firstResponseDue")}</span>
          <Badge tone={slaTone(c.first_response_due_at, c.first_responded_at)}>
            {dateFromEpochSeconds(c.first_response_due_at)}
          </Badge>
        </li>
        <li>
          <span>{t("cases.resolutionDue")}</span>
          <Badge tone={slaTone(c.resolution_due_at, c.resolved_at)}>
            {dateFromEpochSeconds(c.resolution_due_at)}
          </Badge>
        </li>
        <li>
          <span>{t("cases.firstResponded")}</span>
          <span>{dateFromEpochSeconds(c.first_responded_at)}</span>
        </li>
        <li>
          <span>{t("cases.resolvedAt")}</span>
          <span>{dateFromEpochSeconds(c.resolved_at)}</span>
        </li>
      </ul>

      <h3 className="section-title">{t("cases.commands")}</h3>
      {prompt.element}
      <div className="toolbar wrap">
        {/* Stops a clock the SLA is measured against, and cannot be undone —
            so it confirms like every other write here, instead of firing on
            a single unguarded click. */}
        <button
          className="btn"
          onClick={async () => {
            const ok = await prompt.confirm(
              t("cases.recordFirstResponseConfirm"),
              t("cases.recordFirstResponse"),
              t("cases.recordFirstResponseDetail"),
            );
            if (ok) onCommand("record_first_response");
          }}
        >
          {t("cases.recordFirstResponse")}
        </button>
        <button
          className="btn"
          onClick={async () => {
            const values = await prompt.ask({
              title: t("cases.transitionTitle"),
              confirmLabel: "Transition",
              fields: [
                { name: "target", label: t("cases.targetLabel"), options: STATUSES, required: true },
              ],
            });
            if (values) onCommand("transition", { target: values.target });
          }}
        >
          {t("cases.transition")}
        </button>
        <button
          className="btn"
          onClick={async () => {
            const values = await prompt.ask({
              title: t("cases.priorityTitle"),
              confirmLabel: "Change",
              fields: [
                { name: "priority", label: t("cases.priorityLabel"), options: PRIORITIES, required: true },
              ],
            });
            if (values) onCommand("change_priority", { priority: values.priority });
          }}
        >
          {t("cases.changePriority")}
        </button>
        <button
          className="btn"
          onClick={async () => {
            const values = await prompt.ask({
              title: t("cases.assignTitle"),
              confirmLabel: "Assign",
              detail: t("cases.assignDetail"),
              fields: [
                { name: "assignee_ref", label: t("cases.assigneeRef"), placeholder: t("cases.optional") },
                { name: "team_ref", label: t("cases.teamRef"), placeholder: t("cases.optional") },
              ],
            });
            if (values) {
              onCommand("assign", {
                assignee_ref: values.assignee_ref || undefined,
                team_ref: values.team_ref || undefined,
              });
            }
          }}
        >
          {t("cases.assign")}
        </button>
      </div>
    </Card>
  );
}
