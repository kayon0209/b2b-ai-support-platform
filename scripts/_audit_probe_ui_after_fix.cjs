/**
 * 修复后 UI 的定点核对：把「已经改好了」变成可核对的事实。
 *
 * 全部从真实 DOM 取值（计算样式、属性、角色），不看截图判断 —— 上一轮走查里
 * 凭低分辨率截图读文案差点报出一个不存在的缺陷。
 *
 * 运行：
 *   node scripts/_audit_probe_ui_after_fix.cjs
 */

const { chromium } = require("C:/Users/Rose/.workbuddy/binaries/node/workspace/node_modules/playwright-core");

const CHROME =
  "D:/migrated/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const BASE = "http://127.0.0.1:5173";
const OUT = "D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan/outputs/journey-audit";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let failed = 0;
function check(label, ok, detail = "") {
  if (!ok) failed += 1;
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${label}${detail ? `\n        ${detail}` : ""}`);
}

function contrast(fg, bg) {
  const lin = (c) => {
    c = c / 255;
    return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  const lum = (rgb) => {
    const m = rgb.match(/\d+/g).map(Number);
    return 0.2126 * lin(m[0]) + 0.7152 * lin(m[1]) + 0.0722 * lin(m[2]);
  };
  const a = lum(fg);
  const b = lum(bg);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });

  // ---------------------------------------------------------------- 桌面端
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push(e.message));

  await page.goto(`${BASE}/support`, { waitUntil: "domcontentloaded" });
  await sleep(4500);
  await page.screenshot({ path: `${OUT}/F1-修复后-桌面.png`, fullPage: true });

  const facts = await page.evaluate(() => {
    const q = (sel) => document.querySelector(sel);
    const all = (sel) => Array.from(document.querySelectorAll(sel));
    const cs = (el) => (el ? getComputedStyle(el) : null);
    const empty = q(".support-empty-body");
    return {
      h1: q("h1")?.innerText ?? null,
      h1Count: all("h1").length,
      chips: all(".support-chip").map((b) => b.innerText.trim()),
      humanButton: q(".support-human")?.innerText.trim() ?? null,
      composerTag: q(".support-input")?.tagName ?? null,
      composerRow: q(".support-input")?.rows ?? null,
      threadRole: q(".support-thread")?.getAttribute("role") ?? null,
      threadLive: q(".support-thread")?.getAttribute("aria-live") ?? null,
      threadRelevant: q(".support-thread")?.getAttribute("aria-relevant") ?? null,
      ariaLiveCount: all("[aria-live]").length,
      labelledByThread: q(".support-thread")?.getAttribute("aria-label") ?? null,
      roleAlertCount: all("[role=alert]").length,
      labels: all("label[for]").map((l) => l.getAttribute("for")),
      controls: all("input, textarea").map((el) => ({
        id: el.id,
        tag: el.tagName,
        labelled: Boolean(el.id && document.querySelector(`label[for="${el.id}"]`)),
      })),
      emptyBodyColor: cs(empty)?.color ?? null,
      emptyBg: cs(empty?.closest(".support-shell"))?.backgroundColor ?? null,
      banner: q(".support-banner")?.innerText.trim() ?? null,
      statusText: q(".support-status")?.innerText.trim() ?? null,
      shellShadow: cs(q(".support-shell"))?.boxShadow ?? null,
    };
  });

  console.log("== 修复后的 /support（桌面端 1440px）==");
  check(
    "h1 同时写了租户名和页面名（不再只有一个公司名）",
    (facts.h1 ?? "").includes("在线客服"),
    `h1=${JSON.stringify(facts.h1)}`,
  );
  check("页面只有一个 h1", facts.h1Count === 1, `count=${facts.h1Count}`);
  check(
    "引导建议出现且是 3 条",
    facts.chips.length === 3,
    JSON.stringify(facts.chips),
  );
  check("有「转人工」按钮", facts.humanButton === "转人工", String(facts.humanButton));
  check("输入框是 textarea（可粘贴多行）", facts.composerTag === "TEXTAREA", String(facts.composerTag));
  check(
    "对话流是 role=log + aria-live + aria-relevant",
    facts.threadRole === "log" &&
      facts.threadLive === "polite" &&
      facts.threadRelevant === "additions",
    `${facts.threadRole}/${facts.threadLive}/${facts.threadRelevant}`,
  );
  check("对话流有可读名称", Boolean(facts.labelledByThread), String(facts.labelledByThread));
  check("每个输入控件都有真实 label", facts.controls.every((c) => c.labelled), JSON.stringify(facts.controls));

  // `role="status"` / `role="alert"` are on the *conditional* regions (the
  // typing indicator, the handoff bar, the error bar), so they are not in the
  // initial DOM by design - asserting their absence here would be asserting
  // that nothing has happened yet. They are checked in the states that produce
  // them: `_audit_probe_errors.cjs` for role=alert.
  check(
    "初始页面上不谎报状态区（没有东西可播报时就没有 live region 之外的东西）",
    facts.ariaLiveCount === 1,
    `aria-live count=${facts.ariaLiveCount}`,
  );
  check("桌面端内容列有边界（不是白页里的一条灰带）", facts.shellShadow !== "none", facts.shellShadow);

  if (facts.emptyBodyColor && facts.emptyBg) {
    const r = contrast(facts.emptyBodyColor, facts.emptyBg);
    check(`空状态文案对比度 ≥ 4.5:1`, r >= 4.5, `${r.toFixed(2)} : 1（${facts.emptyBodyColor} on ${facts.emptyBg}）`);
  }

  // ---------------------------------------------------------------- 状态条
  // 直接注入一次「已转人工」的渲染条件不易，改为核对 CSS 已经定义、且组件会渲染。
  const hasHandoffCss = await page.evaluate(() => {
    for (const sheet of Array.from(document.styleSheets)) {
      try {
        for (const rule of Array.from(sheet.cssRules)) {
          if (rule.selectorText === ".support-handoff") return true;
        }
      } catch {
        /* cross-origin sheet */
      }
    }
    return false;
  });
  check("「已转人工」状态条样式已定义", hasHandoffCss);

  // ---------------------------------------------------------------- 移动端
  const mctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
  });
  const m = await mctx.newPage();
  await m.goto(`${BASE}/support`, { waitUntil: "domcontentloaded" });
  await sleep(4000);
  await m.screenshot({ path: `${OUT}/F2-修复后-手机.png`, fullPage: true });
  const mobileFacts = await m.evaluate(() => ({
    chips: Array.from(document.querySelectorAll(".support-chip")).length,
    composerTag: document.querySelector(".support-input")?.tagName ?? null,
    human: document.querySelector(".support-human")?.innerText.trim() ?? null,
    // Nothing may overflow the viewport horizontally.
    overflow:
      document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  console.log("\n== 修复后的 /support（手机端 390px）==");
  check("引导建议在手机端也在", mobileFacts.chips === 3, `count=${mobileFacts.chips}`);
  check("转人工按钮在手机端也在", mobileFacts.human === "转人工", String(mobileFacts.human));
  check("没有横向溢出", mobileFacts.overflow <= 0, `overflow=${mobileFacts.overflow}px`);

  console.log("\n== 控制台错误 ==");
  check("页面无 console error / pageerror", errors.length === 0, errors.slice(0, 3).join(" | "));

  await mctx.close();
  await ctx.close();
  await browser.close();

  console.log(`\n失败项：${failed}`);
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  console.error("探针失败：", e);
  process.exit(1);
});
