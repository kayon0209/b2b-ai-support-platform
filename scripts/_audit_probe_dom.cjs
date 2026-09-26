/**
 * 定点核对探针：把肉眼在低分辨率截图里读不准的文案、以及 WCAG 需要实测的
 * 计算样式（placeholder 颜色、字号），直接从真实 DOM 取回来。
 *
 * 一次运行，两次用途 —— 避免"凭截图下结论"。
 */

const { chromium } = require("C:/Users/Rose/.workbuddy/binaries/node/workspace/node_modules/playwright-core");

const CHROME =
  "D:/migrated/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const BASE = process.env.APP_BASE_URL || "http://127.0.0.1:5173";
// From the environment, like every other probe here - a committed script that
// carries its own token is a credential in the repository.
const TOKEN = process.env.APP_TOKEN || "";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  if (!TOKEN) {
    console.error("APP_TOKEN is required (pt_<tenant-slug>_<user-id>)");
    process.exit(2);
  }
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });

  /* --- 1. /conversations 底部提示的精确文案 --- */
  const c1 = await browser.newContext({ viewport: { width: 1600, height: 950 } });
  const p1 = await c1.newPage();
  await p1.addInitScript((t) => {
    window.localStorage.setItem("b2b_token", t);
  }, TOKEN);
  await p1.goto(BASE + "/conversations", { waitUntil: "domcontentloaded" });
  await sleep(4000);
  const notes = await p1.locator("p, .muted, .hint, small, div").allInnerTexts();
  const hit = notes.filter((t) => t.includes("可回放") || t.includes("未执行"));
  console.log("== /conversations 「未执行 run」提示的精确文案 ==");
  hit.forEach((t) => console.log("   " + JSON.stringify(t.trim())));
  // 列表行的全部文本，验证"没有时间戳/客户信息"这一判断
  const rowTexts = await p1
    .locator("li, tr, [class*=row], [class*=item]")
    .allInnerTexts()
    .catch(() => []);
  console.log("== 会话列表前 3 行的完整可见文本 ==");
  rowTexts.filter((t) => t.trim()).slice(0, 3).forEach((t) => console.log("   " + JSON.stringify(t.trim())));
  await c1.close();

  /* --- 2. /support 的计算样式：placeholder 颜色与字号 --- */
  const c2 = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const p2 = await c2.newPage();
  await p2.goto(BASE + "/support", { waitUntil: "domcontentloaded" });
  await sleep(3500);
  const styles = await p2.evaluate(() => {
    const out = {};
    const composer = document.querySelector(".support-composer input");
    if (composer) {
      const cs = getComputedStyle(composer);
      out.composerColor = cs.color;
      out.composerBg = cs.backgroundColor;
      out.composerFontSize = cs.fontSize;
      out.composerTag = composer.tagName;
      out.composerPlaceholder = getComputedStyle(composer, "::placeholder").color;
    }
    const empty = document.querySelector(".support-empty");
    if (empty) {
      const cs = getComputedStyle(empty);
      out.emptyColor = cs.color;
      out.emptyFontSize = cs.fontSize;
      out.emptyParentBg = getComputedStyle(empty.parentElement).backgroundColor;
    }
    const shell = document.querySelector(".support-shell");
    if (shell) out.shellBg = getComputedStyle(shell).backgroundColor;
    // 是否存在 role=alert / aria-live 区域
    out.hasAriaLive = !!document.querySelector("[aria-live]");
    out.hasRoleAlert = !!document.querySelector("[role=alert]");
    // 表单控件是否有 label
    const ctrls = Array.from(document.querySelectorAll("input, textarea"));
    out.controls = ctrls.map((el) => ({
      tag: el.tagName,
      type: el.getAttribute("type"),
      placeholder: el.getAttribute("placeholder"),
      ariaLabel: el.getAttribute("aria-label"),
      hasLabel: !!document.querySelector(`label[for="${el.id}"]`) || !!el.closest("label"),
    }));
    // 页面有几个 h1
    out.h1 = Array.from(document.querySelectorAll("h1")).map((e) => e.innerText);
    return out;
  });
  console.log("\n== /support 计算样式与无障碍事实 ==");
  console.log(JSON.stringify(styles, null, 2));

  await c2.close();
  await browser.close();
  process.exit(0);
})().catch((e) => {
  console.error("探针失败：", e);
  process.exit(1);
});
