import { ApiError } from "./types";

const BASE = import.meta.env.VITE_API_BASE_URL || "/api";

function authHeader(): Record<string, string> {
  const token =
    import.meta.env.VITE_API_TOKEN || localStorage.getItem("b2b_token") || "";
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
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
  const res = await fetch(`${BASE}${path}`, {
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
  const res = await fetch(`${BASE}${path}`, {
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
  const res = await fetch(`${BASE}${path}`, { method: "DELETE", headers });
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
    throw toApiError(res.status, data);
  }
  return data as T;
}

function toApiError(status: number, data: unknown): ApiError {
  // The control plane returns { error: { code, message, retryable }, trace_id }
  // on failure; cases endpoints additionally wrap success in ok_response.
  const envelope = (data ?? {}) as Record<string, any>;
  const errorBlock = envelope.error ?? {};
  return new ApiError(
    status,
    typeof errorBlock.code === "string" ? errorBlock.code : "HTTP_ERROR",
    typeof errorBlock.message === "string" ? errorBlock.message : `HTTP ${status}`,
    Boolean(errorBlock.retryable),
  );
}

export function isUnauthorized(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401;
}
