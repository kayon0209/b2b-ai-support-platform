import { useCallback, useState } from "react";

export interface ActionState {
  /** The last failure, already unwrapped from `ApiError`. */
  error: string | null;
  /** The last success, when the caller supplied a message for it. */
  notice: string | null;
  /** True while the action is in flight, for disabling the trigger. */
  busy: boolean;
  run: (fn: () => Promise<void>, successMessage?: string) => Promise<boolean>;
  /** Report a client-side refusal through the same channel as a server one. */
  fail: (message: string) => void;
  clear: () => void;
}

/**
 * Feedback for a write action, rendered in the page instead of in a modal.
 *
 * These pages used `alert()`, which has three problems that matter here: it
 * blocks the whole tab, it cannot be styled, and it is not announced by a
 * screen reader as a live region the way an inline banner is. An operator
 * performing a sequence of Case commands had to dismiss a dialog per step.
 *
 * `run` returns whether the action succeeded, so a caller can keep the
 * "reload after success" behaviour without duplicating the try/catch.
 */
export function useAction(): ActionState {
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const run = useCallback(
    async (fn: () => Promise<void>, successMessage?: string): Promise<boolean> => {
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        await fn();
        if (successMessage) setNotice(successMessage);
        return true;
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const fail = useCallback((message: string) => {
    setNotice(null);
    setError(message);
  }, []);

  const clear = useCallback(() => {
    setError(null);
    setNotice(null);
  }, []);

  return { error, notice, busy, run, fail, clear };
}
