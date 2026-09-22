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
const PAGES = [
  { path: "/conversations", expect: "会话列表" },
  { path: "/knowledge", expect: "文档" },
  { path: "/usage", expect: "已用运行数" },
  { path: "/quality", expect: null },
  { path: "/cases", expect: null },
  { path: "/support", expect: null },
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
    const clean = problems.length === 0 && !missing;
    if (!clean) failures += 1;

    const shot = path.join(OUT, `render${target.path.replace(/\//g, "_")}.png`);
    await page.screenshot({ path: shot, fullPage: true });

    console.log(
      `${clean ? "OK   " : "FAIL "}${target.path}  errors=${problems.length}` +
        `${missing ? ` missing=${JSON.stringify(target.expect)}` : ""}  shot=${shot}`,
    );
    if (!clean) {
      console.log(`      head: ${body.replace(/\s+/g, " ").slice(0, 200)}`);
      for (const problem of problems.slice(0, 5)) {
        console.log(`      !! ${problem.slice(0, 200)}`);
      }
    }
    await context.close();
  }

  await browser.close();
  console.log(failures === 0 ? "all pages rendered cleanly" : `${failures} page(s) failed`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((error) => {
  console.error(`render check could not run: ${error.message}`);
  process.exit(2);
});
