/**
 * Behaviour tests for the URL-state decisions, run by Node's type stripping.
 *
 * Why this file exists
 * --------------------
 * `apps/api/tests/unit/test_admin_deep_links.py` guarded these decisions by
 * matching strings in the source: `assert "push = false" in body`. Renaming that
 * variable to `replaceByDefault` failed the test with the behaviour completely
 * unchanged, and passing the test by keeping a name is not what a guard is for.
 * Verified rather than assumed - the rename was made, the test failed, the
 * rename was reverted.
 *
 * The fix is structural rather than a better regex. The decisions the hook
 * makes are now pure functions - `applyUrlValue`, `readUrlValue`, `isOneOf` -
 * so they can be executed. The hook still exists for the React and router
 * wiring, and the wiring is still checked; what is no longer checked by string
 * is the behaviour.
 *
 * Why Node rather than a test framework
 * -------------------------------------
 * `apps/admin-web` has no test runner and no DOM environment. Adding vitest and
 * jsdom for three pure functions would be a larger change than the thing being
 * guarded, and the project's own rule is not to introduce an abstraction before
 * a second caller needs it. Node 22 strips types natively, so the functions run
 * as written - no build step, no new dependency, and a failure here is a real
 * failure rather than a source-text mismatch.
 */

import assert from "node:assert/strict";

import {
  applyUrlValue,
  isOneOf,
  readUrlValue,
} from "../src/lib/urlState.ts";

const ALL_TABS = ["queue", "mine", "waiting"] as const;

let failures = 0;
let passes = 0;

function check(name: string, body: () => void): void {
  try {
    body();
    passes += 1;
  } catch (error) {
    failures += 1;
    console.error(`FAIL ${name}`);
    console.error(`     ${error instanceof Error ? error.message : String(error)}`);
  }
}

check("an empty value removes the parameter rather than writing ?q=", () => {
  const result = applyUrlValue(new URLSearchParams("q=hello"), "q", "");
  assert.equal(result.has("q"), false, `expected no q parameter, got ${result}`);
});

check("null also removes it", () => {
  const result = applyUrlValue(new URLSearchParams("q=hello"), "q", null);
  assert.equal(result.has("q"), false);
});

check("a real value is set", () => {
  const result = applyUrlValue(new URLSearchParams(), "q", "acme");
  assert.equal(result.get("q"), "acme");
});

check("setting a value replaces the previous one", () => {
  const result = applyUrlValue(new URLSearchParams("q=old"), "q", "new");
  assert.equal(result.get("q"), "new");
});

check("other parameters survive a change", () => {
  // This is the one a naive implementation gets wrong: rebuilding the query
  // from scratch would silently reset the operator's tab while they type.
  const result = applyUrlValue(new URLSearchParams("tab=mine&q=old"), "q", "new");
  assert.equal(result.get("tab"), "mine", "tab must survive");
  assert.equal(result.get("q"), "new");
});

check("an absent parameter reads as the fallback", () => {
  assert.equal(readUrlValue(new URLSearchParams("other=1"), "q", "queue"), "queue");
});

check("an empty parameter also reads as the fallback", () => {
  // `?q=` and no `q` are the same request; treating them differently makes a
  // shared link behave differently from the page it was copied from.
  assert.equal(readUrlValue(new URLSearchParams("q="), "q", "queue"), "queue");
});

check("a present value is returned as-is", () => {
  assert.equal(readUrlValue(new URLSearchParams("q=acme"), "q", "queue"), "acme");
});

check("a known tab is recognised", () => {
  assert.equal(isOneOf("queue", ALL_TABS), true);
});

check("a hand-edited tab falls back instead of being rendered", () => {
  // `?tab=whatever` is one edit away. The page must render its default, not a
  // state no branch handles - which shows as an empty panel, and an empty panel
  // reads as "there are no cases" rather than "that link is wrong".
  assert.equal(isOneOf("whatever", ALL_TABS), false);
});

check("the case matters, so Queue is not queue", () => {
  assert.equal(isOneOf("Queue", ALL_TABS), false);
});

// The mutation check: `applyUrlValue` must not edit the caller's object. It
// returns a new one, and a future in-place optimisation would make two pages
// sharing a params object overwrite each other's state.
check("the input parameters are not mutated", () => {
  const original = new URLSearchParams("q=hello&tab=mine");
  applyUrlValue(original, "q", "changed");
  assert.equal(original.get("q"), "hello", "the caller's params were edited");
  assert.equal(original.get("tab"), "mine");
});

console.log(`${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
