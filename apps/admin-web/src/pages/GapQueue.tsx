import { useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { useSearchParams } from "react-router-dom";

import { newIdempotencyKey } from "../lib/idempotency";
import { useAction } from "../lib/useAction";
import { useAsync } from "../lib/useAsync";
import type { Draft, Gap, GapStats } from "../lib/types";
import {
  ActionFeedback,
  Badge,
  Card,
  EmptyState,
  PageHeader,
  ListTotal,
  Spinner,
} from "../components/ui";
import { LoadError } from "../components/LoadError";
import { usePrompt } from "../components/Prompt";
import { useLang, type DictKey } from "../lib/i18n";
import { dateFromEpochSeconds, int } from "../lib/format";

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
  const { t } = useLang();
  // The tab is part of the view, so it lives in the address: a refresh
  // keeps the reviewer on the drafts list rather than bouncing them back to
  // the gap queue.
  const [searchParams, setSearchParams] = useSearchParams();
  const tab: "gaps" | "drafts" = searchParams.get("tab") === "drafts" ? "drafts" : "gaps";

  function setTab(next: "gaps" | "drafts") {
    const params = new URLSearchParams(searchParams);
    if (next === "gaps") params.delete("tab");
    else params.set("tab", next);
    setSearchParams(params);
  }
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
  const spaces = useAsync<{ items: { id: string; name: string }[]; total: number }>(
    () => apiGet<{ items: { id: string; name: string }[]; total: number }>(
      `/v1/knowledge/spaces`,
    ),
    [],
  );
  const action = useAction();
  const prompt = usePrompt();

  async function act(path: string, body?: unknown, okMsg?: string) {
    const ok = await action.run(
      () => apiPost(path, body, newIdempotencyKey()).then(() => undefined),
      // Every write here used to run with no success message: a gap that
      // stayed on screen after "Dismiss" (its new status keeps it in the
      // list) looked exactly like a click that had done nothing.
      okMsg,
    );
    if (ok) {
      gaps.reload();
      drafts.reload();
      stats.reload();
    }
    return ok;
  }

  return (
    <div className="page">
      <PageHeader
        title={t("gaps.title")}
        subtitle={t("gaps.subtitle")}
        actions={
          <div className="segmented">
            <button
              className={`segment${tab === "gaps" ? " active" : ""}`}
              onClick={() => setTab("gaps")}
            >
              {t("gaps.tabGaps")}
            </button>
            <button
              className={`segment${tab === "drafts" ? " active" : ""}`}
              onClick={() => setTab("drafts")}
            >
              {t("gaps.tabDrafts")}
            </button>
          </div>
        }
      />

      <ActionFeedback error={action.error} notice={action.notice} />
      {prompt.element}

      {stats.data ? (
        <div className="stat-grid stat-grid-sm">
          <Card>
            <StatLite label={t("gaps.statTotal")} value={int(stats.data.total_gaps)} />
          </Card>
          <Card>
            <StatLite
              label={t("gaps.statOccurrences")}
              value={int(stats.data.total_occurrences)}
            />
          </Card>
          {Object.entries(stats.data.by_status).map(([statusKey, count]) => (
            <Card key={statusKey}>
              <StatLite
                label={t(`gaps.status.${statusKey}` as DictKey)}
                value={int(count)}
              />
            </Card>
          ))}
        </div>
      ) : null}

      {tab === "gaps" ? (
        <>
          <div className="toolbar">
            <label className="field">
              <span>{t("gaps.statusFilter")}</span>
              <select value={status} onChange={(e) => setStatus(e.target.value)}>
                {STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {t(`gaps.status.${s}` as DictKey)}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <LoadError error={gaps.error} status={gaps.errorStatus} onRetry={gaps.reload} />
          {gaps.loading ? <Spinner label={t("gaps.loadingGaps")} /> : null}
          {gaps.data && gaps.data.items.length === 0 ? (
            <EmptyState message={t("gaps.emptyGaps")} />
          ) : null}

          {gaps.data && gaps.data.items.length > 0 ? (
            <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">{t("gaps.headerSample")}</th>
                  <th scope="col">{t("gaps.headerReason")}</th>
                  <th scope="col" className="num">{t("gaps.headerFreq")}</th>
                  <th scope="col">{t("common.status")}</th>
                  <th scope="col">{t("gaps.headerLastSeen")}</th>
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
                      <Badge tone={toneForStatus(g.status)}>
                        {t(`gaps.status.${g.status}` as DictKey)}
                      </Badge>
                    </td>
                    <td className="muted">{dateFromEpochSeconds(g.last_seen_at)}</td>
                    <td className="row-actions">
                      <button
                        className="btn"
                        disabled={action.busy || g.status !== "open"}
                        onClick={() =>
                          act(
                            `/v1/knowledge/gaps/${g.id}/acknowledge`,
                            undefined,
                            t("gaps.claimed"),
                          )
                        }
                      >
                        {t("gaps.claim")}
                      </button>
                      <button
                        className="btn"
                        disabled={action.busy || g.status === "resolved" || g.status === "dismissed"}
                        onClick={async () => {
                          const values = await prompt.ask({
                            title: t("gaps.dismissTitle"),
                            confirmLabel: t("gaps.dismiss"),
                            detail: t("gaps.dismissDetail"),
                            fields: [
                              { name: "reason", label: t("gaps.reasonLabel"), required: true },
                            ],
                          });
                          if (values)
                            act(
                              `/v1/knowledge/gaps/${g.id}/dismiss`,
                              { reason: values.reason },
                              t("gaps.dismissed"),
                            );
                        }}
                      >
                        {t("gaps.dismiss")}
                      </button>
                      <button
                        className="btn"
                        disabled={action.busy || g.status === "resolved"}
                        onClick={async () => {
                          const values = await prompt.ask({
                            title: t("gaps.draftAnswerTitle"),
                            confirmLabel: t("prompts.createDraft"),
                            fields: [
                              {
                                name: "title",
                                label: t("gaps.draftTitleLabel"),
                                required: true,
                              },
                              {
                                name: "body",
                                label: t("gaps.draftBodyLabel"),
                                required: true,
                              },
                            ],
                          });
                          if (values) {
                            act(
                              `/v1/knowledge/gaps/${g.id}/drafts`,
                              { title: values.title, body: values.body },
                              t("gaps.draftCreated"),
                            );
                          }
                        }}
                      >
                        {t("gaps.draft")}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          ) : null}
          <ListTotal shown={gaps.data?.items.length ?? 0} total={gaps.data?.total ?? 0} />
        </>
      ) : (
        <>
          <LoadError error={drafts.error} status={drafts.errorStatus} onRetry={drafts.reload} />
          {drafts.loading ? <Spinner label={t("gaps.loadingDrafts")} /> : null}
          {drafts.data && drafts.data.items.length === 0 ? (
            <EmptyState message={t("gaps.emptyDrafts")} />
          ) : null}
          {drafts.data && drafts.data.items.length > 0 ? (
            <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">{t("gaps.headerTitle")}</th>
                  <th scope="col">{t("common.status")}</th>
                  <th scope="col">{t("gaps.headerReviewer")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {drafts.data.items.map((d) => (
                  <tr key={d.id}>
                    <td className="cell-strong">{d.title}</td>
                    <td>
                      <Badge tone={d.status === "approved" ? "good" : "info"}>
                        {t(`gaps.status.${d.status}` as DictKey)}
                      </Badge>
                    </td>
                    <td className="muted">{d.reviewed_by ?? "—"}</td>
                    <td className="row-actions">
                      <button
                        className="btn"
                        disabled={action.busy || d.status !== "pending"}
                        onClick={async () => {
                          const values = await prompt.ask({
                            title: t("gaps.approveTitle", { title: d.title }),
                            confirmLabel: t("gaps.approve"),
                            detail: t("gaps.approveDetail"),
                            fields: [
                              {
                                name: "notes",
                                label: t("gaps.notesLabel"),
                                placeholder: t("gaps.notesOptional"),
                              },
                            ],
                          });
                          if (values) {
                            act(
                              `/v1/knowledge/drafts/${d.id}/review`,
                              { approve: true, notes: values.notes ?? "" },
                              t("gaps.approved"),
                            );
                          }
                        }}
                      >
                        {t("gaps.approve")}
                      </button>
                      <button
                        className="btn"
                        disabled={action.busy || d.status !== "pending"}
                        onClick={async () => {
                          const values = await prompt.ask({
                            title: t("gaps.rejectTitle", { title: d.title }),
                            confirmLabel: t("gaps.reject"),
                            fields: [{ name: "notes", label: t("gaps.rejectNotesLabel") }],
                          });
                          if (values) {
                            act(
                              `/v1/knowledge/drafts/${d.id}/review`,
                              { approve: false, notes: values.notes ?? "" },
                              t("gaps.rejected"),
                            );
                          }
                        }}
                      >
                        {t("gaps.reject")}
                      </button>
                      <button
                        className="btn"
                        disabled={action.busy || d.status !== "approved"}
                        onClick={async () => {
                          const spaceItems = spaces.data?.items ?? [];
                          const values = await prompt.ask({
                            title: t("gaps.publishTitle", { title: d.title }),
                            confirmLabel: t("gaps.publish"),
                            detail: spaces.error
                              ? t("gaps.spacesFailed")
                              : spaceItems.length === 0
                                ? t("gaps.spaceHint")
                                : undefined,
                            fields: [
                              {
                                name: "space_id",
                                label: t("gaps.spaceLabel"),
                                // Value is the id: names are not unique, and
                                // resolving name -> id afterwards could pick
                                // the wrong space or none at all.
                                options: spaceItems.map((sp) => ({
                                  value: sp.id,
                                  label: sp.name,
                                })),
                                required: true,
                              },
                            ],
                          });
                          if (values?.space_id) {
                            act(
                              `/v1/knowledge/drafts/${d.id}/publish`,
                              { space_id: values.space_id, version_label: "v1" },
                              t("gaps.published"),
                            );
                          }
                        }}
                      >
                        {t("gaps.publish")}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          ) : null}
          {/* Publishing needs the space list. If that read failed, say so
              rather than opening a picker that is empty for the wrong
              reason — an empty list and a failed list look identical. */}
          {spaces.error ? <p className="muted">{t("gaps.spacesFailed")}</p> : null}
          <ListTotal
            shown={drafts.data?.items.length ?? 0}
            total={drafts.data?.total ?? 0}
          />
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
