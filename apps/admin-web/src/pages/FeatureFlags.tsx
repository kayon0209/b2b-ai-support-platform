import { useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { useAction } from "../lib/useAction";
import { useAsync } from "../lib/useAsync";
import type { FeatureFlag } from "../lib/types";
import {
  ActionFeedback,
  Badge,
  Card,
  ErrorBanner,
  PageHeader,
  Spinner,
} from "../components/ui";
import { dateFromEpochSeconds } from "../lib/format";

export function FeatureFlags() {
  const [newKey, setNewKey] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const action = useAction();

  const flags = useAsync<{ items: FeatureFlag[]; total: number }>(
    () => apiGet<{ items: FeatureFlag[]; total: number }>(`/v1/flags?limit=100`),
    [],
  );

  async function act(path: string, body?: unknown): Promise<boolean> {
    const ok = await action.run(() => apiPost(path, body).then(() => undefined));
    if (ok) flags.reload();
    return ok;
  }

  return (
    <div className="page">
      <PageHeader
        title="Feature Flags"
        subtitle="Deterministic, canary-gated rollout of platform capabilities."
      />

      <Card title="Define a flag">
        <div className="toolbar">
          <input
            className="text-input"
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
            placeholder="flag_key"
          />
          <input
            className="text-input"
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
            placeholder="description"
          />
          <button
            className="btn btn-primary"
            disabled={!newKey.trim() || action.busy}
            onClick={() => {
              if (!newKey.trim()) return;
              // Clear the form only on success: wiping what someone typed
              // because the request failed makes them retype it blind.
              void act("/v1/flags", {
                key: newKey.trim(),
                description: newDesc.trim(),
              }).then((ok) => {
                if (ok) {
                  setNewKey("");
                  setNewDesc("");
                }
              });
            }}
          >
            Define
          </button>
        </div>
      </Card>

      <ActionFeedback error={action.error} notice={action.notice} />
      {flags.error ? <ErrorBanner message={flags.error} onRetry={flags.reload} /> : null}
      {flags.loading ? <Spinner label="Loading flags…" /> : null}
      {flags.data && flags.data.items.length === 0 ? (
        <p className="muted">No feature flags defined.</p>
      ) : null}

      {flags.data && flags.data.items.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th>Key</th>
              <th>Description</th>
              <th className="num">Rollout</th>
              <th>State</th>
              <th>Created</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {flags.data.items.map((f) => (
              <tr key={f.key}>
                <td className="cell-strong">{f.key}</td>
                <td className="muted">{f.description || "—"}</td>
                <td className="num">
                  <span className="rollout">{f.rollout_percent}%</span>
                </td>
                <td>
                  <Badge tone={f.enabled ? "good" : "neutral"}>
                    {f.enabled ? "enabled" : "disabled"}
                  </Badge>
                </td>
                <td className="muted">{dateFromEpochSeconds(f.created_at)}</td>
                <td className="row-actions">
                  <button
                    className="btn"
                    onClick={() =>
                      act(`/v1/flags/${f.key}/enabled`, { enabled: !f.enabled })
                    }
                  >
                    {f.enabled ? "Disable" : "Enable"}
                  </button>
                  <button
                    className="btn"
                    onClick={() => {
                      const pctRaw = window.prompt("Rollout percent (0-100)", String(f.rollout_percent));
                      if (pctRaw === null) return;
                      const pctNum = Number(pctRaw);
                      if (Number.isNaN(pctNum) || pctNum < 0 || pctNum > 100) {
                        action.fail("Enter a number between 0 and 100.");
                        return;
                      }
                      void act(`/v1/flags/${f.key}/rollout`, { rollout_percent: pctNum });
                    }}
                  >
                    Set rollout
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}
