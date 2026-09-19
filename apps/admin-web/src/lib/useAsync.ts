import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./types";

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  /**
   * HTTP status of the failure, when it was an API error.
   *
   * `error` alone is not enough to react to a failure: a page that wants to
   * say "you do not have permission to see this section" has to distinguish
   * a 403 from a 500, and the message text is the server's to choose.
   */
  errorStatus: number | null;
  reload: () => void;
}

/**
 * Run an async loader on mount and whenever `reload()` is called. The loader
 * receives a signal so a slow request from a previous render is ignored.
 */
export function useAsync<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  // Drop the previous result when the query itself changes (a different
  // status filter, a different case id). Keeping it meant the table showed
  // rows belonging to the *previous* query while the new one was in flight,
  // which reads as "my filter did nothing".
  //
  // A manual `reload()` deliberately keeps the current rows: replacing them
  // with a spinner on every refresh makes the page flicker for no reason.
  const depKey = JSON.stringify(deps);
  const lastDepKey = useRef(depKey);
  useEffect(() => {
    if (lastDepKey.current === depKey) return;
    lastDepKey.current = depKey;
    setData(null);
  }, [depKey]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    setErrorStatus(null);
    loader()
      .then((result) => {
        if (active) setData(result);
      })
      .catch((err: unknown) => {
        if (!active) return;
        setError(err instanceof Error ? err.message : String(err));
        setErrorStatus(err instanceof ApiError ? err.status : null);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  return { data, loading, error, errorStatus, reload };
}
