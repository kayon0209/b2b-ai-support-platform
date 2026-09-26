/**
 * The one way this app calls the network.
 *
 * `fetch` has no default timeout. A request that never answers leaves the UI
 * waiting forever: no error, no spinner that ends, nothing to cancel. The
 * operator console fixed that inside its own client (`lib/api.ts`), and the
 * customer chat surface kept calling `fetch` directly - so a stalled backend
 * pinned the send button at "发送中…" with no way out, on the one screen a
 * customer cannot navigate away from.
 *
 * Two things are guaranteed here, and both are needed:
 *
 *  - a deadline, so an unanswered request becomes an error the caller can
 *    show and the customer can retry;
 *  - an external `signal`, so a caller that is being torn down (a component
 *    unmounting, a poll superseded, the customer switching conversation) can
 *    cancel in-flight work instead of letting it resolve into state that no
 *    longer has a component to receive it.
 *
 * Cancellation and timeout both surface as `AbortError`, so callers that only
 * care about "this did not complete" can catch one error; `timedOut` tells
 * them which it was.
 */

export const REQUEST_TIMEOUT_MS = 20_000;

export class HttpTimeoutError extends Error {
  readonly timedOut = true;

  constructor(timeoutMs: number) {
    super(`request exceeded ${timeoutMs}ms`);
    this.name = "HttpTimeoutError";
  }
}

/** Thrown when the caller's own signal aborted the request. */
export class HttpCancelledError extends Error {
  readonly timedOut = false;

  constructor() {
    super("request cancelled by caller");
    this.name = "HttpCancelledError";
  }
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

export interface FetchWithTimeoutOptions {
  /** Caller-owned cancellation, typically aborted on unmount. */
  signal?: AbortSignal;
  timeoutMs?: number;
}

/**
 * `fetch` with a deadline and optional external cancellation.
 *
 * Throws `HttpTimeoutError` when the deadline passes and `HttpCancelledError`
 * when the caller's signal aborts. A response of any status is returned as-is:
 * status handling belongs to the caller, which has the error-code mapping its
 * own surface needs.
 */
export async function fetchWithTimeout(
  input: string,
  init: RequestInit = {},
  { signal, timeoutMs = REQUEST_TIMEOUT_MS }: FetchWithTimeoutOptions = {},
): Promise<Response> {
  const controller = new AbortController();
  let timedOut = false;

  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  const forwardAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) {
      clearTimeout(timer);
      throw new HttpCancelledError();
    }
    signal.addEventListener("abort", forwardAbort, { once: true });
  }

  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (err) {
    if (isAbort(err)) {
      throw timedOut ? new HttpTimeoutError(timeoutMs) : new HttpCancelledError();
    }
    throw err;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", forwardAbort);
  }
}
