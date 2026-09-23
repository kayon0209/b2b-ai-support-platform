/**
 * Admin UI render check.
 *
 * What a build cannot tell you. `tsc -b && vite build` proves the types line
 * up and the bundle compiles; it says nothing about whether a page renders.
 * This opens each page in a real Chromium, fails on any console or page error,
 * and writes a screenshot so the layout can be eyeballed.
 *
 * Written the day a new page was added and had never once been rendered: the
 * build was green and the API returned 200, and neither of those would have
 * caught a runtime error in the render path.
 *
 * Usage:
 *   node scripts/admin_render_check.cjs
 *   ADMIN_BASE_URL=http://127.0.0.1:5173 node scripts/admin_render_check.cjs
 *
 * Environment:
 *   ADMIN_BASE_URL        default http://127.0.0.1:5173
 *   PLAYWRIGHT_CHROMIUM   explicit chrome.exe path. Only needed when the
 *                         installed playwright-core expects a browser revision
 *                         that is not the one on disk - pinning the binary then
 *                         avoids a several-hundred-MB download for a check.
 *   RENDER_CHECK_OUT      screenshot directory, default the system temp dir.
 *
 * Requires the dev server to be running and `playwright-core` resolvable
 * (NODE_PATH to a node_modules that has it).
 */

const os = require("os");
const path = require("path");

const { chromium } = require("playwright-core");

const BASE = process.env.ADMIN_BASE_URL || "http://127.0.0.1:5173";
const OUT = process.env.RENDER_CHECK_OUT || os.tmpdir();

/**
 * `expect` is a string that must appear in the rendered text - the cheapest
 * proof that real data arrived rather than an empty state or an error banner.
 */
// Every page an operator can reach, not a sample of them.
//
// This list held six entries against a fourteen-item sidebar, so nine operator
// pages were never rendered by the only guard that renders pages. That gap was
// found on 2026-09-23 while restructuring the route table (the console became a
// pathless layout route so `/` could host the customer landing page): the
// change touches **every** operator URL, and the guard could not have told
// anyone if it had broken one of the nine.
//
// `expect` is a Chinese string from `lib/i18n.tsx`, which is the console's
// default language, and it is the page's own heading or a value it always
// renders - so a page that mounts but fails to fetch is still caught. Pages
// whose content depends on live data that may legitimately be empty use `null`
// and are checked for errors only.
const PAGES = [
  { path: "/admin/quality", expect: null },
  { path: "/admin/gaps", expect: "知识缺口" },
  { path: "/admin/knowledge", expect: "文档" },
  { path: "/admin/conversations", expect: "会话列表" },
  { path: "/admin/prompts", expect: null },
  { path: "/admin/flags", expect: null },
  { path: "/admin/channels", expect: null },
  { path: "/admin/experiments", expect: null },
  { path: "/admin/cases", expect: null },
  { path: "/admin/workbench", expect: null },
  { path: "/admin/approvals", expect: null },
  { path: "/admin/members", expect: null },
  { path: "/admin/usage", expect: "已用运行数" },
  { path: "/admin/branding", expect: null },
  // A legacy address still has to resolve, or every bookmark and every
  // documented link breaks. One entry proves the redirect routes are wired; the
  // pages themselves are covered above at their real address.
  { path: "/cases", expect: null },
  // The customer surfaces. `/support` is the product's customer window; `/` is
  // its landing page; `/support/*` is the customer's own 404, which must not
  // show the operator sidebar.
  { path: "/support", expect: null, expectAbsent: ".sidebar" },
  { path: "/", expect: "开始对话", expectAbsent: ".sidebar" },
  { path: "/support/nope", expect: "这个页面不存在", expectAbsent: ".sidebar" },
  // The case this whole prefix exists for: a customer who mistypes the
  // support address must not land on the operator console.
  { path: "/suport", expect: null, expectAbsent: ".sidebar" },
];

async function main() {
  const launchOptions = process.env.PLAYWRIGHT_CHROMIUM
    ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM }
    : {};
  const browser = await chromium.launch(launchOptions);
  let failures = 0;

  for (const target of PAGES) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    const problems = [];
    page.on("console", (message) => {
      if (message.type() === "error") problems.push(`console: ${message.text()}`);
    });
    page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));

    await page.goto(`${BASE}${target.path}`, { waitUntil: "load" });
    // These screens fetch on mount; the wait is what makes the difference
    // between checking the shell and checking what the operator will see.
    await page.waitForTimeout(2000);

    const body = await page.innerText("body").catch(() => "");
    const missing = target.expect ? !body.includes(target.expect) : false;
    // `expectAbsent` is the other half of a customer-surface assertion: it is
    // not enough that the page rendered, it must not have rendered the
    // operator's shell. The sidebar was the actual defect on the mistyped-URL
    // 404 (2026-09-23), so a check that only looked for the page's own text
    // would have passed while the bug was live.
    const leaked = target.expectAbsent
      ? (await page.locator(target.expectAbsent).count()) > 0
      : false;
    const clean = problems.length === 0 && !missing && !leaked;
    if (!clean) failures += 1;

    const shot = path.join(OUT, `render${target.path.replace(/\//g, "_")}.png`);
    await page.screenshot({ path: shot, fullPage: true });

    console.log(
      `${clean ? "OK   " : "FAIL "}${target.path}  errors=${problems.length}` +
        `${missing ? ` missing=${JSON.stringify(target.expect)}` : ""}` +
        `${leaked ? ` leaked=${target.expectAbsent}` : ""}  shot=${shot}`,
    );
    if (!clean) {
      console.log(`      head: ${body.replace(/\s+/g, " ").slice(0, 200)}`);
      for (const problem of problems.slice(0, 5)) {
        console.log(`      !! ${problem.slice(0, 200)}`);
      }
    }
    await context.close();
  }

  // Bounded on purpose. `browser.close()` has been observed to hang on this
  // box *after* the last page is rendered and logged, leaving the process
  // alive with no verdict printed — and a check that hangs reports nothing,
  // which is worse than one that fails. The work is finished by here, so a
  // timeout costs nothing and guarantees the result is emitted.
  await Promise.race([
    browser.close(),
    new Promise((resolve) => setTimeout(resolve, 10_000)),
  ]);
  console.log(failures === 0 ? "all pages rendered cleanly" : `${failures} page(s) failed`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((error) => {
  console.error(`render check could not run: ${error.message}`);
  process.exit(2);
});
