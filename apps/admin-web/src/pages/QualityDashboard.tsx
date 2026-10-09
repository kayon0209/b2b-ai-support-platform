import { useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useAsync } from "../lib/useAsync";
import type { CsatSummary, QualityMetrics, RouteDistribution } from "../lib/types";
import { Card, EmptyState, PageHeader, SkeletonRows, Stat, Badge } from "../components/ui";
import { LoadError } from "../components/LoadError";
import { useLang } from "../lib/i18n";
import { int, ms, pct } from "../lib/format";

interface CustomerOutcomeSummary {
  confirmation_requested: number;
  customer_confirmed: number;
  customer_rejected: number;
  customer_no_response: number;
  awaiting_confirmation: number;
  explicit_confirmation_rate_of_requests: number | null;
  same_issue_recontact_rate: number | null;
  recontact_linkage_status: string;
}

type ReviewReasonCode =
  | "unsupported_claim"
  | "wrong_route"
  | "citation_gap"
  | "unsafe_action"
  | "task_outcome_mismatch"
  | "other";

interface ReviewItem {
  agent_run_id: string;
  conversation_ref_id: string;
  stratum: string;
  route: string;
  run_status: string;
  prompt_version_id: string | null;
  code_version: string;
  policy_version: string;
  reviewed: boolean;
  verdict: "agree" | "override" | null;
  reason_code: ReviewReasonCode | null;
}

interface ReviewBatch {
  batch_id: string;
  window_seconds: number;
  requested_size: number;
  target_prompt_version_id: string | null;
  population_by_stratum: Record<string, number>;
  sampler_version: string;
  summary: {
    status: "measured" | "incomplete" | "unavailable";
    selected_count: number;
    reviewed_count: number;
    completion_rate: number | null;
    weighted_override_rate: number | null;
  };
  items: ReviewItem[];
}

interface ReviewEvidence {
  evidence_id: string;
  evidence_hash: string;
  snapshot: Record<string, unknown>;
  replayed: boolean;
}

const REVIEW_REASONS: ReviewReasonCode[] = [
  "unsupported_claim",
  "wrong_route",
  "citation_gap",
  "unsafe_action",
  "task_outcome_mismatch",
  "other",
];

const WINDOWS: { labelKey: "quality.window1h" | "quality.window24h" | "quality.window30d"; seconds: number }[] = [
  { labelKey: "quality.window1h", seconds: 3600 },
  { labelKey: "quality.window24h", seconds: 86_400 },
  { labelKey: "quality.window30d", seconds: 2_592_000 },
];

function toneFor(rate: number, warnAbove: number, badAbove: number) {
  if (rate >= badAbove) return "bad" as const;
  if (rate >= warnAbove) return "warn" as const;
  return "good" as const;
}

export function QualityDashboard() {
  const { t } = useLang();
  const [window, setWindow] = useState(86_400);
  const [reviewBatch, setReviewBatch] = useState<ReviewBatch | null>(null);
  const [reviewEvidence, setReviewEvidence] = useState<ReviewEvidence | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [reviewReasons, setReviewReasons] = useState<Record<string, ReviewReasonCode>>({});
  const [targetPromptVersionId, setTargetPromptVersionId] = useState("");

  const metrics = useAsync<QualityMetrics>(
    () => apiGet<QualityMetrics>(`/v1/quality/metrics?window_seconds=${window}`),
    [window],
  );
  const routes = useAsync<RouteDistribution>(
    () => apiGet<RouteDistribution>(`/v1/quality/routes?window_seconds=${window}`),
    [window],
  );
  // The satisfaction numbers. They were collected by nothing at all until
  // 2026-09-23 - `csat.py` had no production caller - so this is the first time
  // the platform can answer "are customers happy", which is one of the two
  // experience metrics the industry tracks (the other being first-time-fix).
  const csat = useAsync<{ csat: CsatSummary }>(
    () => apiGet<{ csat: CsatSummary }>(`/v1/quality/csat?window_seconds=${window}`),
    [window],
  );
  const outcomes = useAsync<{ outcomes: CustomerOutcomeSummary }>(
    () => apiGet<{ outcomes: CustomerOutcomeSummary }>(`/v1/quality/outcomes?window_seconds=${window}`),
    [window],
  );

  async function createReviewBatch() {
    setReviewBusy(true);
    setReviewError(null);
    setReviewEvidence(null);
    try {
      const batch = await apiPost<{
        batch_id: string;
        window_seconds: number;
        requested_size: number;
        target_prompt_version_id: string | null;
        population_by_stratum: Record<string, number>;
        sampler_version: string;
        selected_count: number;
        items: Array<Omit<ReviewItem, "reviewed" | "verdict" | "reason_code">>;
      }>(
        "/v1/quality/reviews/batches",
        {
          window_seconds: window,
          size: 20,
          ...(targetPromptVersionId.trim()
            ? { target_prompt_version_id: targetPromptVersionId.trim() }
            : {}),
        },
        newIdempotencyKey(),
      );
      setReviewBatch({
        ...batch,
        summary: {
          status: "incomplete",
          selected_count: batch.selected_count,
          reviewed_count: 0,
          completion_rate: batch.selected_count ? 0 : null,
          weighted_override_rate: null,
        },
        items: batch.items.map((item) => ({
          ...item,
          reviewed: false,
          verdict: null,
          reason_code: null,
        })),
      });
    } catch (error) {
      setReviewError(error instanceof Error ? error.message : t("quality.reviewError"));
    } finally {
      setReviewBusy(false);
    }
  }

  async function refreshReviewBatch(batchId: string) {
    const current = await apiGet<ReviewBatch>(`/v1/quality/reviews/batches/${batchId}`);
    setReviewBatch(current);
  }

  async function recordReview(item: ReviewItem, verdict: "agree" | "override") {
    if (!reviewBatch || item.reviewed) return;
    setReviewBusy(true);
    setReviewError(null);
    try {
      await apiPost(
        `/v1/quality/reviews/batches/${reviewBatch.batch_id}/decisions`,
        {
          agent_run_id: item.agent_run_id,
          verdict,
          reason_code: verdict === "override" ? (reviewReasons[item.agent_run_id] ?? "other") : null,
        },
        newIdempotencyKey(),
      );
      await refreshReviewBatch(reviewBatch.batch_id);
    } catch (error) {
      setReviewError(error instanceof Error ? error.message : t("quality.reviewError"));
    } finally {
      setReviewBusy(false);
    }
  }

  async function finalizeReviewBatch() {
    if (!reviewBatch || reviewBatch.summary.status !== "measured") return;
    setReviewBusy(true);
    setReviewError(null);
    try {
      const evidence = await apiPost<ReviewEvidence>(
        `/v1/quality/reviews/batches/${reviewBatch.batch_id}/finalize`,
        undefined,
        newIdempotencyKey(),
      );
      setReviewEvidence(evidence);
    } catch (error) {
      setReviewError(error instanceof Error ? error.message : t("quality.reviewError"));
    } finally {
      setReviewBusy(false);
    }
  }

  return (
    <div className="page">
      <PageHeader
        title={t("quality.title")}
        subtitle={t("quality.subtitle")}
        actions={
          <div className="segmented">
            {WINDOWS.map((w) => (
              <button
                key={w.seconds}
                className={`segment${w.seconds === window ? " active" : ""}`}
                onClick={() => setWindow(w.seconds)}
              >
                {t(w.labelKey)}
              </button>
            ))}
          </div>
        }
      />

      <LoadError error={metrics.error} status={metrics.errorStatus} onRetry={metrics.reload} />
      <LoadError error={routes.error} status={routes.errorStatus} onRetry={routes.reload} />
      <LoadError error={outcomes.error} status={outcomes.errorStatus} onRetry={outcomes.reload} />

      {metrics.loading ? <SkeletonRows rows={4} label={t("quality.loading")} /> : null}

      {/* Zero runs and a broken pipeline render identically as a wall of
          0.0%. Saying which one it is is the difference between "nothing
          happened yet" and "nothing is working". */}
      {metrics.data && metrics.data.total_runs === 0 ? (
        <EmptyState message={t("quality.empty")} />
      ) : null}

      {metrics.data ? (
        <>
          <div className="stat-grid">
            <Card>
              <Stat label={t("quality.totalRuns")} value={int(metrics.data.total_runs)} />
            </Card>
            <Card>
              <Stat
                label={t("quality.completed")}
                value={int(metrics.data.completed)}
                tone="good"
              />
            </Card>
            <Card>
              <Stat
                label={t("quality.abstentionRate")}
                value={pct(metrics.data.abstention_rate)}
                tone={toneFor(metrics.data.abstention_rate, 0.1, 0.25)}
              />
            </Card>
            <Card>
              <Stat
                label={t("quality.handoffRate")}
                value={pct(metrics.data.handoff_rate)}
                tone={toneFor(metrics.data.handoff_rate, 0.1, 0.25)}
              />
            </Card>
            <Card>
              <Stat
                label={t("quality.citationCoverage")}
                value={pct(metrics.data.citation_coverage)}
                tone={metrics.data.citation_coverage < 0.8 ? "warn" : "good"}
              />
            </Card>
            <Card>
              <Stat label={t("quality.failed")} value={int(metrics.data.failed)} tone="bad" />
            </Card>
            <Card>
              <Stat label={t("quality.latencyP50")} value={ms(metrics.data.latency_p50_ms)} />
            </Card>
            <Card>
              <Stat label={t("quality.latencyP95")} value={ms(metrics.data.latency_p95_ms)} />
            </Card>
            <Card>
              <Stat
                label={t("quality.supportedResolution")}
                value={pct(metrics.data.supported_resolution_rate)}
                tone={toneFor(1 - metrics.data.supported_resolution_rate, 0.1, 0.25)}
              />
            </Card>
            <Card>
              <Stat
                label={t("quality.pendingCorrections")}
                value={int(metrics.data.pending_corrections)}
                tone={
                  metrics.data.pending_corrections > 0 ? ("warn" as const) : ("good" as const)
                }
              />
            </Card>
            <Card>
              <Stat
                label={t("quality.wrongResolution")}
                value={pct(metrics.data.wrong_resolution_rate)}
                tone={toneFor(metrics.data.wrong_resolution_rate, 0.05, 0.15)}
              />
            </Card>
            <Card>
              {/* An em dash, not 0 or "-": no responses yet is not a score of
                  zero, and showing 0.00 would read as "everyone is furious". */}
              <Stat
                label={t("quality.csat")}
                value={
                  csat.data?.csat.average != null
                    ? `${csat.data.csat.average.toFixed(2)} / 5`
                    : "—"
                }
                tone={
                  csat.data?.csat.average == null
                    ? undefined
                    : csat.data.csat.average >= 4
                      ? ("good" as const)
                      : csat.data.csat.average >= 3
                        ? ("warn" as const)
                        : ("bad" as const)
                }
              />
            </Card>
            <Card>
              <Stat
                label={t("quality.csatRate")}
                value={
                  csat.data?.csat.response_rate != null
                    ? pct(csat.data.csat.response_rate)
                    : "—"
                }
              />
            </Card>
            <Card>
              <Stat
                label={t("quality.explicitResolutionConfirmation")}
                value={pct(outcomes.data?.outcomes.explicit_confirmation_rate_of_requests)}
              />
            </Card>
            <Card>
              <Stat
                label={t("quality.resolutionQuestionNoResponse")}
                value={int(outcomes.data?.outcomes.customer_no_response)}
              />
            </Card>
            <Card>
              <Stat
                label={t("quality.sameCaseRecontact")}
                value={pct(outcomes.data?.outcomes.same_issue_recontact_rate)}
              />
            </Card>
          </div>

          <div className="grid-2">
            <Card title={t("quality.resolutionOutcomes")}>
              {metrics.data.cases_measured > 0 ? (
                <ul className="kv">
                  <li>
                    <span>{t("quality.casesMeasured")}</span>
                    <span>{int(metrics.data.cases_measured)}</span>
                  </li>
                  <li>
                    <span>{t("quality.resolutionHeld")}</span>
                    <span className="text-good">{int(metrics.data.supported_resolution)}</span>
                  </li>
                  <li>
                    <span>{t("quality.reopened")}</span>
                    <span className="text-bad">{int(metrics.data.wrong_resolution)}</span>
                  </li>
                </ul>
              ) : (
                <p className="muted">{t("quality.noResolutions")}</p>
              )}
              <p className="muted">
                {t("quality.resolutionNote", { open: int(metrics.data.open_cases) })}
              </p>
            </Card>

            <Card title={t("quality.outcomeMix")}>
              <ul className="kv">
                <li>
                  <span>{t("quality.completed")}</span>
                  <span>{int(metrics.data.completed)}</span>
                </li>
                <li>
                  <span>{t("quality.abstained")}</span>
                  <span>{int(metrics.data.abstained)}</span>
                </li>
                <li>
                  <span>{t("quality.handedOff")}</span>
                  <span>{int(metrics.data.handed_off)}</span>
                </li>
                <li>
                  <span>{t("quality.failed")}</span>
                  <span>{int(metrics.data.failed)}</span>
                </li>
                <li>
                  <span>{t("quality.untimedRuns")}</span>
                  <span>{int(metrics.data.untimed_runs)}</span>
                </li>
              </ul>
            </Card>

            <Card title={t("quality.routeDistribution")}>
              {routes.data ? (
                routes.data.route_counts &&
                Object.keys(routes.data.route_counts).length > 0 ? (
                  <div className="table-scroll">
                  <table className="table">
                    <thead>
                      <tr>
                        <th scope="col">{t("quality.route")}</th>
                        <th scope="col" className="num">{t("quality.runs")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(routes.data.route_counts).map(([k, v]) => (
                        <tr key={k}>
                          <td>
                            <Badge tone="info">
                              {t(`route.${k}` as Parameters<typeof t>[0])}
                            </Badge>
                          </td>
                          <td className="num">{int(v)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  </div>
                ) : (
                  <p className="muted">{t("quality.noRouted")}</p>
                )
              ) : routes.loading ? (
                <SkeletonRows rows={3} />
              ) : (
                <p className="muted">{t("quality.routeUnavailable")}</p>
              )}
            </Card>
          </div>

          <div className="grid-2">
            <Card title={t("quality.leaks")}>
              {metrics.data.automation_candidates?.length ? (
                <table className="table">
                  <thead>
                    <tr>
                      <th>{t("quality.reason")}</th>
                      <th>{t("quality.count")}</th>
                      <th>{t("quality.action")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {metrics.data.automation_candidates.map((item) => (
                      <tr key={item.reason}>
                        <td>{item.reason}</td>
                        <td>{int(item.count)}</td>
                        <td>
                          <Badge tone={item.automatable ? "good" : "neutral"}>
                            {item.automatable
                              ? t("quality.automatable")
                              : t("quality.keepHuman")}
                          </Badge>
                          {item.sample_questions.length > 0 ? (
                            <ul className="leak-samples">
                              {item.sample_questions.map((q) => (
                                <li key={q}>{q}</li>
                              ))}
                            </ul>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="muted">{t("quality.noHandoffs")}</p>
              )}
            </Card>
          </div>

          <Card title={t("quality.humanReviewTitle")}>
            <p className="muted">{t("quality.humanReviewDescription")}</p>
            {reviewError ? <p role="alert" className="text-bad">{reviewError}</p> : null}
            {!reviewBatch ? (
              <>
                <label className="field">
                  <span>{t("quality.promptVersionFilter")}</span>
                  <input
                    className="text-input"
                    value={targetPromptVersionId}
                    onChange={(event) => setTargetPromptVersionId(event.target.value)}
                    aria-label={t("quality.promptVersionFilter")}
                    placeholder={t("quality.promptVersionFilterHint")}
                  />
                </label>
                <button className="btn btn-primary" type="button" disabled={reviewBusy} onClick={() => void createReviewBatch()}>
                  {reviewBusy ? t("quality.reviewWorking") : t("quality.createReviewBatch")}
                </button>
              </>
            ) : (
              <>
                <p className="muted">
                  {t("quality.reviewBatchCount", {
                    reviewed: int(reviewBatch.summary.reviewed_count),
                    selected: int(reviewBatch.summary.selected_count),
                  })}
                  {" · "}{t("quality.reviewOverrideRate")}: {pct(reviewBatch.summary.weighted_override_rate)}
                </p>
                {reviewBatch.target_prompt_version_id ? (
                  <p className="muted">
                    {t("quality.promptVersionFilter")}: <code>{reviewBatch.target_prompt_version_id}</code>
                  </p>
                ) : null}
                {reviewBatch.items.map((item) => (
                  <div className="quality-review-row" key={item.agent_run_id}>
                    <a href={`/admin/workbench/conversation/${item.conversation_ref_id}`}>
                      {t("quality.reviewOpenConversation")}
                    </a>
                    <span>{item.route} · {item.run_status} · {item.stratum}</span>
                    {item.reviewed ? (
                      <Badge tone={item.verdict === "override" ? "warn" : "good"}>
                        {item.verdict === "override" ? t("quality.reviewOverride") : t("quality.reviewAgree")}
                      </Badge>
                    ) : (
                      <div className="quality-review-actions">
                        <select
                          aria-label={t("quality.reviewOverrideReason")}
                          value={reviewReasons[item.agent_run_id] ?? "other"}
                          onChange={(event) => setReviewReasons((previous) => ({
                            ...previous,
                            [item.agent_run_id]: event.target.value as ReviewReasonCode,
                          }))}
                        >
                          {REVIEW_REASONS.map((reason) => (
                            <option value={reason} key={reason}>{t(`quality.reviewReason.${reason}`)}</option>
                          ))}
                        </select>
                        <button type="button" className="btn btn-ghost" disabled={reviewBusy} onClick={() => void recordReview(item, "agree")}>
                          {t("quality.reviewAgree")}
                        </button>
                        <button type="button" className="btn btn-primary" disabled={reviewBusy} onClick={() => void recordReview(item, "override")}>
                          {t("quality.reviewOverride")}
                        </button>
                      </div>
                    )}
                  </div>
                ))}
                <div className="quality-review-actions">
                  <button className="btn btn-ghost" type="button" disabled={reviewBusy} onClick={() => void refreshReviewBatch(reviewBatch.batch_id)}>
                    {t("quality.reviewRefresh")}
                  </button>
                  <button className="btn btn-primary" type="button" disabled={reviewBusy || reviewBatch.summary.status !== "measured"} onClick={() => void finalizeReviewBatch()}>
                    {t("quality.reviewFinalize")}
                  </button>
                </div>
                {reviewEvidence ? (
                  <p role="status" className="text-good">
                    {t("quality.reviewEvidenceCreated")}: {reviewEvidence.evidence_hash}
                  </p>
                ) : null}
              </>
            )}
          </Card>
        </>
      ) : null}
    </div>
  );
}
