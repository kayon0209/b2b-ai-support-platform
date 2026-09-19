import { ApiError } from "./types";

const BASE = import.meta.env.VITE_API_BASE_URL || "/api";
const TOKEN_KEY = "b2b_token";

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
  return localStorage.getItem(TOKEN_KEY) || import.meta.env.VITE_API_TOKEN || "";
}

export function setToken(token: string): void {
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
        `The control plane did not respond within ${
          REQUEST_TIMEOUT_MS / 1000
        }s. Check that the API is running, then retry.`,
        true,
      );
    }
    if (err instanceof TypeError) {
      // fetch rejects with a bare TypeError for DNS failures, refused
      // connections and CORS. "Failed to fetch" tells an operator nothing.
      throw new ApiError(
        0,
        "NETWORK",
        "Could not reach the control plane. Check the network or the API host, then retry.",
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
      throw new ApiError(res.status, "NON_JSON_RESPONSE", `HTTP ${res.status}`, res.status >= 500);
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
  const message = stated || `HTTP ${status}`;
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
