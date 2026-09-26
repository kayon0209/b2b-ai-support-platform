import { UserManager, WebStorageStateStore } from "oidc-client-ts";

/** Production operator auth: authorization code + PKCE, no pasted bearer token.
 * The OIDC library validates state and provider responses. The user session is
 * tab-scoped; the access token used by API requests lives in this module.
 */
const authority = import.meta.env.VITE_OIDC_ISSUER?.trim() ?? "";
const clientId = import.meta.env.VITE_OIDC_CLIENT_ID?.trim() ?? "";
let manager: UserManager | null = null;
let accessToken = "";

export function oidcConfigured(): boolean {
  return Boolean(authority && clientId);
}

function userManager(): UserManager {
  if (!oidcConfigured()) throw new Error("未配置企业单点登录。请联系系统管理员。");
  manager ??= new UserManager({
    authority,
    client_id: clientId,
    redirect_uri: `${window.location.origin}/auth/callback`,
    post_logout_redirect_uri: `${window.location.origin}/`,
    response_type: "code",
    scope: "openid profile email",
    automaticSilentRenew: false,
    stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
  });
  return manager;
}

export function operatorAccessToken(): string {
  return accessToken;
}

export async function restoreOperatorSession(): Promise<boolean> {
  if (!oidcConfigured()) return false;
  const user = await userManager().getUser();
  if (!user || user.expired) {
    accessToken = "";
    return false;
  }
  accessToken = user.access_token;
  return true;
}

export async function beginOperatorLogin(): Promise<void> {
  const returnTo = `${window.location.pathname}${window.location.search}`;
  await userManager().signinRedirect({
    state: { returnTo: returnTo.startsWith("/admin/") ? returnTo : "/admin/workbench" },
  });
}

export async function finishOperatorLogin(): Promise<string> {
  const user = await userManager().signinRedirectCallback();
  accessToken = user.access_token;
  const state = user.state as { returnTo?: string } | null;
  const candidate = state?.returnTo ?? "";
  return candidate.startsWith("/admin/") && !candidate.startsWith("//")
    ? candidate
    : "/admin/workbench";
}

export async function logoutOperator(): Promise<void> {
  accessToken = "";
  await userManager().signoutRedirect();
}
