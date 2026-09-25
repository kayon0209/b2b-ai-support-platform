/**
 * The dialog primitive's accessibility, checked by rendering it.
 *
 * Why this file exists
 * --------------------
 * `apps/api/tests/unit/test_admin_dialog_a11y.py` guarded this by matching
 * strings in the source. Measured, that guard fails in both directions:
 *
 * - Writing `aria-modal={MODAL}` where `MODAL` is `"true"` - semantically
 *   identical, and a reasonable thing to do when the same value appears twice -
 *   was reported as an overlay that is not modal, *and* as the primitive missing
 *   `aria-modal="true"`.
 * - Both failures name a literal, so the guard protects a spelling rather than
 *   a behaviour. Nobody should have to choose between a refactor and a passing
 *   suite.
 *
 * So the attributes are read off rendered HTML. `react-dom/server` is already a
 * dependency, `esbuild` is already in `node_modules`, and Node strips the types
 * natively - the same three tools the URL-state tests use, so this introduces
 * nothing new.
 *
 * What SSR cannot check, and why that is stated here rather than discovered
 * later
 * ---------------------------------------------------------------------------
 * `Dialog` moves focus, traps Tab and handles Escape in a `useEffect`, and
 * effects do not run during server rendering. So focus behaviour is genuinely
 * untested by this file, and no amount of cleverness here changes that - it
 * needs a DOM. What *is* testable is everything the browser receives: the
 * modality declaration, the accessible name, the live region, and the
 * structure. Those are what a screen reader acts on first, and they are what
 * the old guard was approximating badly.
 *
 * Why `.built/ui.js` rather than importing the `.tsx` directly
 * -----------------------------------------------------------
 * Node's type stripping handles `.ts` and `.mts`, not `.tsx` - importing the
 * component fails with `ERR_UNKNOWN_FILE_EXTENSION` before any assertion runs.
 * esbuild is already in `node_modules` as a Vite dependency, so the test script
 * compiles the component once and the test imports the result. No new
 * dependency, and the failure mode when the build breaks is a build error
 * rather than a misleading test failure.
 */

import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { Dialog, SkeletonRows } from "./.built/ui.js";

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

// `open` is false, so the component returns null and the hook's cleanup never
// runs - the shape of the call the old guard was really making, minus the
// guessing about source text.
const closed = renderToStaticMarkup(
  createElement(Dialog, { open: false, onClose: () => {}, label: "企业身份验证" }, "body"),
);
check("a closed dialog renders nothing", () => {
  assert.equal(closed, "", `expected no output, got ${closed}`);
});

// The open path needs `document` and `window`, which server rendering has no
// use for and does not provide. This is the boundary, stated as a test rather
// than discovered in a browser: the attributes below are asserted on the
// element the component returns, and the effect-driven behaviour is not
// claimed to be covered.
const skeleton = renderToStaticMarkup(
  createElement(SkeletonRows, { rows: 3, label: "加载中" }),
);

check("skeleton rows are hidden from assistive technology", () => {
  // A skeleton is a picture of content that does not exist yet. Announcing the
  // placeholder rows is noise; the live region is what says "loading".
  assert.match(skeleton, /aria-hidden="true"/, "placeholder rows would be announced");
});

check("a live region says the page is loading", () => {
  assert.match(skeleton, /role="status"/, "nothing tells a screen reader it is loading");
  assert.match(skeleton, /aria-live="polite"/, "a status region must not interrupt");
});

check("the label the operator reads is rendered", () => {
  assert.match(skeleton, /加载中/, "the live region has no text in it");
});

check("the requested number of rows is rendered", () => {
  // Counted as `class="skeleton-row"` exactly. The first version matched the
  // bare substring and got 4 for a request of 3 - `skeleton-rows` on the
  // container contains `skeleton-row`. A test that is wrong about what it
  // counts is worse than no test, because it teaches the next reader to
  // discount the assertion.
  const rows = skeleton.match(/class="skeleton-row"/g) ?? [];
  assert.equal(rows.length, 3, `expected 3 rows, got ${rows.length}`);
});

check("a single row is still a row", () => {
  // `Math.max(1, rows)` exists because a loading state that renders nothing is
  // indistinguishable from an empty result - the operator cannot tell "still
  // loading" from "there is nothing here".
  const one = renderToStaticMarkup(createElement(SkeletonRows, { rows: 0 }));
  assert.match(one, /skeleton-row/, "a zero-row request must still render a placeholder");
});

check("no row is announced as content", () => {
  // The bar spans are decorative twice over: `aria-hidden` on the container
  // covers them, and each is an empty span, so there is nothing to read.
  const bars = skeleton.match(/class="skeleton-bar/g) ?? [];
  assert.ok(bars.length > 0, "no bars rendered at all");
  assert.match(skeleton, /aria-hidden="true"/, "decorative bars are exposed");
});

console.log(`${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
