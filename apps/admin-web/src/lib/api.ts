import { ApiError } from "./types";
import { operatorAccessToken } from "./operatorAuth";

const BASE = import.meta.env.VITE_API_BASE_URL || "/api";
const TOKEN_KEY = "b2b_token";

const ZH_ERRORS: Record<string, string> = {
  AUTH_UNRESOLVED: "登录状态已失效，请重新登录。",
  LEASE_CONFLICT: "会话状态已变化，请刷新后重试。",
  AGENT_UNAVAILABLE: "坐席未在当前企业启用。",
  AGENT_AT_CAPACITY: "目标坐席已达到接待上限。",
  CASE_CLAIM_ACTOR_MISMATCH: "只能认领给当前登录的坐席。",
  CONVERSATION_CLOSED: "会话已结束，请开始新会话。",
  IDEMPOTENCY_CONFLICT: "这次提交的标识已用于其他内容，请重新操作。",
};

function isChinese(): boolean {
  return document.documentElement.lang.startsWith("zh");
}

let unauthorizedHandler: (() => void) | null = null;

/**
 * Registered once by the shell: every 401 opens the token dialog instead of
 * leaving each page to render a bare "AUTH_UNRESOLVED" banner the operator
 * cannot act on.
 */
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  unauthorizedHandler = fn;
}

export function getToken(): string {
  if (import.meta.env.PROD) return operatorAccessToken();
  const stored = localStorage.getItem(TOKEN_KEY);
  if (stored) return stored;
  // A build-time token would be sent to every visitor, including customers.
  // Only a local dev build may use one; production reads the OIDC session.
  return import.meta.env.DEV ? import.meta.env.VITE_API_TOKEN || "" : "";
}

/** Whether a build-time token is being used (dev only) rather than a stored one. */
export function usingBuildToken(): boolean {
  return !localStorage.getItem(TOKEN_KEY) && Boolean(import.meta.env.DEV && import.meta.env.VITE_API_TOKEN);
}

export function setToken(token: string): void {
  if (import.meta.env.PROD) throw new Error("生产环境只能使用企业单点登录");
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function authHeader(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Nothing in the console ever hung up on the server before: `fetch` has no
 * default timeout, so a request that never answered left the page spinning
 * indefinitely with no error and no way to cancel. Every call now aborts
 * after `REQUEST_TIMEOUT_MS` and reports something actionable.
 */
const REQUEST_TIMEOUT_MS = 20_000;

async function send(path: string, init: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(`${BASE}${path}`, { ...init, signal: controller.signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new ApiError(
        0,
        "TIMEOUT",
        isChinese()
          ? "服务响应超时，请稍后重试。"
          : `The control plane did not respond within ${REQUEST_TIMEOUT_MS / 1000}s. Check that the API is running, then retry.`,
        true,
      );
    }
    if (err instanceof TypeError) {
      // fetch rejects with a bare TypeError for DNS failures, refused
      // connections and CORS. "Failed to fetch" tells an operator nothing.
      throw new ApiError(
        0,
        "NETWORK",
        isChinese()
          ? "无法连接客服服务，请检查网络后重试。"
          : "Could not reach the control plane. Check the network or the API host, then retry.",
        true,
      );
    }
    throw err;
  } finally {
    window.clearTimeout(timer);
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await send(`${path}`, {
    method: "GET",
    headers: { Accept: "application/json", ...authHeader() },
  });
  return unwrap<T>(res);
}

export async function apiPost<T>(
  path: string,
  body: unknown,
  idempotencyKey?: string,
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
    ...authHeader(),
  };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const res = await send(`${path}`, {
    method: "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return unwrap<T>(res);
}

export async function apiPut<T>(
  path: string,
  body: unknown,
  idempotencyKey?: string,
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
    ...authHeader(),
  };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const res = await send(`${path}`, {
    method: "PUT",
    headers,
    body: JSON.stringify(body),
  });
  return unwrap<T>(res);
}

export async function apiPatch<T>(
  path: string,
  body: unknown,
  idempotencyKey?: string,
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
    ...authHeader(),
  };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const res = await send(`${path}`, { method: "PATCH", headers, body: JSON.stringify(body) });
  return unwrap<T>(res);
}

/**
 * POST a multipart body, for endpoints that take a file.
 *
 * Separate from `apiPost` rather than a flag on it: `apiPost` sets
 * `Content-Type: application/json` explicitly, and a multipart request must
 * *not* set one - the browser generates it from the FormData so it carries the
 * boundary string. Sending the JSON header with a FormData body makes the
 * server parse nothing and reject the upload as malformed, which is a
 * confusing failure for a mistake that looks like a detail.
 */
export async function apiUpload<T>(path: string, form: FormData, idempotencyKey?: string): Promise<T> {
  const res = await send(`${path}`, {
    method: "POST",
    // No Content-Type - the browser sets it, boundary included.
    headers: {
      Accept: "application/json",
      ...authHeader(),
      ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
    },
    body: form,
  });
  return unwrap<T>(res);
}

export async function apiDelete<T>(path: string, idempotencyKey?: string): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...authHeader(),
  };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const res = await send(`${path}`, { method: "DELETE", headers });
  return unwrap<T>(res);
}

async function unwrap<T>(res: Response): Promise<T> {
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text) as unknown;
    } catch {
      // A proxy or an ingress can answer with HTML (a 502 page, an auth
      // redirect). Letting JSON.parse throw would surface as a syntax error
      // and hide the status, which is the actionable part.
      //
      // A bare status is still not enough, though. The helpful "could not
      // reach the control plane" branch in `send` only fires when the browser
      // itself cannot connect, which is not the deployed shape: with a proxy
      // or an ingress in front, an outage arrives as an HTTP response. Vite's
      // dev proxy answers 500 when its target is down, so an operator with the
      // API stopped was shown "HTTP 500" - which reads like a bug in the
      // server, not like the server being absent.
      const down = res.status >= 500;
      throw new ApiError(
        res.status,
        "NON_JSON_RESPONSE",
        down
          ? isChinese()
            ? `服务暂时不可用（HTTP ${res.status}），请稍后重试。`
            : `The control plane answered HTTP ${res.status} without a JSON body, so it is most likely down or restarting. Retry in a moment.`
          : `HTTP ${res.status}`,
        down,
      );
    }
  }
  if (!res.ok) {
    const err = toApiError(res.status, data);
    if (res.status === 401) unauthorizedHandler?.();
    throw err;
  }
  return data as T;
}

function toApiError(status: number, data: unknown): ApiError {
  // The control plane returns { error: { code, message, retryable }, trace_id }
  // on failure; cases endpoints additionally wrap success in ok_response.
  const envelope = (data ?? {}) as Record<string, any>;
  const errorBlock = envelope.error ?? {};
  // Two spellings exist on the wire: the shared `error_response` envelope
  // uses `message`, while some domain errors historically used `reason`.
  // Reading only one of them rendered the other as a bare "HTTP <status>",
  // which is the least useful thing a banner can say.
  const stated =
    typeof errorBlock.message === "string"
      ? errorBlock.message
      : typeof errorBlock.reason === "string"
        ? errorBlock.reason
        : "";
  // A 5xx with nothing to say about itself is the shape of a server that is
  // not there. This is the common case, not the exotic one: Vite's dev proxy
  // answers a dead target with `500`, `text/plain`, and an *empty* body, so
  // neither the JSON branch nor the JSON-parse failure branch above ever runs -
  // the operator was simply shown "HTTP 500", which reads like a bug in the
  // control plane rather than like its absence.
  const message =
    (isChinese() ? ZH_ERRORS[errorBlock.code] : "") ||
    (isChinese() && status >= 500 ? `服务暂时不可用（HTTP ${status}），请稍后重试。` : stated) ||
    (status >= 500
      ? `The control plane answered HTTP ${status} with no explanation, so it is most likely down or restarting. Retry in a moment.`
      : `HTTP ${status}`);
  // The trace id is the one thing support needs from a failing request;
  // appending it means every banner shows it without each page caring.
  const traceId = typeof envelope.trace_id === "string" ? envelope.trace_id : "";
  return new ApiError(
    status,
    typeof errorBlock.code === "string" ? errorBlock.code : "HTTP_ERROR",
    traceId ? `${message} (trace ${traceId})` : message,
    Boolean(errorBlock.retryable),
  );
}

export function isUnauthorized(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401;
}
