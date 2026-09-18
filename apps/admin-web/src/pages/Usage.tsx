import { useState } from "react";
import { apiGet, apiPut } from "../lib/api";
import { useAsync } from "../lib/useAsync";
import { type BillingRollup, type UsageSnapshot } from "../lib/types";
import { Badge, Card, ErrorBanner, PageHeader, Spinner, Stat } from "../components/ui";
import { dateFromEpochSeconds, int, pct } from "../lib/format";

/**
 * Usage and quota (Phase 5). Reads GET /v1/tenant/usage and writes
 * PUT /v1/tenant/quota.
 *
 * The write is a commercial change, so the control plane requires
 * TENANT_ADMIN plus an Idempotency-Key. This page generates a fresh key per
 * submit attempt rather than per render, so a double-click sends one change
 * and a retry after a network error still sends one change.
 *
 * The billing ledger is a separate, more restricted read (AUDIT_READ): it is
 * the record an invoice is computed from, so a support agent who may see the
 * live run count may not see it. A refusal is therefore shown as a note, not
 * as an error banner - the page is working, the viewer just lacks the
 * permission, and a red "failed to load" would send them to support.
 */
export function Usage() {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const usage = useAsync<{ usage: UsageSnapshot }>(
    () => apiGet<{ usage: UsageSnapshot }>("/v1/tenant/usage"),
    [],
  );
  const billing = useAsync<{ billing: BillingRollup }>(
    () => apiGet<{ billing: BillingRollup }>("/v1/tenant/billing"),
    [],
  );

  const snapshot = usage.data?.usage;
  const ledger = billing.data?.billing;
  // 403 is a permission boundary, not a failure: the page is fine, this
  // viewer may not read commercial data.
  const billingForbidden = billing.errorStatus === 403;

  function startEdit() {
    setDraft(snapshot?.quota === null || snapshot?.quota === undefined ? "" : String(snapshot.quota));
    setEditing(true);
    setSaveError(null);
    setNotice(null);
  }

  async function save() {
    setSaving(true);
    setSaveError(null);
    setNotice(null);
    try {
      // Empty input is an explicit "no limit"; anything else must be a
      // non-negative integer, matching QuotaIn on the control plane.
      const trimmed = draft.trim();
      const value = trimmed === "" ? null : Number(trimmed);
      if (value !== null && (!Number.isInteger(value) || value < 0)) {
        setSaveError("Quota must be a whole number of runs, or empty for unlimited.");
        return;
      }
      await apiPut<{ usage: UsageSnapshot }>(
        "/v1/tenant/quota",
        { monthly_run_quota: value },
        crypto.randomUUID(),
      );
      setEditing(false);
      setNotice(
        value === null
          ? "Quota cleared — this tenant is now unlimited."
          : `Quota set to ${value.toLocaleString()} runs per calendar month.`,
      );
      usage.reload();
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Could not update the quota.");
    } finally {
      setSaving(false);
    }
  }

  const usedShare =
    snapshot && snapshot.quota ? Math.min(1, snapshot.runs_used / snapshot.quota) : null;
  const period = snapshot
    ? `${dateFromEpochSeconds(snapshot.period_start)} → ${dateFromEpochSeconds(snapshot.period_end)}`
    : "—";

  return (
    <div className="page">
      <PageHeader
        title="Usage & Quota"
        subtitle="Agent runs consumed this calendar month (UTC), and the ceiling on them."
        actions={
          editing ? (
            <div className="row">
              <input
                className="text-input"
                inputMode="numeric"
                placeholder="unlimited"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                aria-label="Monthly run quota"
              />
              <button className="btn" onClick={save} disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </button>
              <button className="btn" onClick={() => setEditing(false)} disabled={saving}>
                Cancel
              </button>
            </div>
          ) : (
            <button className="btn" onClick={startEdit} disabled={!snapshot}>
              Change quota
            </button>
          )
        }
      />

      {usage.error ? <ErrorBanner message={usage.error} onRetry={usage.reload} /> : null}
      {saveError ? <ErrorBanner message={saveError} onRetry={save} /> : null}
      {notice ? <div className="banner banner-ok">{notice}</div> : null}

      {usage.loading && !snapshot ? <Spinner label="Loading usage…" /> : null}

      {snapshot ? (
        <>
          <div className="stat-grid">
            <Card>
              <Stat
                label="Runs used"
                value={int(snapshot.runs_used)}
                tone={snapshot.over_quota ? "bad" : "good"}
              />
            </Card>
            <Card>
              <Stat
                label="Quota"
                value={snapshot.quota === null ? "Unlimited" : int(snapshot.quota)}
              />
            </Card>
            <Card>
              <Stat
                label="Remaining"
                value={snapshot.remaining === null ? "—" : int(snapshot.remaining)}
                tone={snapshot.over_quota ? "bad" : "good"}
              />
            </Card>
            <Card>
              <Stat label="Prompt tokens" value={int(snapshot.prompt_tokens)} />
            </Card>
            <Card>
              <Stat label="Completion tokens" value={int(snapshot.completion_tokens)} />
            </Card>
            <Card>
              <Stat
                label="Consumed"
                value={usedShare === null ? "—" : pct(usedShare)}
                tone={snapshot.over_quota ? "bad" : "good"}
              />
            </Card>
          </div>

          <Card title="This period">
            <ul className="kv">
              <li>
                <span>Window</span>
                <span>{period}</span>
              </li>
              <li>
                <span>Status</span>
                <span>
                  {snapshot.over_quota ? (
                    <Badge tone="bad">Over quota — new runs are refused with 429</Badge>
                  ) : (
                    <Badge tone="good">Accepting runs</Badge>
                  )}
                </span>
              </li>
            </ul>
            <p className="muted">
              Usage counts agent runs started in the period. A run enqueued while over quota is
              refused with a 429 rather than dropped silently, so a caller can tell "declined for
              capacity" from "no evidence found".
            </p>
          </Card>
        </>
      ) : null}

      <Card title="Billing ledger">
        {billingForbidden ? (
          <p className="muted">
            You do not have permission to read billing data. It requires the audit-read role,
            the same one that grants access to the audit trail — commercial totals are not part
            of the support role.
          </p>
        ) : billing.error ? (
          <ErrorBanner message={billing.error} onRetry={billing.reload} />
        ) : billing.loading && !ledger ? (
          <Spinner label="Loading ledger…" />
        ) : ledger ? (
          <>
            <ul className="kv">
              <li>
                <span>Ledger entries</span>
                <span>{int(ledger.entries)}</span>
              </li>
              <li>
                <span>Usage entries</span>
                <span>{int(ledger.usage_entries)}</span>
              </li>
              <li>
                <span>Adjustments</span>
                <span>{int(ledger.adjustment_entries)}</span>
              </li>
              <li>
                <span>Prompt tokens</span>
                <span>{int(ledger.prompt_tokens)}</span>
              </li>
              <li>
                <span>Completion tokens</span>
                <span>{int(ledger.completion_tokens)}</span>
              </li>
              <li>
                <span>Total tokens</span>
                <span>{int(ledger.total_tokens)}</span>
              </li>
            </ul>
            <p className="muted">
              The append-only record an invoice is computed from, keyed by the usage event so an
              outbox redelivery collapses instead of double-billing. A correction is a new
              adjustment entry, never an edit — which is why adjustments can differ from the live
              run count above.
            </p>
          </>
        ) : null}
      </Card>
    </div>
  );
}
