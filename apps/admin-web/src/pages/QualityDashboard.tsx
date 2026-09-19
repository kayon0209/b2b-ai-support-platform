import { useState } from "react";
import { apiGet } from "../lib/api";
import { useAsync } from "../lib/useAsync";
import type { QualityMetrics, RouteDistribution } from "../lib/types";
import { Card, EmptyState, PageHeader, Spinner, Stat, Badge } from "../components/ui";
import { LoadError } from "../components/LoadError";
import { useLang } from "../lib/i18n";
import { int, ms, pct } from "../lib/format";

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

  const metrics = useAsync<QualityMetrics>(
    () => apiGet<QualityMetrics>(`/v1/quality/metrics?window_seconds=${window}`),
    [window],
  );
  const routes = useAsync<RouteDistribution>(
    () => apiGet<RouteDistribution>(`/v1/quality/routes?window_seconds=${window}`),
    [window],
  );

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

      {metrics.loading ? <Spinner label={t("quality.loading")} /> : null}

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
                label={t("quality.wrongResolution")}
                value={pct(metrics.data.wrong_resolution_rate)}
                tone={toneFor(metrics.data.wrong_resolution_rate, 0.05, 0.15)}
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
                <Spinner />
              ) : (
                <p className="muted">{t("quality.routeUnavailable")}</p>
              )}
            </Card>
          </div>
        </>
      ) : null}
    </div>
  );
}
