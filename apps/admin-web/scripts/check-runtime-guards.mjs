#!/usr/bin/env node
/**
 * Runtime guards that `tsc` cannot express.
 *
 * Why this exists rather than a test framework: the defect this catches has
 * already happened once in this repository. `lib/idempotency.ts` documents it
 * in its own header - `crypto.randomUUID` only exists in a secure context, and
 * a deployment reached over plain http crashed on it, taking the quota and
 * billing forms down while every other page kept working. That fix was a local
 * fallback inside one helper; the customer chat surface kept calling
 * `crypto.randomUUID()` directly and inherited the same crash.
 *
 * Two guards, because the two failure modes differ:
 *
 *  1. Behaviour - with `crypto.randomUUID` absent, an id generator must still
 *     return a usable, unique, length-bounded string. This is the actual bug.
 *  2. Structure - no page may call `crypto.randomUUID(` directly. A page that
 *     does is one refactor away from the crash the helper already fixed, and a
 *     type checker will never flag it, because the call is perfectly typed.
 *
 * Run: `npm run check:runtime` (needs Node 22 for TypeScript stripping).
 */

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const srcDir = join(here, "..", "src");
const failures = [];

function check(name, fn) {
  try {
    fn();
    console.log(`  ok    ${name}`);
  } catch (err) {
    failures.push(`${name} -- ${err.message}`);
    console.log(`  FAIL  ${name}\n        ${err.message}`);
  }
}

function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) return walk(full);
    return /\.tsx?$/.test(entry.name) ? [full] : [];
  });
}

/**
 * Strip comments before scanning. Without this the guard fires on a comment
 * that merely *names* the forbidden call - which is the one place naming it is
 * useful. A guard that cries wolf on the explanation gets deleted instead of
 * fixed.
 */
function stripComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

// The module reads `crypto` when a function runs, not when it is imported, so
// it is safe to load first and remove the method afterwards.
const ids = await import(pathToFileURL(join(srcDir, "lib", "idempotency.ts")).href);
const withBrokenCrypto = (fn) => {
  const original = globalThis.crypto.randomUUID;
  delete globalThis.crypto.randomUUID;
  try {
    return fn();
  } finally {
    globalThis.crypto.randomUUID = original;
  }
};

withBrokenCrypto(() => {
  check("randomId() exists and returns a usable id in a non-secure context", () => {
    assert.equal(
      typeof ids.randomId,
      "function",
      "lib/idempotency.ts must export randomId(prefix) so pages stop calling crypto directly",
    );
    const id = ids.randomId("visitor");
    assert.equal(typeof id, "string", "randomId must return a string");
    assert.ok(id.length > 0, "randomId must not return an empty string");
  });

  check("randomId() output fits the 64-char visitor_id field", () => {
    const id = ids.randomId("visitor");
    assert.ok(
      id.length <= 64,
      `visitor_id is capped at 64 characters server-side; got ${id.length}`,
    );
  });

  check("randomId() does not repeat across calls", () => {
    // The conversation ref is derived from the visitor id, so a repeated id
    // would silently merge two different visitors into one conversation.
    assert.notEqual(ids.randomId("visitor"), ids.randomId("visitor"));
  });

  check("newIdempotencyKey() still works without crypto.randomUUID", () => {
    const key = ids.newIdempotencyKey();
    assert.ok(key.length > 0, "idempotency key must not be empty");
  });
});

check("no page calls crypto.randomUUID() directly", () => {
  const offenders = [];
  for (const file of walk(srcDir)) {
    if (/crypto\.randomUUID\s*\(/.test(stripComments(readFileSync(file, "utf8")))) {
      offenders.push(relative(srcDir, file));
    }
  }
  assert.equal(
    offenders.length,
    0,
    `these files call crypto.randomUUID() and crash in a non-secure context: ${offenders.join(", ")}; ` +
      "use randomId()/newIdempotencyKey() from lib/idempotency.ts instead",
  );
});

check("no page calls fetch() without a timeout", () => {
  // `fetch` has no default timeout: a request that never answered leaves the
  // UI waiting forever with no error and nothing to cancel. lib/http.ts owns
  // the fix, so a page reaching for `fetch` itself has opted out of it.
  const offenders = [];
  for (const file of walk(join(srcDir, "pages"))) {
    if (/(^|[^.\w])fetch\s*\(/.test(stripComments(readFileSync(file, "utf8")))) {
      offenders.push(relative(srcDir, file));
    }
  }
  assert.equal(
    offenders.length,
    0,
    `these pages call fetch() directly and can hang forever: ${offenders.join(", ")}; ` +
      "use fetchWithTimeout() from lib/http.ts instead",
  );
});

if (failures.length > 0) {
  console.error(`\n${failures.length} runtime guard(s) failed:`);
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("\nall runtime guards passed");
