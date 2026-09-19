import { useEffect, useState } from "react";

/**
 * Delay a rapidly-changing value.
 *
 * The prompt template field drives two API calls on every keystroke, so
 * typing a 20-character template name issued ~40 requests, and clearing the
 * field mid-edit produced a 422 banner for a state the operator never
 * intended to submit. Debouncing keeps the input responsive and the request
 * count proportional to what the operator actually settled on.
 */
export function useDebounced<T>(value: T, delayMs = 250): T {
  const [settled, setSettled] = useState(value);

  useEffect(() => {
    const id = window.setTimeout(() => setSettled(value), delayMs);
    return () => window.clearTimeout(id);
  }, [value, delayMs]);

  return settled;
}
