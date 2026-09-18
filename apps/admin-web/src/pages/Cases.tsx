import { useState } from "react";
import { apiGet, apiPost } from "../lib/api";
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
} from "../components/ui";
import { dateFromEpochSeconds } from "../lib/format";

const PRIORITIES = ["p0", "p1", "p2", "p3"];
const STATUSES = [
  "new",
  "open",
  "pending_customer",
  "escalated",
  "resolved",
  "closed",
];

function idem() {
  return (crypto.randomUUID?.() ?? `idem-${Date.now()}-${Math.random()}`).toString();
}

function slaTone(due: number | null, done: number | null): "good" | "warn" | "bad" | "neutral" {
  if (due === null) return "neutral";
  if (done !== null) return done <= due ? "good" : "bad";
  const remaining = due - Math.floor(Date.now() / 1000);
  if (remaining < 0) return "bad";
  if (remaining < 3600) return "warn";
  return "good";
}

export function Cases() {
  const [selected, setSelected] = useState<string | null>(null);
  const list = useAsync<{ items: Case[]; total: number }>(
    () => apiGet<{ items: Case[]; total: number }>(`/v1/cases?limit=100`),
    [],
  );
  const detail = useAsync<{ case: Case }>(
    () => apiGet<{ case: Case }>(`/v1/cases/${selected}`),
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
          idem(),
        );
      },
      `Command "${command}" applied.`,
    );
    if (ok) {
      detail.reload();
      list.reload();
    }
  }

  return (
    <div className="page">
      <PageHeader
        title="Cases & SLA"
        subtitle="Tenant-scoped support cases with SLA clocks."
      />

      <ActionFeedback error={action.error} notice={action.notice} />
      {list.error ? <ErrorBanner message={list.error} onRetry={list.reload} /> : null}
      {list.loading ? <Spinner label="Loading cases…" /> : null}
      {list.data && list.data.items.length === 0 ? (
        <EmptyState message="No cases yet." />
      ) : null}

      <div className="grid-case">
        <Card title="Case list">
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
                        {c.status}
                      </Badge>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </Card>

        <div>
          {!selected ? (
            <EmptyState message="Select a case to see details and SLA." />
          ) : detail.loading ? (
            <Spinner label="Loading case…" />
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
  return (
    <Card title={c.subject}>
      <div className="toolbar">
        <Badge tone="info">{c.priority}</Badge>
        <Badge tone={c.status === "resolved" || c.status === "closed" ? "good" : "warn"}>
          {c.status}
        </Badge>
        <button className="btn btn-ghost" onClick={onClose}>
          Close
        </button>
      </div>

      <ul className="kv">
        <li>
          <span>Category</span>
          <span>{c.category}</span>
        </li>
        <li>
          <span>Version</span>
          <span>{c.version}</span>
        </li>
        <li>
          <span>Assignee</span>
          <span>{c.assignee_ref ?? "—"}</span>
        </li>
        <li>
          <span>Team</span>
          <span>{c.team_ref ?? "—"}</span>
        </li>
        <li>
          <span>Opened</span>
          <span>{dateFromEpochSeconds(c.opened_at)}</span>
        </li>
        <li>
          <span>First response due</span>
          <Badge tone={slaTone(c.first_response_due_at, c.first_responded_at)}>
            {dateFromEpochSeconds(c.first_response_due_at)}
          </Badge>
        </li>
        <li>
          <span>Resolution due</span>
          <Badge tone={slaTone(c.resolution_due_at, c.resolved_at)}>
            {dateFromEpochSeconds(c.resolution_due_at)}
          </Badge>
        </li>
        <li>
          <span>First responded</span>
          <span>{dateFromEpochSeconds(c.first_responded_at)}</span>
        </li>
        <li>
          <span>Resolved</span>
          <span>{dateFromEpochSeconds(c.resolved_at)}</span>
        </li>
      </ul>

      <h3 className="section-title">Commands</h3>
      <div className="toolbar wrap">
        <button className="btn" onClick={() => onCommand("record_first_response")}>
          Record first response
        </button>
        <button
          className="btn"
          onClick={() => {
            const target = window.prompt(`Target status: ${STATUSES.join(", ")}`);
            if (target) onCommand("transition", { target });
          }}
        >
          Transition…
        </button>
        <button
          className="btn"
          onClick={() => {
            const priority = window.prompt(`Priority: ${PRIORITIES.join(", ")}`);
            if (priority) onCommand("change_priority", { priority });
          }}
        >
          Change priority…
        </button>
        <button
          className="btn"
          onClick={() => {
            const assignee_ref = window.prompt("Assignee ref (optional)");
            const team_ref = window.prompt("Team ref (optional)");
            onCommand("assign", { assignee_ref, team_ref });
          }}
        >
          Assign…
        </button>
      </div>
    </Card>
  );
}
