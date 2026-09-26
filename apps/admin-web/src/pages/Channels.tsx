import { apiGet, apiPost } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useAction } from "../lib/useAction";
import { useAsync } from "../lib/useAsync";
import type { Connector } from "../lib/types";
import {
  ActionFeedback,
  Badge,
  Card,
  EmptyState,
  ListTotal,
  PageHeader,
  Spinner,
} from "../components/ui";
import { LoadError } from "../components/LoadError";
import { useLang } from "../lib/i18n";
import { dateFromEpochSeconds } from "../lib/format";

/**
 * Channel and connector health.
 *
 * The backend for this existed and had no screen: `/v1/connectors` lists them,
 * `/{id}/health-check` probes one, `/{id}/reactivate` clears a re-auth hold, and
 * `integrations/health.py` holds the state machine. So "渠道健康与配置台" was a
 * frontend gap, not a missing capability - and the consequence of it being
 * missing was that an operator had no way to answer "is the email channel
 * actually working" without curl.
 *
 * Two things the page is careful about:
 *
 * - **`executable` is shown separately from `status`.** A connector can be
 *   `active` and still not usable (a failed probe holds it out of the write
 *   path). Showing only the raw status would let an operator conclude a channel
 *   was fine while it was quietly excluded from every send.
 * - **`last_health_at` is rendered as "never" when absent, not as an epoch.**
 *   A connector nobody has probed is a real and different state, and it is the
 *   one most likely to be the reason a channel is not working.
 */
export function Channels() {
  const { t } = useLang();
  const action = useAction();

  const connectors = useAsync<{ connectors: Connector[] }>(() =>
    apiGet<{ connectors: Connector[] }>("/v1/connectors"),
  );

  async function act(path: string, okMsg: string): Promise<void> {
    const ok = await action.run(
      () => apiPost(path, {}, newIdempotencyKey()).then(() => undefined),
      okMsg,
    );
    if (ok) connectors.reload();
  }

  const items = connectors.data?.connectors ?? [];

  return (
    <div className="page">
      <PageHeader title={t("channels.title")} subtitle={t("channels.subtitle")} />

      <ActionFeedback error={action.error} notice={action.notice} />
      <LoadError error={connectors.error} status={connectors.errorStatus} onRetry={connectors.reload} />
      {connectors.loading ? <Spinner label={t("channels.loading")} /> : null}
      {connectors.data && items.length === 0 ? (
        <EmptyState message={t("channels.empty")} />
      ) : null}

      {items.length > 0 ? (
        <Card title={t("channels.tableTitle")}>
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">{t("channels.headerName")}</th>
                  <th scope="col">{t("channels.headerProvider")}</th>
                  <th scope="col">{t("channels.headerStatus")}</th>
                  <th scope="col">{t("channels.headerUsable")}</th>
                  <th scope="col">{t("channels.headerCredential")}</th>
                  <th scope="col">{t("channels.headerLastHealth")}</th>
                  <th scope="col">{t("channels.headerCapabilities")}</th>
                  <th scope="col" />
                </tr>
              </thead>
              <tbody>
                {items.map((c) => (
                  <tr key={c.connector_id}>
                    <td className="cell-strong">{c.name}</td>
                    <td className="muted">{c.provider}</td>
                    <td>
                      <Badge tone={c.executable ? "good" : "warn"}>{c.status}</Badge>
                    </td>
                    <td>
                      {/* Separate from `status`: a connector can be active and
                          still held out of the write path by a failed probe. */}
                      <Badge tone={c.executable ? "good" : "warn"}>
                        {c.executable ? t("channels.usable") : t("channels.withheld")}
                      </Badge>
                    </td>
                    <td>
                      <Badge tone={c.credential_configured ? "good" : "warn"}>
                        {c.credential_configured
                          ? t("channels.credentialSet")
                          : t("channels.credentialMissing")}
                      </Badge>
                    </td>
                    <td className="muted">
                      {c.last_health_at
                        ? dateFromEpochSeconds(c.last_health_at)
                        : t("channels.neverProbed")}
                    </td>
                    <td className="muted">
                      {c.capabilities.length > 0 ? c.capabilities.join(", ") : "—"}
                    </td>
                    <td className="row-actions">
                      <button
                        className="btn"
                        disabled={action.busy}
                        onClick={() =>
                          void act(
                            `/v1/connectors/${encodeURIComponent(c.connector_id)}/health-check`,
                            t("channels.probeDone", { name: c.name }),
                          )
                        }
                      >
                        {t("channels.probe")}
                      </button>
                      {!c.executable ? (
                        <button
                          className="btn"
                          disabled={action.busy}
                          onClick={() =>
                            void act(
                              `/v1/connectors/${encodeURIComponent(c.connector_id)}/reactivate`,
                              t("channels.reactivated", { name: c.name }),
                            )
                          }
                        >
                          {t("channels.reactivate")}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}
      <ListTotal shown={items.length} total={items.length} />
    </div>
  );
}
