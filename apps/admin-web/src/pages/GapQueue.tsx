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
  SkeletonRows,
} from "../components/ui";
import { LoadError } from "../components/LoadError";
import { usePrompt } from "../components/Prompt";
import { useLang, type DictKey } from "../lib/i18n";
import { dateFromEpochSeconds, int } from "../lib/format";

const STATUSES = ["open", "acknowledged", "drafted", "resolved", "dismissed"];

interface ReleaseEvaluationSummary {
  evaluation_id: string;
  candidate_version_id: string;
  candidate_fingerprint: string;
  status: "eligible" | "blocked";
  reason_code: string;
  approval_count: number;
  current_user_approved: boolean;
  post_test_status: "passed" | "blocked" | null;
  created_at: number;
}

interface ReleaseEvaluationsResponse {
  items: ReleaseEvaluationSummary[];
  release_gate_enabled: boolean;
  release_evidence_available: boolean;
  can_approve: boolean;
}

const RELEASE_REASON_LABELS: Record<string, readonly [string, string]> = {
  EVALUATOR_PROVENANCE_UNAVAILABLE: ["评测来源证明尚未生成", "Evaluator provenance is not available"],
  EVALUATOR_PROVENANCE_INVALID: ["评测来源证明无效", "Evaluator provenance is invalid"],
  EVAL_DATASET_NOT_APPROVED: ["评测集尚未获批", "The evaluation dataset is not approved"],
  EVALUATION_BLOCKED: ["评测未达到发布条件", "The evaluation did not meet release criteria"],
  RELEASE_GATE_DISABLED: ["知识发布门禁尚未启用", "The knowledge release gate is disabled"],
  RELEASE_CANDIDATE_MOVED: ["候选内容已变化，需要重新评测", "The candidate changed and must be evaluated again"],
  POST_TEST_RELEASE_NOT_ACTIVE: ["发布后验证时版本已不再生效", "The release was no longer active during post-test"],
};

function releaseReasonLabel(reasonCode: string, lang: "en" | "zh"): string {
  const labels = RELEASE_REASON_LABELS[reasonCode];
  if (labels) return lang === "zh" ? labels[0] : labels[1];
  return lang === "zh" ? "尚有发布条件未满足" : "A release condition is not satisfied";
}

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
  const { t, lang } = useLang();
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
  const [releaseDraftId, setReleaseDraftId] = useState<string | null>(null);

  const gaps = useAsync<{ items: Gap[]; total: number }>(
    () =>
      apiGet<{ items: Gap[]; total: number }>(
        `/v1/knowledge/gaps?status=${status}&limit=100`,
      ),
    [status],
  );
  const drafts = useAsync<{
    items: Draft[];
    total: number;
    release_gate_enabled: boolean;
    release_evidence_available: boolean;
  }>(
    () => apiGet(`/v1/knowledge/gaps/drafts?limit=100`),
    [],
  );
  const releaseEvaluations = useAsync<ReleaseEvaluationsResponse>(
    () => releaseDraftId
      ? apiGet(`/v1/knowledge/drafts/${releaseDraftId}/release-evaluations`)
      : Promise.resolve({ items: [], release_gate_enabled: false, release_evidence_available: false, can_approve: false }),
    [releaseDraftId],
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
      releaseEvaluations.reload();
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
          {gaps.loading ? <SkeletonRows rows={5} label={t("gaps.loadingGaps")} /> : null}
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
          {drafts.loading ? <SkeletonRows rows={5} label={t("gaps.loadingDrafts")} /> : null}
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
                      <Badge tone={d.published_document_id || d.status === "approved" ? "good" : "info"}>
                        {t((d.published_document_id ? "gaps.status.published" : `gaps.status.${d.status}`) as DictKey)}
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
                        disabled={action.busy || d.status !== "approved" || d.published_document_id !== null || (drafts.data?.release_gate_enabled && !drafts.data?.release_evidence_available)}
                        onClick={async () => {
                          const spaceItems = spaces.data?.items ?? [];
                          const releaseGateEnabled = drafts.data?.release_gate_enabled ?? false;
                          let evaluationOptions: ReleaseEvaluationSummary[] = [];
                          if (releaseGateEnabled) {
                            try {
                              const evidence = await apiGet<ReleaseEvaluationsResponse>(
                                `/v1/knowledge/drafts/${d.id}/release-evaluations`,
                              );
                              evaluationOptions = evidence.items.filter(
                                (item) => item.status === "eligible" && item.approval_count >= 2,
                              );
                            } catch (reason) {
                              await action.run(() => Promise.reject(reason));
                              return;
                            }
                            if (evaluationOptions.length === 0) {
                              setReleaseDraftId(d.id);
                              await action.run(
                                () => Promise.reject(new Error(t("gaps.releaseEvalRequired"))),
                              );
                              return;
                            }
                          }
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
                              ...(releaseGateEnabled ? [{
                                name: "release_evaluation_id",
                                label: t("gaps.releaseEvaluationLabel"),
                                options: evaluationOptions.map((item) => ({
                                  value: item.evaluation_id,
                                  label: `${item.candidate_fingerprint.slice(0, 12)} · ${item.approval_count}/2`,
                                })),
                                required: true,
                              }] : []),
                            ],
                          });
                          if (values?.space_id) {
                            const body: Record<string, string> = {
                              space_id: values.space_id,
                              version_label: "v1",
                            };
                            if (releaseGateEnabled) {
                              body.release_evaluation_id = values.release_evaluation_id;
                            }
                            act(
                              `/v1/knowledge/drafts/${d.id}/publish`,
                              body,
                              t("gaps.published"),
                            );
                          }
                        }}
                      >
                        {t(d.published_document_id ? "gaps.alreadyPublished" : "gaps.publish")}
                      </button>
                      <button
                        className="btn"
                        disabled={d.status !== "approved"}
                        aria-pressed={releaseDraftId === d.id}
                        onClick={() => setReleaseDraftId(d.id)}
                      >
                        {t("gaps.releaseEvidence")}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          ) : null}
          {releaseDraftId ? <Card>
            <div className="release-evidence-head">
              <div>
                <strong>{t("gaps.releaseEvidence")}</strong>
                <p className="muted">
                  {drafts.data?.items.find((item) => item.id === releaseDraftId)?.title ?? releaseDraftId}
                </p>
              </div>
              <button className="btn" type="button" onClick={() => setReleaseDraftId(null)}>
                {t("common.close")}
              </button>
            </div>
            <p className="muted">
              {t(releaseEvaluations.data?.release_gate_enabled
                ? releaseEvaluations.data?.release_evidence_available
                  ? "gaps.releaseGateEnabled"
                  : "gaps.releaseEvidenceUnavailable"
                : "gaps.releaseGateDisabled")}
            </p>
            <LoadError
              error={releaseEvaluations.error}
              status={releaseEvaluations.errorStatus}
              onRetry={releaseEvaluations.reload}
            />
            {releaseEvaluations.loading ? (
              <SkeletonRows rows={3} label={t("gaps.loadingReleaseEvidence")} />
            ) : null}
            {releaseEvaluations.data?.items.length === 0 ? <p className="muted">{t("gaps.noReleaseEvidence")}</p> : null}
            {releaseEvaluations.data?.items.map((item) => (
              <div className="release-evidence-row" key={item.evaluation_id}>
                <div>
                  <strong>{item.status === "eligible" ? t("gaps.releaseCandidateEligible") : t("gaps.releaseCandidateBlocked")}</strong>
                  <p className="muted" title={`${lang === "zh" ? "诊断代码" : "Diagnostic code"}: ${item.reason_code}`}>
                    {releaseReasonLabel(item.reason_code, lang)} · {item.candidate_fingerprint.slice(0, 16)}
                  </p>
                  <p className="muted">{t("gaps.releaseApprovedCount", { count: item.approval_count })}</p>
                  {item.post_test_status ? <p className="muted">{t(item.post_test_status === "passed" ? "gaps.releasePostTestPassed" : "gaps.releasePostTestBlocked")}</p> : null}
                </div>
                <div className="release-evidence-actions">
                  <button
                    className="btn"
                    disabled={action.busy || !releaseEvaluations.data?.release_gate_enabled || !releaseEvaluations.data?.release_evidence_available || !releaseEvaluations.data?.can_approve || item.status !== "eligible" || item.current_user_approved || item.approval_count >= 2}
                    onClick={() => act(
                      `/v1/knowledge/drafts/${releaseDraftId}/release-evaluations/${item.evaluation_id}/approve`,
                      undefined,
                      t("gaps.releaseApproved"),
                    )}
                  >
                    {item.current_user_approved ? t("gaps.releaseAlreadyApproved") : t("gaps.releaseApprove")}
                  </button>
                  {item.post_test_status === "blocked" ? <button
                    className="btn"
                    disabled={action.busy || !releaseEvaluations.data?.can_approve}
                    onClick={async () => {
                      const confirmed = await prompt.ask({
                        title: t("gaps.rollbackReleaseTitle"),
                        confirmLabel: t("gaps.rollbackRelease"),
                        detail: t("gaps.rollbackReleaseDetail"),
                      });
                      if (confirmed) {
                        act(
                          `/v1/knowledge/releases/${item.evaluation_id}/rollback`,
                          { reason_code: "post_test_failed" },
                          t("gaps.rollbackReleaseDone"),
                        );
                      }
                    }}
                  >
                    {t("gaps.rollbackRelease")}
                  </button> : null}
                </div>
              </div>
            ))}
          </Card> : null}
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
