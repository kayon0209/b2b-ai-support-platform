import { useState } from "react";
import { apiGet } from "../lib/api";
import { useAsync } from "../lib/useAsync";
import type { QualityMetrics, RouteDistribution } from "../lib/types";
import { Card, ErrorBanner, PageHeader, Spinner, Stat, Badge } from "../components/ui";
import { int, ms, pct } from "../lib/format";

const WINDOWS: { label: string; seconds: number }[] = [
  { label: "Last 1h", seconds: 3600 },
  { label: "Last 24h", seconds: 86400 },
  { label: "Last 30d", seconds: 2_592_000 },
];

function toneFor(rate: number, warnAbove: number, badAbove: number) {
  if (rate >= badAbove) return "bad" as const;
  if (rate >= warnAbove) return "warn" as const;
  return "good" as const;
}

export function QualityDashboard() {
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
        title="Quality Dashboard"
        subtitle="Agent-run outcomes for your tenant, over the selected window."
        actions={
          <div className="segmented">
            {WINDOWS.map((w) => (
              <button
                key={w.seconds}
                className={`segment${w.seconds === window ? " active" : ""}`}
                onClick={() => setWindow(w.seconds)}
              >
                {w.label}
              </button>
            ))}
          </div>
        }
      />

      {metrics.error ? <ErrorBanner message={metrics.error} onRetry={metrics.reload} /> : null}
      {routes.error ? <ErrorBanner message={routes.error} onRetry={routes.reload} /> : null}

      {metrics.loading ? <Spinner label="Loading quality metrics…" /> : null}

      {metrics.data ? (
        <>
          <div className="stat-grid">
            <Card>
              <Stat label="Total runs" value={int(metrics.data.total_runs)} />
            </Card>
            <Card>
              <Stat
                label="Completed"
                value={int(metrics.data.completed)}
                tone="good"
              />
            </Card>
            <Card>
              <Stat
                label="Abstention rate"
                value={pct(metrics.data.abstention_rate)}
                tone={toneFor(metrics.data.abstention_rate, 0.1, 0.25)}
              />
            </Card>
            <Card>
              <Stat
                label="Handoff rate"
                value={pct(metrics.data.handoff_rate)}
                tone={toneFor(metrics.data.handoff_rate, 0.1, 0.25)}
              />
            </Card>
            <Card>
              <Stat
                label="Citation coverage"
                value={pct(metrics.data.citation_coverage)}
                tone={
                  metrics.data.citation_coverage !== null &&
                  metrics.data.citation_coverage < 0.8
                    ? "warn"
                    : "good"
                }
              />
            </Card>
            <Card>
              <Stat label="Failed" value={int(metrics.data.failed)} tone="bad" />
            </Card>
            <Card>
              <Stat label="Latency P50" value={ms(metrics.data.latency_p50_ms)} />
            </Card>
            <Card>
              <Stat label="Latency P95" value={ms(metrics.data.latency_p95_ms)} />
            </Card>
            <Card>
              <Stat
                label="Supported resolution"
                value={pct(metrics.data.supported_resolution_rate)}
                tone={toneFor(1 - metrics.data.supported_resolution_rate, 0.1, 0.25)}
              />
            </Card>
            <Card>
              <Stat
                label="Wrong resolution"
                value={pct(metrics.data.wrong_resolution_rate)}
                tone={toneFor(metrics.data.wrong_resolution_rate, 0.05, 0.15)}
              />
            </Card>
          </div>

          <div className="grid-2">
            <Card title="Resolution outcomes">
              {metrics.data.cases_measured > 0 ? (
                <ul className="kv">
                  <li>
                    <span>Cases measured</span>
                    <span>{int(metrics.data.cases_measured)}</span>
                  </li>
                  <li>
                    <span>Resolution held</span>
                    <span className="text-good">{int(metrics.data.supported_resolution)}</span>
                  </li>
                  <li>
                    <span>Reopened after resolving</span>
                    <span className="text-bad">{int(metrics.data.wrong_resolution)}</span>
                  </li>
                </ul>
              ) : (
                <p className="muted">
                  No Case was resolved or reopened in this window, so there is no resolution
                  outcome to report yet.
                </p>
              )}
              <p className="muted">
                Derived from Cases, not runs: a Case that was resolved and never reopened is a
                resolution that held; one that was reopened is a resolution that did not. Open
                cases ({int(metrics.data.open_cases)}) are excluded so the rate does not move
                with backlog.
              </p>
            </Card>

            <Card title="Outcome mix">
              <ul className="kv">
                <li>
                  <span>Completed</span>
                  <span>{int(metrics.data.completed)}</span>
                </li>
                <li>
                  <span>Abstained</span>
                  <span>{int(metrics.data.abstained)}</span>
                </li>
                <li>
                  <span>Handed off</span>
                  <span>{int(metrics.data.handed_off)}</span>
                </li>
                <li>
                  <span>Failed</span>
                  <span>{int(metrics.data.failed)}</span>
                </li>
                <li>
                  <span>Untimed runs</span>
                  <span>{int(metrics.data.untimed_runs)}</span>
                </li>
              </ul>
            </Card>

            <Card title="Route distribution">
              {routes.data ? (
                routes.data.route_counts &&
                Object.keys(routes.data.route_counts).length > 0 ? (
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Route</th>
                        <th className="num">Runs</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(routes.data.route_counts).map(([k, v]) => (
                        <tr key={k}>
                          <td>
                            <Badge tone="info">{k}</Badge>
                          </td>
                          <td className="num">{int(v)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <p className="muted">No routed runs in this window.</p>
                )
              ) : routes.loading ? (
                <Spinner />
              ) : (
                <p className="muted">Route data unavailable.</p>
              )}
            </Card>
          </div>
        </>
      ) : null}
    </div>
  );
}
