import { useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useAction } from "../lib/useAction";
import { usePrompt } from "../components/Prompt";
import { useAsync } from "../lib/useAsync";
import type { FeatureFlag } from "../lib/types";
import {
  ActionFeedback,
  Badge,
  Card,
  EmptyState,
  PageHeader,
  Spinner,
  ListTotal,
} from "../components/ui";
import { LoadError } from "../components/LoadError";
import { useLang } from "../lib/i18n";
import { dateFromEpochSeconds } from "../lib/format";

/**
 * Mirrors the server's own rule (flag_service._validate_key): the key ends
 * up in a URL path, a log line and a metric label, so anything outside this
 * set either breaks the URL or corrupts the label.
 */
const KEY_PATTERN = /^[A-Za-z0-9._-]+$/;

export function FeatureFlags() {
  const { t } = useLang();
  const [newKey, setNewKey] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const action = useAction();
  const prompt = usePrompt();

  const flags = useAsync<{ items: FeatureFlag[]; total: number }>(
    () => apiGet<{ items: FeatureFlag[]; total: number }>(`/v1/flags?limit=200`),
    [],
  );

  async function act(path: string, body?: unknown, okMsg?: string): Promise<boolean> {
    const ok = await action.run(
      () => apiPost(path, body, newIdempotencyKey()).then(() => undefined),
      // Every write used to run with no success message, so a change that
      // landed was indistinguishable from one that never fired.
      okMsg,
    );
    if (ok) flags.reload();
    return ok;
  }

  const trimmedKey = newKey.trim();
  const keyValid = trimmedKey !== "" && KEY_PATTERN.test(trimmedKey);

  return (
    <div className="page">
      <PageHeader title={t("flags.title")} subtitle={t("flags.subtitle")} />

      <Card title={t("flags.defineTitle")}>
        <div className="toolbar">
          <input
            className="text-input"
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
            placeholder={t("flags.keyPlaceholder")}
            pattern="[A-Za-z0-9._-]+"
            aria-invalid={trimmedKey !== "" && !keyValid}
          />
          <input
            className="text-input"
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
            placeholder={t("flags.descPlaceholder")}
          />
          <button
            className="btn btn-primary"
            disabled={!keyValid || action.busy}
            onClick={() => {
              if (!keyValid) return;
              // Clear the form only on success: wiping what someone typed
              // because the request failed makes them retype it blind.
              void act(
                "/v1/flags",
                { key: trimmedKey, description: newDesc.trim() },
                t("flags.defined", { key: trimmedKey }),
              ).then((ok) => {
                if (ok) {
                  setNewKey("");
                  setNewDesc("");
                }
              });
            }}
          >
            {t("flags.define")}
          </button>
        </div>
        {trimmedKey !== "" && !keyValid ? (
          <p className="prompt-error" role="alert">
            {t("flags.invalidKey")}
          </p>
        ) : null}
      </Card>

      <ActionFeedback error={action.error} notice={action.notice} />
      {prompt.element}
      <LoadError error={flags.error} status={flags.errorStatus} onRetry={flags.reload} />
      {flags.loading ? <Spinner label={t("flags.loading")} /> : null}
      {flags.data && flags.data.items.length === 0 ? (
        <EmptyState message={t("flags.empty")} />
      ) : null}

      {flags.data && flags.data.items.length > 0 ? (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">{t("flags.headerKey")}</th>
                <th scope="col">{t("flags.headerDescription")}</th>
                <th scope="col" className="num">
                  {t("flags.headerRollout")}
                </th>
                <th scope="col">{t("flags.headerState")}</th>
                <th scope="col">{t("flags.headerCreated")}</th>
                <th scope="col" />
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
                      {f.enabled ? t("common.enabled") : t("common.disabled")}
                    </Badge>
                  </td>
                  <td className="muted">{dateFromEpochSeconds(f.created_at)}</td>
                  <td className="row-actions">
                    <button
                      className="btn"
                      onClick={() =>
                        // Encoded: the key is interpolated into the path, and
                        // one containing `#`, `?` or `/` silently produced a
                        // request to the wrong endpoint.
                        void act(
                          `/v1/flags/${encodeURIComponent(f.key)}/enabled`,
                          { enabled: !f.enabled },
                          t("flags.stateChanged", {
                            key: f.key,
                            state: f.enabled ? t("common.disabled") : t("common.enabled"),
                          }),
                        )
                      }
                    >
                      {f.enabled ? t("flags.disable") : t("flags.enable")}
                    </button>
                    <button
                      className="btn"
                      onClick={async () => {
                        const values = await prompt.ask({
                          title: t("flags.rolloutTitle", { key: f.key }),
                          confirmLabel: t("flags.setRollout"),
                          detail: t("flags.rolloutDetail"),
                          fields: [
                            {
                              name: "rollout_percent",
                              label: t("flags.percentLabel"),
                              initial: String(f.rollout_percent),
                              integer: true,
                              min: 0,
                              max: 100,
                              required: true,
                            },
                          ],
                        });
                        if (values) {
                          const percent = Number(values.rollout_percent);
                          void act(
                            `/v1/flags/${encodeURIComponent(f.key)}/rollout`,
                            { rollout_percent: percent },
                            t("flags.rolloutSet", { key: f.key, percent }),
                          );
                        }
                      }}
                    >
                      {t("flags.setRollout")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <ListTotal shown={flags.data?.items.length ?? 0} total={flags.data?.total ?? 0} />
    </div>
  );
}
