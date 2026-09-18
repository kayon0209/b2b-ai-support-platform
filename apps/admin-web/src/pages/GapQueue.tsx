import { useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { useAction } from "../lib/useAction";
import { useAsync } from "../lib/useAsync";
import type { Draft, Gap, GapStats } from "../lib/types";
import {
  ActionFeedback,
  Badge,
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  Spinner,
} from "../components/ui";
import { usePrompt } from "../components/Prompt";
import { dateFromEpochSeconds, int, titleCase } from "../lib/format";

const STATUSES = ["open", "acknowledged", "drafted", "resolved", "dismissed"];

function toneForStatus(status: string): "neutral" | "info" | "warn" | "good" | "bad" {
  switch (status) {
    case "open":
      // "open" means nobody has claimed the gap, so it is rendered as the
      // loudest tone. The return type has to include "bad" for that to be
      // expressible at all.
      return "bad";
    case "acknowledged":
      return "warn";
    case "drafted":
      return "info";
    case "resolved":
      return "good";
    default:
      return "neutral";
  }
}

export function GapQueue() {
  const [tab, setTab] = useState<"gaps" | "drafts">("gaps");
  const [status, setStatus] = useState<string>("open");

  const gaps = useAsync<{ items: Gap[]; total: number }>(
    () =>
      apiGet<{ items: Gap[]; total: number }>(
        `/v1/knowledge/gaps?status=${status}&limit=100`,
      ),
    [status],
  );
  const drafts = useAsync<{ items: Draft[]; total: number }>(
    () => apiGet<{ items: Draft[]; total: number }>(`/v1/knowledge/gaps/drafts?limit=100`),
    [],
  );
  const stats = useAsync<GapStats>(() => apiGet<GapStats>(`/v1/knowledge/gaps/stats`), []);
  const action = useAction();
  const prompt = usePrompt();

  async function act(path: string, body?: unknown) {
    const ok = await action.run(() => apiPost(path, body).then(() => undefined));
    if (ok) {
      gaps.reload();
      drafts.reload();
      stats.reload();
    }
  }

  return (
    <div className="page">
      <PageHeader
        title="Knowledge Gap Queue"
        subtitle="Questions the bot could not answer, most-demanded first."
        actions={
          <div className="segmented">
            <button
              className={`segment${tab === "gaps" ? " active" : ""}`}
              onClick={() => setTab("gaps")}
            >
              Gaps
            </button>
            <button
              className={`segment${tab === "drafts" ? " active" : ""}`}
              onClick={() => setTab("drafts")}
            >
              Drafts
            </button>
          </div>
        }
      />

      <ActionFeedback error={action.error} notice={action.notice} />
      {prompt.element}

      {stats.data ? (
        <div className="stat-grid stat-grid-sm">
          {Object.entries(stats.data).map(([k, v]) => (
            <Card key={k}>
              <StatLite label={titleCase(k)} value={int(v)} />
            </Card>
          ))}
        </div>
      ) : null}

      {tab === "gaps" ? (
        <>
          <div className="toolbar">
            <label className="field">
              <span>Status</span>
              <select value={status} onChange={(e) => setStatus(e.target.value)}>
                {STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {titleCase(s)}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {gaps.error ? <ErrorBanner message={gaps.error} onRetry={gaps.reload} /> : null}
          {gaps.loading ? <Spinner label="Loading gaps…" /> : null}
          {gaps.data && gaps.data.items.length === 0 ? (
            <EmptyState message="No gaps in this state." />
          ) : null}

          {gaps.data && gaps.data.items.length > 0 ? (
            <table className="table">
              <thead>
                <tr>
                  <th>Sample question</th>
                  <th>Reason</th>
                  <th className="num">Freq</th>
                  <th>Status</th>
                  <th>Last seen</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {gaps.data.items.map((g) => (
                  <tr key={g.id}>
                    <td className="cell-strong">{g.sample_question}</td>
                    <td>
                      <Badge tone="info">{g.reason_code}</Badge>
                    </td>
                    <td className="num">{int(g.frequency)}</td>
                    <td>
                      <Badge tone={toneForStatus(g.status)}>{titleCase(g.status)}</Badge>
                    </td>
                    <td className="muted">{dateFromEpochSeconds(g.last_seen_at)}</td>
                    <td className="row-actions">
                      <button
                        className="btn"
                        disabled={g.status !== "open"}
                        onClick={() => act(`/v1/knowledge/gaps/${g.id}/acknowledge`)}
                      >
                        Claim
                      </button>
                      <button
                        className="btn"
                        disabled={g.status === "resolved" || g.status === "dismissed"}
                        onClick={async () => {
                          const values = await prompt.ask({
                            title: "Dismiss this gap",
                            confirmLabel: "Dismiss",
                            detail: "The reason is recorded on the gap.",
                            fields: [{ name: "reason", label: "Reason", required: true }],
                          });
                          if (values) act(`/v1/knowledge/gaps/${g.id}/dismiss`, { reason: values.reason });
                        }}
                      >
                        Dismiss
                      </button>
                      <button
                        className="btn"
                        disabled={g.status === "resolved"}
                        onClick={async () => {
                          const values = await prompt.ask({
                            title: "Draft an answer for this gap",
                            confirmLabel: "Create draft",
                            fields: [
                              { name: "title", label: "Draft title", required: true },
                              { name: "body", label: "Draft answer", required: true },
                            ],
                          });
                          if (values) {
                            act(`/v1/knowledge/gaps/${g.id}/drafts`, {
                              title: values.title,
                              body: values.body,
                            });
                          }
                        }}
                      >
                        Draft
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </>
      ) : (
        <>
          {drafts.error ? <ErrorBanner message={drafts.error} onRetry={drafts.reload} /> : null}
          {drafts.loading ? <Spinner label="Loading drafts…" /> : null}
          {drafts.data && drafts.data.items.length === 0 ? (
            <EmptyState message="No drafts yet." />
          ) : null}

          {drafts.data && drafts.data.items.length > 0 ? (
            <table className="table">
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Status</th>
                  <th>Reviewer</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {drafts.data.items.map((d) => (
                  <tr key={d.id}>
                    <td className="cell-strong">{d.title}</td>
                    <td>
                      <Badge tone={d.status === "approved" ? "good" : "info"}>
                        {titleCase(d.status)}
                      </Badge>
                    </td>
                    <td className="muted">{d.reviewed_by ?? "—"}</td>
                    <td className="row-actions">
                      <button
                        className="btn"
                        disabled={d.status !== "pending"}
                        onClick={async () => {
                          const values = await prompt.ask({
                            title: `Approve “${d.title}”`,
                            confirmLabel: "Approve",
                            detail: "Approving publishes this draft to the review queue.",
                            fields: [
                              { name: "notes", label: "Review notes", placeholder: "optional" },
                            ],
                          });
                          if (values) {
                            act(`/v1/knowledge/drafts/${d.id}/review`, {
                              approve: true,
                              notes: values.notes ?? "",
                            });
                          }
                        }}
                      >
                        Approve
                      </button>
                      <button
                        className="btn"
                        disabled={d.status !== "pending"}
                        onClick={async () => {
                          const values = await prompt.ask({
                            title: `Reject “${d.title}”`,
                            confirmLabel: "Reject",
                            fields: [{ name: "notes", label: "Reason for rejection" }],
                          });
                          if (values) {
                            act(`/v1/knowledge/drafts/${d.id}/review`, {
                              approve: false,
                              notes: values.notes ?? "",
                            });
                          }
                        }}
                      >
                        Reject
                      </button>
                      <button
                        className="btn"
                        disabled={d.status !== "approved"}
                        onClick={async () => {
                          const values = await prompt.ask({
                            title: `Publish “${d.title}”`,
                            confirmLabel: "Publish",
                            fields: [
                              {
                                name: "space_id",
                                label: "Target knowledge space id",
                                required: true,
                              },
                            ],
                          });
                          if (values) {
                            act(`/v1/knowledge/drafts/${d.id}/publish`, {
                              space_id: values.space_id,
                              version_label: "v1",
                            });
                          }
                        }}
                      >
                        Publish
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </>
      )}
    </div>
  );
}

function StatLite({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}
