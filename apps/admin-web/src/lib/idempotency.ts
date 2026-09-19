/**
 * Idempotency keys for write commands.
 *
 * Every write to the control plane needs a fresh key per submit attempt, so
 * a double-click sends one change and a retry after a network error still
 * sends one change. One helper, because the fallback matters:
 * `crypto.randomUUID` only exists in a secure context (https or
 * localhost), and an internal deployment reached over plain http is not one
 * — there it is `undefined` and the call throws, which took out the quota
 * and billing-correction forms entirely while every other page worked.
 */
export function newIdempotencyKey(): string {
  return crypto.randomUUID?.() ?? `idem-${Date.now()}-${Math.random()}`;
}
