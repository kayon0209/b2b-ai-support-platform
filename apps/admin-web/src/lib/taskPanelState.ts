/**
 * The two concurrency rules the task panel has to hold, as executable
 * functions.
 *
 * Why extracted rather than tested through the component
 * ------------------------------------------------------
 * `apps/admin-web` has no DOM environment, and this repository's established
 * answer to that is the one `url-state.test.mts` documents: move the decision
 * into a pure function and execute it, rather than matching source text. The
 * acceptance review found both defects below by reading the component, and
 * both are invisible to a type checker.
 *
 * 1. **A response for a conversation the operator has left must not land in
 *    the panel for the new one.** Two conversations open in sequence produce
 *    two overlapping requests; the slower one can resolve second and repopulate
 *    the panel with another customer's tasks.
 *
 * 2. **Two tasks waiting for the same field must not share one input.** Two
 *    tasks on one screen can both be waiting for `street`; keyed by field name
 *    alone, one task's typed value appears in the other's input and is
 *    submitted against the wrong task.
 *
 * The component keeps the wiring; these are the decisions.
 */

/**
 * The request counter the component keeps in a ref.
 *
 * A mutable object rather than a number so `nextSeq` can advance it and
 * `mayApply` can read it, with the same shape in the component and in the
 * test - two representations would be two things to keep in step.
 */
export type RequestSeq = { current: number };

export function nextSeq(seq: RequestSeq): number {
  seq.current += 1;
  return seq.current;
}

/**
 * Whether a response that started under `startedAt` may still be applied.
 *
 * False when a newer request has begun, or when the conversation changed since
 * the request was issued - the second check is what catches a response that
 * was already in flight when the operator navigated.
 */
export function mayApply(
  seq: RequestSeq,
  startedAt: number,
  startedFor: string,
  currentFor: string,
): boolean {
  if (startedAt !== seq.current) return false;
  return startedFor === currentFor;
}

/** The key one task's field value is stored under. */
export function collectKey(taskId: string, field: string): string {
  return `${taskId}:${field}`;
}

/**
 * Read one task's collected values out of the shared map, in `missing` order.
 *
 * Passing `missing` rather than every key in the map is deliberate: the
 * command sends only the fields the task is actually waiting for, and a batch
 * naming anything else is refused by the server.
 */
export function collectedFor(
  collecting: Record<string, string>,
  taskId: string,
  missing: string[],
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const field of missing) {
    out[field] = collecting[collectKey(taskId, field)] ?? "";
  }
  return out;
}

/** Whether every missing field has a non-blank value. */
export function allCollected(collecting: Record<string, string>, taskId: string, missing: string[]): boolean {
  return missing.every((field) => (collecting[collectKey(taskId, field)] ?? "").trim() !== "");
}
