/**
 * A smoke check for the one defect class the test suite cannot see.
 *
 * Five bugs found in the 2026-09-21 audit had the same shape: the API
 * answered, the page rendered, and the two were not connected. The workbench
 * read `data.cases` from a response that says `items`, so the queue showed
 * "no cases" with 21 rows in the database - and 1741 tests were green, because
 * the generic on `apiGet<T>` is a cast rather than a check and nothing opened
 * the page.
 *
 * So this opens the pages. For each route it records how many rows the list
 * endpoint returned and counts the rows actually rendered, then reports the
 * one contradiction that matters:
 *
 *     the API served rows, and the page rendered none of them
 *
 * Two earlier versions of this file were wrong and both are worth knowing:
 * matching "empty state" copy in the page text flagged pages that were
 * correctly showing a filtered-empty list, and reading the page before the
 * first token-carrying load had settled attributed the startup 401s to
 * whichever route ran first. Row selectors and a settled page are what make
 * this check trustworthy; a guard that cries wolf is as bad as no guard.
 *
 * It needs no project dependency (playwright-core and the browser are already
 * present) and it runs against a live API and dev server, because that is the
 * only place this class appears.
 *
 *     APP_BASE_URL=http://localhost:5173 \
 *     APP_TOKEN=pt_<tenant-slug>_<user-id> \
 *     APP_CHROME=<path to chrome.exe> \
 *     node scripts/ui_smoke.cjs
 */

const { chromium } = require("playwright-core");

const BASE = process.env.APP_BASE_URL || "http://localhost:5173";
const TOKEN = process.env.APP_TOKEN || "";
const CHROME = process.env.APP_CHROME || "";
const CHROME_FALLBACK =
  "C:/Users/Rose/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";

/**
 * route, label, the list endpoint that feeds it, and the selector its rows
 * render into. The selector is the whole point: without it there is no way to
 * tell "the page ignored the data" from "the page has nothing to show".
 */
const ROUTES = [
  ["/admin/cases", "工单", "/v1/cases", ".case-row"],
  ["/admin/workbench", "坐席工作台", "/v1/cases", ".workbench-item"],
  ["/admin/gaps", "知识缺口", "/v1/knowledge/gaps", "tbody tr"],
  ["/admin/prompts", "提示词发布", "/v1/prompts", "tbody tr"],
  ["/admin/flags", "功能开关", "/v1/flags", "tbody tr"],
  ["/admin/members", "成员", "/v1/identity/members", "tbody tr"],
  ["/admin/approvals", "写操作审批", "/v1/tool-proposals", ".case-row"],
  // Pages with no list to count: they must still load without a failed call.
  ["/admin/quality", "质量看板", "/v1/quality/metrics", null],
  ["/admin/usage", "用量", "/v1/tenant/usage", null],
  ["/admin/branding", "品牌", "/v1/tenant/branding", null],
];

async function main() {
  if (!TOKEN) {
    console.error("APP_TOKEN is required (pt_<tenant-slug>_<user-id>)");
    return 2;
  }
  const browser = await chromium.launch({
    executablePath: CHROME || CHROME_FALLBACK,
    headless: true,
  });
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
  page.setDefaultTimeout(8000);

  // Boot with the token already in place, so the startup calls are not
  // token-less: their 401s belong to the boot, not to whichever route happens
  // to be visited first.
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.evaluate((t) => localStorage.setItem("b2b_token", t), TOKEN);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2000);

  const failures = [];
  const notes = [];

  for (const [route, label, listPath, rowSelector] of ROUTES) {
    const responses = [];
    const onResponse = async (res) => {
      const url = res.url();
      if (url.indexOf("/api/") < 0) return;
      let rows = null;
      try {
        const body = await res.json();
        if (body && Array.isArray(body.items)) rows = body.items.length;
      } catch {
        /* not JSON: nothing to compare */
      }
      // Normalised to the control plane's own path: the console reaches the
      // API through the `/api` prefix, and comparing the raw URL against
      // `/v1/cases` silently matched nothing - which made `served` always 0
      // and the check below unable to fail. A guard that cannot fail is worse
      // than no guard, so the prefix is stripped here rather than trusted.
      const path = url.replace(BASE, "").replace(/^\/api/, "");
      responses.push({ status: res.status(), url: path, rows });
    };
    page.on("response", onResponse);

    await page.goto(BASE + route, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2600);
    page.off("response", onResponse);

    const failed = responses.filter((r) => r.status >= 400);
    if (failed.length) {
      failures.push(
        route +
          " (" +
          label +
          "): request(s) failed:\n       " +
          failed.map((r) => r.status + " " + r.url).join("\n       "),
      );
      continue;
    }

    if (!rowSelector) {
      notes.push("ok   " + route.padEnd(12) + " loaded, no failed request");
      continue;
    }

    const served = responses
      .filter((r) => r.url.indexOf(listPath) === 0 || r.url.indexOf(listPath + "?") === 0)
      .reduce((max, r) => Math.max(max, r.rows === null ? 0 : r.rows), 0);
    const rendered = await page.locator(rowSelector).count();

    if (served > 0 && rendered === 0) {
      failures.push(
        route +
          " (" +
          label +
          "): " +
          listPath +
          " returned " +
          served +
          " row(s) and the page rendered 0 (" +
          rowSelector +
          ")",
      );
    } else {
      notes.push(
        "ok   " + route.padEnd(12) + " served=" + served + " rendered=" + rendered + " (" + rowSelector + ")",
      );
    }
  }

  await browser.close();

  for (const note of notes) console.log(note);
  console.log("");
  for (const failure of failures) console.log("FAIL " + failure);
  console.log(
    "\nSUMMARY " +
      (ROUTES.length - failures.length) +
      "/" +
      ROUTES.length +
      " routes consistent" +
      (failures.length ? " - a page that renders nothing while its endpoint serves rows is the F-5 defect" : ""),
  );
  return failures.length ? 1 : 0;
}

main().then(
  (code) => process.exit(code),
  (err) => {
    console.error("SMOKE ERROR: " + (err && err.message));
    process.exit(2);
  },
);
