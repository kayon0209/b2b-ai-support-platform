import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

/**
 * One place for "this bit of view state belongs in the URL".
 *
 * The gap this closes
 * -------------------
 * Three pieces of operator state lived in `useState` and were therefore lost on
 * refresh and absent from a pasted link: which conversation a replay was
 * showing, which queue tab the workbench was on, and what had been typed into
 * the queue search. The workbench already put `caseId` and `conversationRef` in
 * the path - so half the page was linkable and half was not, and a colleague
 * asking "can you send me that conversation" could only get the page, not the
 * conversation.
 *
 * Why one hook rather than three call sites
 * -----------------------------------------
 * The behaviour that matters is not "read a query parameter"; it is the set of
 * decisions below, and they are the ones that get re-decided differently each
 * time. Centralised, they are made once:
 *
 * - **`replace`, never `push`, by default.** A search box pushes an entry per
 *   keystroke, so Back walks through the letters of the last word typed. A
 *   conversation list pushes an entry per row clicked, so Back walks through
 *   every conversation the operator looked at. Neither is what anybody means by
 *   Back here. `push: true` exists for the rare case that genuinely is
 *   navigation.
 *
 * - **An empty value removes the parameter** rather than writing `?q=`. A
 *   link with `?q=` looks like a filter that is set to nothing, and reads as a
 *   different request than one with no parameter at all.
 *
 * - **The stored value is a string.** Every consumer here wants a string; doing
 *   the conversion at the boundary means a caller cannot accidentally compare a
 *   number to a string and get "always different".
 *
 * - **Unknown values fall back to the caller's default** rather than being
 *   passed through. A hand-edited `?tab=whatever` must not put the page into a
 *   state no code path can reach.
 */
/**
 * Apply a value to a query string, returning the new one.
 *
 * Extracted from the hook so the decisions it makes can be tested without a
 * browser. The hook needs React and a router; this needs neither, and the tests
 * that used to assert on the hook's *source text* could not tell a rename from
 * a behaviour change - renaming `push` to something else failed them while the
 * behaviour was identical, which teaches people to protect names instead of
 * behaviour.
 *
 * The three decisions, in one place and testable:
 * - `null` and `""` both remove the parameter, so a cleared search does not
 *   leave behind `?q=` that reads as a filter set to nothing.
 * - Anything else is set, replacing whatever was there.
 * - Other parameters are preserved: a link with `?tab=queue&q=x` must keep
 *   `tab` when the search changes, and dropping it would silently reset the
 *   operator's view.
 */
export function applyUrlValue(
  current: URLSearchParams,
  key: string,
  next: string | null,
): URLSearchParams {
  const updated = new URLSearchParams(current);
  if (next === null || next === "") updated.delete(key);
  else updated.set(key, next);
  return updated;
}

/**
 * Read a value, treating "absent" and "empty" as the same thing.
 *
 * `?q=` and no `q` at all mean the same request, and a caller asking for the
 * fallback should get it for both.
 */
export function readUrlValue(
  params: URLSearchParams,
  key: string,
  fallback: string,
): string {
  const stored = params.get(key);
  if (stored === null || stored === "") return fallback;
  return stored;
}

export function useUrlState(
  key: string,
  fallback: string,
  options: { push?: boolean } = {},
): readonly [string, (next: string | null) => void] {
  const { push = false } = options;
  const [params, setParams] = useSearchParams();
  const value = useMemo(() => readUrlValue(params, key, fallback), [params, key, fallback]);

  const set = useCallback(
    (next: string | null) => {
      setParams(
        (current) => applyUrlValue(current, key, next),
        { replace: !push },
      );
    },
    [key, push, setParams],
  );

  return [value, set] as const;
}

/**
 * Whether `candidate` is one of the states this page knows how to render.
 *
 * Exported because the answer is a page decision, not a URL decision: a value
 * outside the set means the link was hand-edited or written by an older build,
 * and the honest response is to render the default rather than an empty panel
 * with no explanation.
 */
export function isOneOf<T extends string>(
  candidate: string,
  allowed: readonly T[],
): candidate is T {
  return (allowed as readonly string[]).includes(candidate);
}
