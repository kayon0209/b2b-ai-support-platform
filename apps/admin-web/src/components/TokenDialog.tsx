import { useEffect, useRef, useState } from "react";
import { apiGet, getToken, setToken } from "../lib/api";
import { useLang } from "../lib/i18n";

/**
 * Bearer-token entry for the control plane.
 *
 * Every API call carries a bearer token the operator was issued (an OIDC
 * access token in production). This dialog is the one place a token is
 * entered or replaced, and it validates before keeping it: a token saved
 * blind would surface as 401s on every page with no way to tell a typo from
 * an expired credential.
 */
export function TokenDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useLang();
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Focus the field on open: this dialog can appear over a page full of
  // 401 banners, and the only way forward is typing into it.
  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  if (!open) return null;

  async function save() {
    const token = draft.trim();
    if (!token) {
      setError(t("token.errorEmpty"));
      return;
    }
    const previous = getToken();
    setBusy(true);
    setError(null);
    setToken(token);
    try {
      // A cheap authenticated read: 200 means the token resolves to a
      // membership, 401 means it does not. A reload then brings every page
      // back into a clean loading state under the new token.
      await apiGet("/v1/tenant/usage");
      window.location.reload();
    } catch (err) {
      setToken(previous);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="prompt"
      role="dialog"
      aria-modal="false"
      aria-label={t("token.title")}
      ref={dialogRef}
      onKeyDown={(e) => {
        if (e.key === "Escape" && !busy) onClose();
      }}
    >
      <div className="prompt-head">
        <strong>{t("token.title")}</strong>
      </div>
      <p className="muted">{t("token.body")}</p>
      <div className="prompt-fields">
        <label className="prompt-field">
          <span>{t("token.label")}</span>
          <input
            className="text-input"
            type="password"
            autoComplete="off"
            value={draft}
            ref={inputRef}
            onChange={(e) => setDraft(e.target.value)}
          />
        </label>
      </div>
      {error ? (
        <p className="prompt-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="prompt-actions">
        <button className="btn btn-primary" onClick={save} disabled={busy}>
          {busy ? t("token.checking") : t("token.connect")}
        </button>
        <button className="btn" onClick={onClose} disabled={busy}>
          {t("common.cancel")}
        </button>
      </div>
    </div>
  );
}
