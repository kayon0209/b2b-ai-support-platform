// Focused re-verification against the CURRENT source, plus an i18n
// completeness sweep: render every page in EN and in ZH, and report any
// string that is identical in both (i.e. not translated).
import { chromium } from "playwright-core";
import fs from "node:fs";
import path from "node:path";

const BASE = "http://localhost:5174";
const OUT = "D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan/.workbuddy-ai/acceptance/shots";
// The bootstrap token is never committed. It is a real credential for the
// local admin-demo tenant (`pt_<slug>_<user-id>`), and the user id used to be
// derived from this repo's own constants - so the string that lived here was
// both a published secret and a computable one. `scripts/seed_admin_demo.py`
// now mints a random id per machine and prints the token to paste.
const TOKEN = process.env.API_TOKEN;
if (!TOKEN) {
  console.error("API_TOKEN is unset. Run: python scripts/seed_admin_demo.py");
  process.exit(2);
}
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";

const ROUTES = [
  ["quality", "/quality"], ["gaps", "/gaps"], ["prompts", "/prompts"], ["flags", "/flags"],
  ["cases", "/cases"], ["members", "/members"], ["usage", "/usage"], ["branding", "/branding"],
];

const R = [];
const say = (k, v) => { R.push(`${k}: ${v}`); console.log(`${k}: ${v}`); };

const text = (p) => p.evaluate(() => {
  const skip = new Set(["SCRIPT", "STYLE"]);
  const out = [];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) {
    const n = w.currentNode;
    if (skip.has(n.parentElement?.tagName ?? "")) continue;
    const s = (n.textContent || "").trim();
    if (s) out.push(s);
  }
  return out;
});

async function run() {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });

  // ---------- A. Desktop, English ----------
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 940 }, locale: "en-US" });
  ctx.setDefaultTimeout(8000);
  const p = await ctx.newPage();
  const errs = [];
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message.slice(0, 160)));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text().slice(0, 160)); });
  const bad = [];
  p.on("response", (r) => { if (r.status() >= 400) bad.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`); });
  await p.addInitScript((t) => localStorage.setItem("b2b_token", t), TOKEN);

  const en = {}, zh = {};
  for (const [name, route] of ROUTES) {
    await p.goto(BASE + route, { waitUntil: "domcontentloaded" });
    await p.waitForTimeout(1600);
    en[name] = await text(p);
    await p.screenshot({ path: path.join(OUT, `v-en-${name}.png`), fullPage: true });
  }
  say("desktop_errors_after_load", JSON.stringify([...new Set(errs)].slice(0, 8)));
  say("http_errors_after_load", JSON.stringify([...new Set(bad)]));

  // Cases: /v1/cases/null probe + Close behaviour
  await p.goto(BASE + "/cases", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(1500);
  const nullBefore = bad.filter((b) => b.includes("/v1/cases/null")).length;
  const rows = p.locator(".case-row");
  const rowCount = await rows.count();
  if (rowCount > 0) {
    await rows.first().click();
    await p.waitForTimeout(1500);
    const closeBtn = p.locator(".card .btn-ghost", { hasText: /Close|关闭/ }).first();
    if (await closeBtn.count()) { await closeBtn.click(); await p.waitForTimeout(1200); }
  }
  const nullAfter = bad.filter((b) => b.includes("/v1/cases/null")).length;
  say("cases_null_requests", `before=${nullBefore} afterClickAndClose=${nullAfter} (rows=${rowCount})`);

  // Escape on inline prompt (language-agnostic: use the row-action button)
  await p.goto(BASE + "/flags", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(1500);
  const rolloutBtns = p.locator("table tbody tr button").nth(1);
  if (await rolloutBtns.count()) {
    await rolloutBtns.click();
    await p.waitForTimeout(700);
    const open = await p.locator(".prompt").count();
    const focused = await p.evaluate(() => {
      const a = document.activeElement;
      return a ? `${a.tagName}.${a.className}` : "none";
    });
    await p.keyboard.press("Escape");
    await p.waitForTimeout(600);
    const still = await p.locator(".prompt").count();
    say("prompt_escape", `opened=${open} focusWentTo=${focused} escapeCloses=${still === 0}`);
    const cancel = p.locator(".prompt .btn:not(.btn-primary)").first();
    if (await cancel.count()) await cancel.click();
  } else {
    say("prompt_escape", "no row action button found");
  }

  // 404 route
  await p.goto(BASE + "/definitely-not-a-page", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(900);
  say("unknown_route_lands_on", p.url());

  // ---------- B. Chinese ----------
  const zctx = await browser.newContext({ viewport: { width: 1440, height: 940 }, locale: "zh-CN" });
  zctx.setDefaultTimeout(8000);
  const zp = await zctx.newPage();
  await zp.addInitScript((t) => localStorage.setItem("b2b_token", t), TOKEN);
  await zp.goto(BASE + "/quality", { waitUntil: "domcontentloaded" });
  await zp.waitForTimeout(1200);
  // Force zh via the shell toggle if the default came up English.
  for (const [name, route] of ROUTES) {
    await zp.goto(BASE + route, { waitUntil: "domcontentloaded" });
    await zp.waitForTimeout(1500);
    let t = await text(zp);
    const looksEn = t.join(" ").match(/[A-Za-z]/g)?.length ?? 0;
    const looksZh = t.join(" ").match(/[\u4e00-\u9fff]/g)?.length ?? 0;
    if (looksEn > looksZh) {
      const tg = zp.locator(".shell-toggle").nth(1);
      if (await tg.count()) { await tg.click(); await zp.waitForTimeout(1200); t = await text(zp); }
    }
    zh[name] = t;
    await zp.screenshot({ path: path.join(OUT, `v-zh-${name}.png`), fullPage: true });
  }

  // Untranslated = identical string present in both EN and ZH renders.
  const IGNORE = /^[\d\s.,:%\-—·→…()（）/\\|#"']+$/;
  const KEEP = /[A-Za-z]{2,}/;
  for (const [name] of ROUTES) {
    const setEn = new Set(en[name].filter((s) => KEEP.test(s) && !IGNORE.test(s)));
    const dup = zh[name].filter((s) => setEn.has(s) && KEEP.test(s) && !IGNORE.test(s));
    say(`untranslated[${name}]`, JSON.stringify([...new Set(dup)].slice(0, 25)));
  }

  // ---------- C. Mobile ----------
  const mctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  mctx.setDefaultTimeout(8000);
  const mp = await mctx.newPage();
  await mp.addInitScript((t) => localStorage.setItem("b2b_token", t), TOKEN);
  for (const [name, route] of ROUTES) {
    await mp.goto(BASE + route, { waitUntil: "domcontentloaded" });
    await mp.waitForTimeout(1500);
    await mp.screenshot({ path: path.join(OUT, `v-mob-${name}.png`), fullPage: true });
    const m = await mp.evaluate(() => {
      const sb = document.querySelector(".sidebar")?.getBoundingClientRect();
      const tbl = document.querySelector("table");
      return {
        layoutViewport: window.innerWidth,
        sidebarH: sb ? Math.round(sb.height) : 0,
        tableW: tbl ? Math.round(tbl.getBoundingClientRect().width) : null,
        tableOverflows: tbl ? tbl.getBoundingClientRect().width > window.innerWidth : null,
        hasHScroll: document.documentElement.scrollWidth > window.innerWidth + 1,
      };
    });
    say(`mobile[${name}]`, JSON.stringify(m));
  }

  // ---------- D. Dark theme ----------
  await p.goto(BASE + "/quality", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(1200);
  const tog = p.locator(".shell-toggle").first();
  if (await tog.count()) { await tog.click(); await p.waitForTimeout(900); }
  say("dark_theme_attr", await p.evaluate(() => document.documentElement.dataset.theme));
  await p.screenshot({ path: path.join(OUT, "v-dark-quality.png"), fullPage: true });

  await browser.close();
  fs.writeFileSync(path.join(OUT, "..", "verify-report.txt"), R.join("\n"), "utf8");
}

run().catch((e) => { console.error("HARNESS ERROR", e); process.exit(1); });
