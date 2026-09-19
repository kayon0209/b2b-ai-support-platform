import { useEffect, useState } from "react";
import { apiGet, apiPut } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useAction } from "../lib/useAction";
import { useAsync } from "../lib/useAsync";
import type { TenantBranding } from "../lib/types";
import {
  ActionFeedback,
  Badge,
  Card,
  PageHeader,
  Spinner,
} from "../components/ui";
import { LoadError } from "../components/LoadError";
import { useLang } from "../lib/i18n";

const EMPTY = {
  display_name: "",
  logo_url: "",
  primary_color: "#1a2b3c",
  support_email: "",
};

type Form = typeof EMPTY;

/**
 * `<input type="color">` only accepts `#rrggbb`. Anything else it is given —
 * a three-digit shorthand, an `rgb()` string, a stored null — it silently
 * resets to black, so a tenant whose colour was stored in another format
 * appeared to have none and the operator "fixed" it by picking a new one.
 */
function normaliseHex(value: string, fallback: string): string {
  const raw = value.trim();
  if (/^#[0-9a-fA-F]{6}$/.test(raw)) return raw.toLowerCase();
  if (/^#[0-9a-fA-F]{3}$/.test(raw)) {
    const [, r, g, b] = raw;
    return `#${r}${r}${g}${g}${b}${b}`.toLowerCase();
  }
  if (/^[0-9a-fA-F]{6}$/.test(raw)) return `#${raw}`.toLowerCase();
  return fallback;
}

export function Branding() {
  const { t } = useLang();
  const branding = useAsync<{ branding: TenantBranding }>(
    () => apiGet<{ branding: TenantBranding }>("/v1/tenant/branding"),
    [],
  );

  const [form, setForm] = useState<Form>(EMPTY);
  // What the server last gave us. Kept separately so "unsaved" is a fact
  // the page can show instead of something the operator has to remember:
  // the form is silently rewritten on every reload, and an edit lost that
  // way looks exactly like an edit that was never made.
  const [saved, setSaved] = useState<Form>(EMPTY);
  // Write feedback goes through useAction so a failure renders as an error
  // banner, not as muted text styled like the success it sits beside.
  const action = useAction();

  useEffect(() => {
    if (!branding.data) return;
    const b = branding.data.branding;
    const next: Form = {
      display_name: b.display_name ?? "",
      logo_url: b.logo_url ?? "",
      primary_color: normaliseHex(b.primary_color ?? "", EMPTY.primary_color),
      support_email: b.support_email ?? "",
    };
    setSaved(next);
    setForm((current) => (dirty(current, saved) ? current : next));
    // `saved` is read only to decide whether an in-progress edit should win;
    // it is not what should re-trigger this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [branding.data]);

  const set = (key: keyof Form, value: string) =>
    setForm((f) => ({ ...f, [key]: value }));

  const isDirty = dirty(form, saved);

  async function save() {
    const ok = await action.run(
      async () => {
        // Empty strings mean "clear the field": send null, not "".
        await apiPut(
          "/v1/tenant/branding",
          {
            display_name: form.display_name.trim() || null,
            logo_url: form.logo_url.trim() || null,
            primary_color: form.primary_color || null,
            support_email: form.support_email.trim() || null,
          },
          newIdempotencyKey(),
        );
      },
      t("branding.saved"),
    );
    if (ok) branding.reload();
  }

  return (
    <div className="page">
      <PageHeader
        title={t("branding.title")}
        subtitle={t("branding.subtitle")}
      />

      <LoadError error={branding.error} status={branding.errorStatus} onRetry={branding.reload} />
      {branding.loading ? <Spinner label={t("branding.loading")} /> : null}

      <div className="grid-2">
        <Card title={t("branding.settings")}>
          <div className="form-grid">
            <label className="field">
              <span>{t("branding.displayName")}</span>
              <input
                className="text-input"
                value={form.display_name}
                onChange={(e) => set("display_name", e.target.value)}
                placeholder={t("branding.namePlaceholder")}
              />
            </label>

            <label className="field">
              <span>{t("branding.logoUrl")}</span>
              <input
                className="text-input"
                type="url"
                value={form.logo_url}
                onChange={(e) => set("logo_url", e.target.value)}
                placeholder={t("branding.logoPlaceholder")}
              />
            </label>

            <label className="field">
              <span>{t("branding.primaryColour")}</span>
              <input
                type="color"
                value={normaliseHex(form.primary_color, EMPTY.primary_color)}
                onChange={(e) => set("primary_color", e.target.value)}
              />
            </label>

            <label className="field">
              <span>{t("branding.supportEmail")}</span>
              <input
                className="text-input"
                type="email"
                value={form.support_email}
                onChange={(e) => set("support_email", e.target.value)}
                placeholder={t("branding.emailPlaceholder")}
              />
            </label>
          </div>

          <ActionFeedback error={action.error} notice={action.notice} />
          <div className="toolbar">
            <button
              className="btn btn-primary"
              disabled={action.busy || !isDirty}
              onClick={save}
            >
              {action.busy ? t("common.saving") : t("common.save")}
            </button>
            <button
              className="btn"
              disabled={action.busy || !isDirty}
              onClick={() => setForm(saved)}
            >
              {t("common.cancel")}
            </button>
            {isDirty ? <span className="muted">{t("branding.unsaved")}</span> : null}
          </div>
        </Card>

        <Card title={t("branding.preview")}>
          <div className="branding-preview">
            <div
              className="branding-swatch"
              style={{ background: normaliseHex(form.primary_color, EMPTY.primary_color) }}
            />
            {form.logo_url ? (
              <LogoPreview url={form.logo_url} fallback={t("branding.noLogo")} />
            ) : (
              <Badge tone="neutral">{t("branding.noLogo")}</Badge>
            )}
            <div className="branding-name">{form.display_name || t("branding.untitled")}</div>
            <div className="muted">
              {form.support_email || t("branding.noSupport")}
              {branding.data ? ` · ${branding.data.branding.slug}` : ""}
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}

function dirty(form: Form, saved: Form): boolean {
  return (Object.keys(EMPTY) as (keyof Form)[]).some((k) => form[k] !== saved[k]);
}

/**
 * A logo URL is free text from an operator, so it will be wrong sometimes.
 * Left unguarded a bad URL renders the browser's broken-image glyph in the
 * preview with no explanation, and the save still succeeds.
 */
function LogoPreview({ url, fallback }: { url: string; fallback: string }) {
  const { t } = useLang();
  const [failed, setFailed] = useState(false);

  // A new URL deserves a fresh attempt; otherwise one bad paste disables
  // the preview for every address typed afterwards.
  useEffect(() => setFailed(false), [url]);

  if (failed) {
    return (
      <p className="muted">
        {fallback} — {t("branding.logoFailed")}
      </p>
    );
  }
  return (
    <img
      className="branding-logo"
      src={url}
      alt="Tenant logo"
      onError={() => setFailed(true)}
    />
  );
}
