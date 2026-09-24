/**
 * Opaque id generation, and the idempotency keys built on it.
 *
 * Every id the browser mints for the platform goes through here, because the
 * fallback is the whole point:
 * `crypto.randomUUID` only exists in a secure context (https or localhost), and
 * an internal deployment reached over plain http is not one — there the method
 * is `undefined` and calling it throws. That took out the quota and
 * billing-correction forms entirely while every other page worked.
 *
 * It is exported as a general generator rather than kept private to
 * `newIdempotencyKey` because the customer chat surface mints ids of its own
 * (the visitor id) and had been calling `crypto.randomUUID()` directly, which
 * reproduced the exact crash this file was written to prevent. A second
 * implementation of the same idea is how it came back. `scripts/check-runtime-guards.mjs`
 * fails the build if a page reaches for `crypto` again.
 *
 * The fallback is not a security boundary and does not need to be: these ids
 * are correlation handles and de-duplication keys, never credentials. What it
 * must guarantee is uniqueness within a tab and a length the server accepts —
 * `visitor_id` is capped at 64 characters server-side, which the runtime guard
 * asserts rather than leaving to a comment.
 */
export function randomId(prefix: string): string {
  return `${prefix}-${crypto.randomUUID?.() ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`}`;
}

/**
 * Idempotency keys for write commands.
 *
 * Every write to the control plane needs a fresh key per submit attempt, so
 * a double-click sends one change and a retry after a network error still
 * sends one change.
 */
export function newIdempotencyKey(): string {
  return randomId("idem");
}
