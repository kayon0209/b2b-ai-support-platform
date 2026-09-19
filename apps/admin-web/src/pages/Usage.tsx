import { useState } from "react";
import { apiGet, apiPost, apiPut } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useAction } from "../lib/useAction";
import { useAsync } from "../lib/useAsync";
import {
  type BillingAdjustmentResult,
  type BillingRollup,
  type UsageSnapshot,
} from "../lib/types";
import {
  ActionFeedback,
  Badge,
  Card,
  ErrorBanner,
  PageHeader,
  Spinner,
  Stat,
} from "../components/ui";
import { LoadError } from "../components/LoadError";
import { useLang } from "../lib/i18n";
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
  const { t } = useLang();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const action = useAction();

  // Correction form. The ledger is append-only, so this is the only way to fix
  // a billing error without a database console.
  const [correcting, setCorrecting] = useState(false);
  const [correction, setCorrection] = useState({
    run_id: "",
    prompt_tokens_delta: "",
    completion_tokens_delta: "",
    reason: "",
  });

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
  // viewer may not read commercial data. Adjusting is stricter still - only
  // the tenant owner holds BILLING_ADJUST.
  const billingForbidden = billing.errorStatus === 403;

  async function submitCorrection() {
    if (!correction.run_id.trim()) {
      action.fail(t("usage.errRunId"));
      return;
    }
    if (!correction.reason.trim()) {
      // The server accepts an empty reason, but a financial correction with
      // no stated cause is un-auditable, so the form insists on one.
      action.fail(t("usage.errReason"));
      return;
    }
    const promptDelta = Number(correction.prompt_tokens_delta || "0");
    const completionDelta = Number(correction.completion_tokens_delta || "0");
    if (!Number.isInteger(promptDelta) || !Number.isInteger(completionDelta)) {
      action.fail(t("usage.errWhole"));
      return;
    }
    if (promptDelta === 0 && completionDelta === 0) {
      action.fail(t("usage.errChange"));
      return;
    }
    const ok = await action.run(
      async () => {
        const result = await apiPost<BillingAdjustmentResult>(
          "/v1/tenant/billing/adjustments",
          {
            run_id: correction.run_id.trim(),
            prompt_tokens_delta: promptDelta,
            completion_tokens_delta: completionDelta,
            reason: correction.reason.trim(),
          },
          newIdempotencyKey(),
        );
        if (result.duplicate) {
          // A duplicate means the key was reused, so nothing changed. Say so
          // rather than reporting a success the ledger did not record.
          throw new Error(t("usage.duplicate"));
        }
      },
      t("usage.correctionRecorded"),
    );
    if (ok) {
      setCorrecting(false);
      setCorrection({ run_id: "", prompt_tokens_delta: "", completion_tokens_delta: "", reason: "" });
      billing.reload();
    }
  }

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
        setSaveError(t("usage.quotaError"));
        return;
      }
      await apiPut<{ usage: UsageSnapshot }>(
        "/v1/tenant/quota",
        { monthly_run_quota: value },
        newIdempotencyKey(),
      );
      setEditing(false);
      setNotice(
        value === null
          ? t("usage.quotaCleared")
          : t("usage.quotaSet", { value: value.toLocaleString() }),
      );
      usage.reload();
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : t("usage.quotaError"));
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
        title={t("usage.title")}
        subtitle={t("usage.subtitle")}
        actions={
          editing ? (
            <div className="row">
              <input
                className="text-input"
                inputMode="numeric"
                placeholder={t("usage.quotaPlaceholder")}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                aria-label={t("usage.quotaLabel")}
              />
              <button className="btn" onClick={save} disabled={saving}>
                {saving ? t("common.saving") : t("common.save")}
              </button>
              <button className="btn" onClick={() => setEditing(false)} disabled={saving}>
                {t("common.cancel")}
              </button>
            </div>
          ) : (
            <button className="btn" onClick={startEdit} disabled={!snapshot}>
              {t("usage.changeQuota")}
            </button>
          )
        }
      />

      <LoadError error={usage.error} status={usage.errorStatus} onRetry={usage.reload} />
      {saveError ? <ErrorBanner message={saveError} onRetry={save} /> : null}
      <ActionFeedback error={action.error} notice={action.notice} />
      {/* Same live-region contract as ActionFeedback: a quiet "saved"
          confirmation is invisible to a screen reader otherwise. */}
      {notice ? (
        <div className="banner banner-ok" role="status">
          {notice}
        </div>
      ) : null}

      {usage.loading && !snapshot ? <Spinner label={t("usage.loadUsage")} /> : null}

      {snapshot ? (
        <>
          <div className="stat-grid">
            <Card>
              <Stat
                label={t("usage.runsUsed")}
                value={int(snapshot.runs_used)}
                tone={snapshot.over_quota ? "bad" : "good"}
              />
            </Card>
            <Card>
              <Stat
                label={t("usage.quota")}
                value={snapshot.quota === null ? t("usage.unlimited") : int(snapshot.quota)}
              />
            </Card>
            <Card>
              <Stat
                label={t("usage.remaining")}
                value={snapshot.remaining === null ? "—" : int(snapshot.remaining)}
                tone={snapshot.over_quota ? "bad" : "good"}
              />
            </Card>
            <Card>
              <Stat label={t("usage.promptTokens")} value={int(snapshot.prompt_tokens)} />
            </Card>
            <Card>
              <Stat label={t("usage.completionTokens")} value={int(snapshot.completion_tokens)} />
            </Card>
            <Card>
              <Stat
                label={t("usage.consumed")}
                value={usedShare === null ? "—" : pct(usedShare)}
                tone={snapshot.over_quota ? "bad" : "good"}
              />
            </Card>
          </div>

          <Card title={t("usage.periodTitle")}>
            <ul className="kv">
              <li>
                <span>{t("usage.window")}</span>
                <span>{period}</span>
              </li>
              <li>
                <span>{t("common.status")}</span>
                <span>
                  {snapshot.over_quota ? (
                    <Badge tone="bad">{t("usage.overQuota")}</Badge>
                  ) : (
                    <Badge tone="good">{t("usage.acceptingRuns")}</Badge>
                  )}
                </span>
              </li>
            </ul>
            <p className="muted">{t("usage.overQuotaNote")}</p>
          </Card>
        </>
      ) : null}

      <Card title={t("usage.ledgerTitle")}>
        {billingForbidden ? (
          <p className="muted">{t("usage.noPermission")}</p>
        ) : billing.error ? (
          <ErrorBanner message={billing.error} onRetry={billing.reload} />
        ) : billing.loading && !ledger ? (
          <Spinner label={t("usage.loadLedger")} />
        ) : ledger ? (
          <>
            <ul className="kv">
              <li>
                <span>{t("usage.ledgerEntries")}</span>
                <span>{int(ledger.entries)}</span>
              </li>
              <li>
                <span>{t("usage.usageEntries")}</span>
                <span>{int(ledger.usage_entries)}</span>
              </li>
              <li>
                <span>{t("usage.adjustments")}</span>
                <span>{int(ledger.adjustment_entries)}</span>
              </li>
              <li>
                <span>{t("usage.promptTokens")}</span>
                <span>{int(ledger.prompt_tokens)}</span>
              </li>
              <li>
                <span>{t("usage.completionTokens")}</span>
                <span>{int(ledger.completion_tokens)}</span>
              </li>
              <li>
                <span>{t("usage.totalTokens")}</span>
                <span>{int(ledger.total_tokens)}</span>
              </li>
            </ul>
            <p className="muted">{t("usage.ledgerNote")}</p>

            {correcting ? (
              <div className="toolbar">
                <input
                  className="text-input"
                  value={correction.run_id}
                  onChange={(e) => setCorrection({ ...correction, run_id: e.target.value })}
                  placeholder={t("usage.runId")}
                  aria-label={t("usage.runId")}
                />
                <input
                  className="text-input"
                  inputMode="numeric"
                  value={correction.prompt_tokens_delta}
                  onChange={(e) =>
                    setCorrection({ ...correction, prompt_tokens_delta: e.target.value })
                  }
                  placeholder={t("usage.promptDelta")}
                  aria-label={t("usage.promptDelta")}
                />
                <input
                  className="text-input"
                  inputMode="numeric"
                  value={correction.completion_tokens_delta}
                  onChange={(e) =>
                    setCorrection({ ...correction, completion_tokens_delta: e.target.value })
                  }
                  placeholder={t("usage.completionDelta")}
                  aria-label={t("usage.completionDelta")}
                />
                <input
                  className="text-input"
                  value={correction.reason}
                  onChange={(e) => setCorrection({ ...correction, reason: e.target.value })}
                  placeholder={t("usage.reason")}
                  aria-label={t("usage.reason")}
                />
                <button className="btn btn-primary" onClick={submitCorrection} disabled={action.busy}>
                  {action.busy ? t("usage.recording") : t("usage.record")}
                </button>
                <button
                  className="btn"
                  onClick={() => setCorrecting(false)}
                  disabled={action.busy}
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button className="btn" onClick={() => setCorrecting(true)} disabled={billingForbidden}>
                {t("usage.recordCorrection")}
              </button>
            )}
          </>
        ) : null}
      </Card>
    </div>
  );
}
