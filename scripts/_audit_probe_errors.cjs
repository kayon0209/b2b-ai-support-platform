/**
 * 报错分支探针：把「后台不可达」和「租户不存在」两种情况在真实浏览器里跑出来。
 *
 * 「不可达」用 Playwright 的请求拦截模拟，而不是去停用户正在跑的服务 ——
 * 同样能拿到真实的 UI 反应，且不打扰用户环境。
 */

const fs = require("fs");
const path = require("path");

const { chromium } = require("C:/Users/Rose/.workbuddy/binaries/node/workspace/node_modules/playwright-core");

const CHROME =
  "D:/migrated/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const BASE = "http://127.0.0.1:5173";
const OUT = "D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan/outputs/journey-audit";

fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function dump(page, label, file) {
  const text = (await page.locator("body").innerText()).replace(/\n{2,}/g, "\n").trim();
  const composerDisabled = await page
    .locator(".support-input")
    .isDisabled()
    .catch(() => null);
  const sendDisabled = await page
    .locator(".support-send")
    .isDisabled()
    .catch(() => null);
  const retry = await page.locator(".support-problem-retry").count();
  // The error bar must be announced, and it must carry a way out. Both were
  // missing: the old failure state was a red line and a disabled composer, so
  // the only recovery was guessing that a refresh might help.
  const alertRole =
    (await page.locator(".support-problem").getAttribute("role").catch(() => null)) ?? null;
  const retryText = await page
    .locator(".support-problem-retry")
    .innerText()
    .catch(() => null);
  console.log(`\n===== ${label} =====`);
  console.log(text);
  console.log(`  composer 输入框 disabled = ${composerDisabled}`);
  console.log(`  发送按钮 disabled = ${sendDisabled}`);
  console.log(`  错误块 role = ${JSON.stringify(alertRole)}（应为 "alert"）`);
  console.log(`  重试按钮 = ${JSON.stringify(retryText)}（count=${retry}）`);
  await page.screenshot({ path: path.join(OUT, file), fullPage: true });
  console.log(`  [截图] ${file}`);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });

  // --- 1. 控制平面不可达 ---
  const c1 = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const p1 = await c1.newPage();
  await p1.route("**/api/**", (route) => route.abort("connectionrefused"));
  await p1.goto(BASE + "/support", { waitUntil: "domcontentloaded" });
  await sleep(5000);
  await dump(p1, "后台不可达（所有 /api 请求被拒）", "C1-后台不可达.png");
  await c1.close();

  // --- 2. 租户不存在 ---
  const c2 = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const p2 = await c2.newPage();
  await p2.goto(BASE + "/support?tenant=nope-nope", { waitUntil: "domcontentloaded" });
  await sleep(5000);
  await dump(p2, "租户不存在（?tenant=nope-nope）", "C2-租户不存在.png");
  await c2.close();

  // --- 3. 无 tenant 参数（默认 admin-demo）---
  const c3 = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const p3 = await c3.newPage();
  await p3.goto(BASE + "/support", { waitUntil: "domcontentloaded" });
  await sleep(4500);
  const h1 = await p3.locator(".support-title").innerText();
  const sub = await p3.locator(".support-status").innerText();
  console.log("\n===== 无 tenant 参数（默认落入 admin-demo） =====");
  console.log(`  H1 = ${JSON.stringify(h1)}`);
  console.log(`  状态 = ${JSON.stringify(sub)}`);
  await c3.close();

  await browser.close();
  process.exit(0);
})().catch((e) => {
  console.error("探针失败：", e);
  process.exit(1);
});
