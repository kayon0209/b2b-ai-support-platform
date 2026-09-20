import { chromium } from "playwright-core";
import fs from "node:fs";
import path from "node:path";

const B = "http://localhost:5174";
const T = process.env.API_TOKEN;
if (!T) { console.error("API_TOKEN is unset. Run: python scripts/seed_admin_demo.py"); process.exit(2); }
const C = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const OUT = "D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan/.workbuddy-ai/acceptance/shots";
const R = [];
const say = (k, v) => { R.push(`${k}: ${v}`); console.log(`${k}: ${v}`); };
const ROUTES = [["quality","/quality"],["gaps","/gaps"],["prompts","/prompts"],["flags","/flags"],["cases","/cases"],["members","/members"],["usage","/usage"],["branding","/branding"]];

const texts = (p) => p.evaluate(() => {
  const out = []; const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) { const s = (w.currentNode.textContent || "").trim(); if (s) out.push(s); }
  return out;
});

async function run() {
  const br = await chromium.launch({ executablePath: C, headless: true });

  // ---- EN pass ----
  const en = {};
  const ce = await br.newContext({ viewport: { width: 1440, height: 940 } });
  ce.setDefaultTimeout(8000);
  await ce.addInitScript((t) => { localStorage.setItem("b2b_token", t); localStorage.setItem("b2b_lang", "en"); }, T);
  const pe = await ce.newPage();
  const bad = [];
  pe.on("response", (r) => { if (r.status() >= 400) bad.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`); });
  for (const [n, r] of ROUTES) { await pe.goto(B + r, { waitUntil: "domcontentloaded" }); await pe.waitForTimeout(1600); en[n] = await texts(pe); }
  say("http_errors_en_pass", JSON.stringify([...new Set(bad)]));

  // ---- ZH pass + structural checks ----
  const cz = await br.newContext({ viewport: { width: 1440, height: 940 } });
  cz.setDefaultTimeout(8000);
  await cz.addInitScript((t) => { localStorage.setItem("b2b_token", t); localStorage.setItem("b2b_lang", "zh"); }, T);
  const pz = await cz.newPage();
  const zh = {}, struct = {};
  for (const [n, r] of ROUTES) {
    await pz.goto(B + r, { waitUntil: "domcontentloaded" }); await pz.waitForTimeout(1600);
    zh[n] = await texts(pz);
    struct[n] = await pz.evaluate(() => ({
      title: document.title,
      h1: document.querySelectorAll("h1").length,
      imgsNoAlt: [...document.querySelectorAll("img")].filter((i) => !i.alt).length,
      btnNoLabel: [...document.querySelectorAll("button")].filter((b) => !b.innerText.trim() && !b.getAttribute("aria-label")).length,
      thNoScope: [...document.querySelectorAll("th")].filter((t) => !t.getAttribute("scope")).length,
      navAria: document.querySelector("nav")?.getAttribute("aria-label") ?? null,
      skipLink: !!document.querySelector("a[href^='#main'], a.skip-link"),
      pagination: document.body.innerText.match(/下一页|上一页|Next|Previous|Page \d+/i) ? true : false,
      totalNote: document.body.innerText.match(/Showing \d+ of \d+|共 \d+|\d+ 条/) ? true : false,
      liveRegions: document.querySelectorAll("[aria-live]").length,
    }));
    await pz.screenshot({ path: path.join(OUT, `f-zh-${n}.png`), fullPage: true });
  }
  say("structure", JSON.stringify(struct, null, 1));

  const IGNORE = /^[\d\s.,:%\-—·→…()（）/\\|#"']+$/; const KEEP = /[A-Za-z]{2,}/;
  for (const [n] of ROUTES) {
    const sEn = new Set(en[n].filter((s) => KEEP.test(s) && !IGNORE.test(s)));
    const dup = [...new Set(zh[n].filter((s) => sEn.has(s) && KEEP.test(s) && !IGNORE.test(s)))];
    say(`untranslated[${n}]`, JSON.stringify(dup.slice(0, 20)));
  }

  // ---- cases/null ----
  bad.length = 0;
  await pe.goto(B + "/cases", { waitUntil: "domcontentloaded" }); await pe.waitForTimeout(1800);
  const n1 = bad.filter((b) => b.includes("/v1/cases/null")).length;
  const rows = pe.locator(".case-row"); const rc = await rows.count();
  if (rc) { await rows.first().click(); await pe.waitForTimeout(1600);
    const cl = pe.locator(".card .btn-ghost").first(); if (await cl.count()) { await cl.click(); await pe.waitForTimeout(1400); } }
  say("cases_null_400_count", `onLoad=${n1} afterSelectAndClose=${bad.filter((b) => b.includes("/v1/cases/null")).length} rows=${rc}`);

  // ---- prompt: escape + focus ----
  await pz.goto(B + "/flags", { waitUntil: "domcontentloaded" }); await pz.waitForTimeout(1600);
  const b2 = pz.locator("table tbody tr button").nth(1);
  if (await b2.count()) {
    await b2.click(); await pz.waitForTimeout(700);
    const opened = await pz.locator(".prompt").count();
    const act = await pz.evaluate(() => document.activeElement?.tagName + "|" + (document.activeElement?.className || ""));
    const roleAttr = await pz.locator(".prompt").first().getAttribute("role");
    await pz.keyboard.press("Escape"); await pz.waitForTimeout(600);
    say("prompt_a11y", `opened=${opened} activeEl=${act} role=${roleAttr} escapeCloses=${(await pz.locator(".prompt").count()) === 0}`);
    const cancel = pz.locator(".prompt .btn:not(.btn-primary)").first(); if (await cancel.count()) await cancel.click();
  } else say("prompt_a11y", "no row action button");

  // ---- form validation ----
  await pe.goto(B + "/usage", { waitUntil: "domcontentloaded" }); await pe.waitForTimeout(1800);
  const chq = pe.locator("button", { hasText: /Change quota|修改配额|配额/ }).first();
  if (await chq.count()) {
    await chq.click(); await pe.waitForTimeout(500);
    const inp = pe.locator("input[aria-label]").first();
    await inp.fill("12.5"); await pe.waitForTimeout(200);
    const save = pe.locator("button", { hasText: /^Save|保存$/ }).first();
    if (await save.count()) { await save.click(); await pe.waitForTimeout(1200); }
    say("quota_invalid_input", JSON.stringify(await pe.locator(".banner").allInnerTexts()));
    const cancel = pe.locator("button", { hasText: /Cancel|取消/ }).first();
    if (await cancel.count()) await cancel.click();
  } else say("quota_invalid_input", "change-quota button not found");

  // ---- flag key with special characters ----
  await pe.goto(B + "/flags", { waitUntil: "domcontentloaded" }); await pe.waitForTimeout(1600);
  const keyIn = pe.locator("input[placeholder='flag_key']").first();
  if (await keyIn.count()) {
    await keyIn.fill("bad key/#?1");
    const def = pe.locator("button", { hasText: /Define|定义/ }).first();
    await def.click(); await pe.waitForTimeout(2000);
    say("flag_key_special", JSON.stringify(await pe.locator(".banner, .prompt-error").allInnerTexts()));
    say("flag_key_special_url", JSON.stringify(bad.filter((b) => b.includes("/flags/")).slice(-3)));
  } else say("flag_key_special", "input not found");

  // ---- mobile ----
  const cm = await br.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  cm.setDefaultTimeout(8000);
  await cm.addInitScript((t) => { localStorage.setItem("b2b_token", t); localStorage.setItem("b2b_lang", "zh"); }, T);
  const pm = await cm.newPage();
  for (const [n, r] of ROUTES) {
    await pm.goto(B + r, { waitUntil: "domcontentloaded" }); await pm.waitForTimeout(1500);
    await pm.screenshot({ path: path.join(OUT, `f-mob-${n}.png`), fullPage: true });
    say(`mobile[${n}]`, JSON.stringify(await pm.evaluate(() => ({
      vp: window.innerWidth,
      sidebarH: Math.round(document.querySelector(".sidebar")?.getBoundingClientRect().height ?? 0),
      navBottomVh: Math.round((document.querySelector(".sidebar")?.getBoundingClientRect().bottom ?? 0) / window.innerHeight * 100),
    }))));
  }

  // ---- dark ----
  await pz.goto(B + "/quality", { waitUntil: "domcontentloaded" }); await pz.waitForTimeout(1400);
  const tg = pz.locator(".shell-toggle").first(); if (await tg.count()) { await tg.click(); await pz.waitForTimeout(900); }
  say("dark_theme", await pz.evaluate(() => document.documentElement.dataset.theme));
  await pz.screenshot({ path: path.join(OUT, "f-dark-quality.png"), fullPage: true });

  await br.close();
  fs.writeFileSync(path.join(OUT, "..", "final-report.txt"), R.join("\n"), "utf8");
}
run().catch((e) => { console.error("HARNESS ERROR", e); process.exit(1); });
