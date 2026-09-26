import { useEffect, useRef, useState } from "react";
import { apiGet, getToken, setToken } from "../lib/api";
import { beginOperatorLogin, logoutOperator, oidcConfigured } from "../lib/operatorAuth";
import { useLang } from "../lib/i18n";
import { Dialog } from "./ui";

/**
 * Operator authentication entry. Production uses OIDC; local development may
 * paste a bootstrap token for a disposable test tenant.
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
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Focus the field on open: this dialog can appear over a page full of
  // 401 banners, and the only way forward is typing into it.
  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  if (!open) return null;

  if (import.meta.env.PROD) {
    // This declared `aria-modal="false"` on a dialog that covers the page,
    // which told assistive technology the rest of the console was still live
    // behind it. <Dialog> also traps Tab, closes on Escape, and restores focus
    // to the control that opened it.
    return <Dialog open={open} onClose={onClose} label="企业身份验证" className="prompt">
      <div className="prompt-head"><strong>企业身份验证</strong></div>
      <p className="muted">坐席控制台通过企业单点登录访问。</p>
      {!oidcConfigured() ? <p className="prompt-error" role="alert">尚未配置 OIDC 登录。请联系部署管理员。</p> : null}
      {error ? <p className="prompt-error" role="alert">{error}</p> : null}
      <div className="prompt-actions">
        <button className="btn btn-primary" type="button" disabled={!oidcConfigured() || busy} onClick={() => {
          setBusy(true);
          void beginOperatorLogin().catch((reason: unknown) => {
            setError(reason instanceof Error ? reason.message : String(reason));
            setBusy(false);
          });
        }}>企业单点登录</button>
        {getToken() ? <button className="btn" type="button" disabled={busy} onClick={() => {
          setBusy(true);
          void logoutOperator().catch((reason: unknown) => {
            setError(reason instanceof Error ? reason.message : String(reason));
            setBusy(false);
          });
        }}>退出登录</button> : null}
        <button className="btn btn-ghost" type="button" onClick={onClose}>关闭</button>
      </div>
    </Dialog>;
  }

  function signOut() {
    setToken("");
    // A full reload drops every cached page and every in-flight request bound
    // to the old token, so nothing keeps rendering under an identity that is
    // no longer present.
    window.location.reload();
  }

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
    <Dialog
      open={open}
      // Guarded: a token check is in flight, and closing here would leave the
      // operator unable to tell whether the credential was accepted.
      onClose={() => { if (!busy) onClose(); }}
      label={t("token.title")}
      className="prompt"
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
        {getToken() ? (
          // Without this there was no way to end a session at all: the dialog
          // refuses an empty token, and the token lives in localStorage with no
          // expiry. On a shared workstation the next person inherited it.
          <button className="btn btn-ghost" onClick={signOut} disabled={busy}>
            {t("token.signOut")}
          </button>
        ) : null}
      </div>
    </Dialog>
  );
}
