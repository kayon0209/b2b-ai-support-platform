import { useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { useAsync } from "../lib/useAsync";
import type { Draft, Gap, GapStats } from "../lib/types";
import { Badge, Card, EmptyState, ErrorBanner, PageHeader, Spinner } from "../components/ui";
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

  async function act(path: string, body?: unknown) {
    try {
      await apiPost(path, body);
    } catch (err) {
      alert(`Action failed: ${err instanceof Error ? err.message : String(err)}`);
    }
    gaps.reload();
    drafts.reload();
    stats.reload();
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
                        onClick={() => {
                          const reason = window.prompt("Why dismiss this gap?");
                          if (reason) act(`/v1/knowledge/gaps/${g.id}/dismiss`, { reason });
                        }}
                      >
                        Dismiss
                      </button>
                      <button
                        className="btn"
                        disabled={g.status === "resolved"}
                        onClick={() => {
                          const title = window.prompt("Draft title");
                          if (!title) return;
                          const body = window.prompt("Draft answer");
                          if (body) act(`/v1/knowledge/gaps/${g.id}/drafts`, { title, body });
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
                        onClick={() => {
                          const notes = window.prompt("Review notes (optional)") ?? "";
                          act(`/v1/knowledge/drafts/${d.id}/review`, {
                            approve: true,
                            notes,
                          });
                        }}
                      >
                        Approve
                      </button>
                      <button
                        className="btn"
                        disabled={d.status !== "pending"}
                        onClick={() => {
                          const notes = window.prompt("Reason for rejection") ?? "";
                          act(`/v1/knowledge/drafts/${d.id}/review`, {
                            approve: false,
                            notes,
                          });
                        }}
                      >
                        Reject
                      </button>
                      <button
                        className="btn"
                        disabled={d.status !== "approved"}
                        onClick={() => {
                          const space_id = window.prompt("Target knowledge space id");
                          if (space_id)
                            act(`/v1/knowledge/drafts/${d.id}/publish`, {
                              space_id,
                              version_label: "v1",
                            });
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
