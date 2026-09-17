import { useEffect, useState } from "react";
import { apiGet, apiPut } from "../lib/api";
import { useAsync } from "../lib/useAsync";
import type { TenantBranding } from "../lib/types";
import { Badge, Card, ErrorBanner, PageHeader, Spinner } from "../components/ui";

const EMPTY = {
  display_name: "",
  logo_url: "",
  primary_color: "#1a2b3c",
  support_email: "",
};

export function Branding() {
  const branding = useAsync<{ branding: TenantBranding }>(
    () => apiGet<{ branding: TenantBranding }>("/v1/tenant/branding"),
    [],
  );

  const [form, setForm] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  // Load the fetched values into the form once (and after each reload).
  useEffect(() => {
    if (!branding.data) return;
    const b = branding.data.branding;
    setForm({
      display_name: b.display_name ?? "",
      logo_url: b.logo_url ?? "",
      primary_color: b.primary_color ?? EMPTY.primary_color,
      support_email: b.support_email ?? "",
    });
  }, [branding.data]);

  const set = (key: keyof typeof EMPTY, value: string) =>
    setForm((f) => ({ ...f, [key]: value }));

  async function save() {
    setBusy(true);
    setNotice(null);
    try {
      // Empty strings mean "clear the field": send null, not "".
      await apiPut(
        "/v1/tenant/branding",
        {
          display_name: form.display_name.trim() || null,
          logo_url: form.logo_url.trim() || null,
          primary_color: form.primary_color || null,
          support_email: form.support_email.trim() || null,
        },
        crypto.randomUUID?.() ?? `idem-${Date.now()}`,
      );
      setNotice("Branding saved.");
      branding.reload();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <PageHeader
        title="Branding"
        subtitle="How this tenant appears in the product and to customers."
      />

      {branding.error ? <ErrorBanner message={branding.error} onRetry={branding.reload} /> : null}
      {branding.loading ? <Spinner label="Loading branding…" /> : null}

      <div className="grid-2">
        <Card title="Settings">
          <div className="form-grid">
            <label className="field">
              <span>Display name</span>
              <input
                className="text-input"
                value={form.display_name}
                onChange={(e) => set("display_name", e.target.value)}
                placeholder="Acme Support"
              />
            </label>

            <label className="field">
              <span>Logo URL</span>
              <input
                className="text-input"
                value={form.logo_url}
                onChange={(e) => set("logo_url", e.target.value)}
                placeholder="https://cdn.example.com/logo.png"
              />
            </label>

            <label className="field">
              <span>Primary colour</span>
              <input
                type="color"
                value={form.primary_color || EMPTY.primary_color}
                onChange={(e) => set("primary_color", e.target.value)}
              />
            </label>

            <label className="field">
              <span>Support email</span>
              <input
                className="text-input"
                value={form.support_email}
                onChange={(e) => set("support_email", e.target.value)}
                placeholder="help@acme.example"
              />
            </label>
          </div>

          <div className="toolbar">
            <button className="btn btn-primary" disabled={busy} onClick={save}>
              Save branding
            </button>
            {notice ? <span className="muted">{notice}</span> : null}
          </div>
        </Card>

        <Card title="Preview">
          <div className="branding-preview">
            <div
              className="branding-swatch"
              style={{ background: form.primary_color || EMPTY.primary_color }}
            />
            {form.logo_url ? (
              <img className="branding-logo" src={form.logo_url} alt="Tenant logo" />
            ) : (
              <Badge tone="neutral">no logo</Badge>
            )}
            <div className="branding-name">{form.display_name || "Untitled tenant"}</div>
            <div className="muted">
              {form.support_email || "no support address"}
              {branding.data ? ` · ${branding.data.branding.slug}` : ""}
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}
