// Acceptance walk: drives the real admin UI in a real browser, page by page,
// clicking every interactive element and recording what actually happened.
import { chromium } from "playwright-core";
import fs from "node:fs";
import path from "node:path";

const BASE = "http://localhost:5174";
const OUT = "D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan/.workbuddy-ai/acceptance/shots";
// Environment, not a literal: this is a real credential for the local
// admin-demo tenant. See verify.mjs for why it must not be committed.
const TOKEN = process.env.API_TOKEN;
if (!TOKEN) {
  console.error("API_TOKEN is unset. Run: python scripts/seed_admin_demo.py");
  process.exit(2);
}
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";

const log = [];
const ev = (kind, where, text) => log.push({ kind, where, text });

const ROUTES = [
  ["/quality", "Quality"],
  ["/gaps", "Gaps"],
  ["/prompts", "Prompts"],
  ["/flags", "Flags"],
  ["/cases", "Cases"],
  ["/members", "Members"],
  ["/usage", "Usage"],
  ["/branding", "Branding"],
];

function attach(page, where) {
  page.on("console", (m) => {
    if (m.type() === "error" || m.type() === "warning") {
      ev("console:" + m.type(), where, m.text().slice(0, 300));
    }
  });
  page.on("pageerror", (e) => ev("pageerror", where, e.message.slice(0, 300)));
  page.on("requestfailed", (r) =>
    ev("requestfailed", where, `${r.method()} ${r.url()} ${r.failure()?.errorText ?? ""}`),
  );
  page.on("response", (r) => {
    if (r.status() >= 400) ev("http" + r.status(), where, `${r.request().method()} ${r.url()}`);
  });
}

const snap = (page) =>
  page.evaluate(() => ({
    text: document.body.innerText,
    prompts: document.querySelectorAll(".prompt").length,
    banners: [...document.querySelectorAll(".banner")].map((b) => b.innerText),
    empties: [...document.querySelectorAll(".empty-state")].map((b) => b.innerText),
    spinners: document.querySelectorAll(".spinner").length,
    rows: document.querySelectorAll("table tbody tr").length,
  }));

async function run() {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 940 } });
  const page = await ctx.newPage();
  attach(page, "boot");

  // ---- 1. First visit with no token: the TokenDialog must appear.
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  ev("step", "boot", "url=" + page.url());
  const dlgVisible = await page.locator(".prompt").count();
  ev("step", "boot", "tokenDialogVisible=" + dlgVisible);

  // Empty submit -> must be refused client-side.
  await page.locator(".prompt .btn-primary").click();
  await page.waitForTimeout(200);
  const emptyErr = await page.locator(".prompt-error").count();
  ev("check", "boot", `empty token rejected = ${emptyErr > 0}`);

  // Bad token -> must be refused and NOT persisted.
  await page.locator(".prompt input[type=password]").fill("pt_admin-demo_00000000-0000-0000-0000-000000000000");
  await page.locator(".prompt .btn-primary").click();
  await page.waitForTimeout(2500);
  const badErr = await page.locator(".prompt-error").count();
  const storedBad = await page.evaluate(() => localStorage.getItem("b2b_token"));
  ev("check", "boot", `bad token rejected = ${badErr > 0}; token persisted after failure = ${JSON.stringify(storedBad)}`);

  // Real token.
  await page.locator(".prompt input[type=password]").fill(TOKEN);
  await page.locator(".prompt .btn-primary").click();
  await page.waitForNetworkIdle({ timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(1500);
  ev("step", "boot", "after connect url=" + page.url());

  // ---- 2. Screenshots + element inventory per page.
  for (const [route, name] of ROUTES) {
    await page.goto(BASE + route, { waitUntil: "networkidle" }).catch(() => {});
    await page.waitForTimeout(1200);
    const s = await snap(page);
    await page.screenshot({ path: path.join(OUT, `desk-${name}.png`), fullPage: true });
    ev("page", name, `rows=${s.rows} spinners=${s.spinners} empties=${JSON.stringify(s.empties)} banners=${JSON.stringify(s.banners)}`);

    // Inventory of interactive elements.
    const inv = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll("button, a[href], select, input, textarea").forEach((el) => {
        const label = (el.innerText || el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.tagName).trim().slice(0, 40);
        out.push({
          tag: el.tagName.toLowerCase(),
          label,
          href: el.getAttribute("href") || "",
          disabled: el.disabled === true,
        });
      });
      return out;
    });
    ev("inventory", name, JSON.stringify(inv));
  }

  // ---- 3. Interaction sweep: click every enabled button, record effect.
  const fake = [];
  for (const [route, name] of ROUTES) {
    await page.goto(BASE + route, { waitUntil: "networkidle" }).catch(() => {});
    await page.waitForTimeout(1200);

    const n = await page.locator("button:not([disabled])").count();
    for (let i = 0; i < n; i++) {
      // Re-locate each time: the DOM may have changed.
      const btns = page.locator("button:not([disabled])");
      const count = await btns.count();
      if (i >= count) break;
      const b = btns.nth(i);
      let label = "";
      try {
        label = (await b.innerText()).trim().slice(0, 40) || (await b.getAttribute("aria-label")) || "?";
      } catch {
        continue;
      }
      // Skip the global "Token" button and nav links (covered separately).
      if (label === "Token") continue;

      const before = await snap(page);
      const reqs = [];
      const onReq = (r) => reqs.push(`${r.method()} ${r.url()}`);
      page.on("request", onReq);
      let clicked = false;
      try {
        await b.click({ timeout: 2500 });
        clicked = true;
      } catch (e) {
        ev("click-error", name, `${label}: ${String(e).slice(0, 120)}`);
      }
      await page.waitForTimeout(1400);
      page.off("request", onReq);
      const after = await snap(page);

      const net = reqs.filter((u) => u.includes("/v1/"));
      const domChanged = before.text !== after.text;
      const promptOpened = after.prompts > before.prompts;
      const navAway = !page.url().includes(route);

      if (clicked && !domChanged && net.length === 0) {
        fake.push({ page: name, label, note: "no DOM change, no API call" });
      }
      ev("click", name, `"${label}" -> net=${net.length} domChanged=${domChanged} prompt=${promptOpened} navAway=${navAway}`);

      // Close any inline prompt / restore route.
      if (promptOpened) {
        const cancel = page.locator(".prompt .btn:not(.btn-primary)").first();
        if (await cancel.count()) await cancel.click().catch(() => {});
        await page.waitForTimeout(400);
      }
      if (navAway) {
        await page.goto(BASE + route, { waitUntil: "networkidle" }).catch(() => {});
        await page.waitForTimeout(900);
      }
    }
  }

  // ---- 4. Mobile pass.
  const mctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 2,
  });
  const mp = await mctx.newPage();
  await mp.addInitScript((t) => localStorage.setItem("b2b_token", t), TOKEN);
  attach(mp, "mobile");
  for (const [route, name] of ROUTES) {
    await mp.goto(BASE + route, { waitUntil: "networkidle" }).catch(() => {});
    await mp.waitForTimeout(1200);
    await mp.screenshot({ path: path.join(OUT, `mob-${name}.png`), fullPage: true });
    const m = await mp.evaluate(() => ({
      docW: document.documentElement.scrollWidth,
      winW: window.innerWidth,
      overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      navH: document.querySelector(".sidebar")?.getBoundingClientRect().height ?? 0,
    }));
    ev("mobile", name, JSON.stringify(m));
  }

  // ---- 5. Unknown route.
  await page.goto(BASE + "/this-page-does-not-exist", { waitUntil: "networkidle" }).catch(() => {});
  await page.waitForTimeout(800);
  ev("check", "404", "unknown route lands on url=" + page.url());

  // ---- 6. Keyboard: can the inline prompt be dismissed with Escape?
  await page.goto(BASE + "/flags", { waitUntil: "networkidle" }).catch(() => {});
  await page.waitForTimeout(1200);
  const setRollout = page.locator("button", { hasText: "Set rollout" }).first();
  if (await setRollout.count()) {
    await setRollout.click();
    await page.waitForTimeout(600);
    await page.keyboard.press("Escape");
    await page.waitForTimeout(500);
    const still = await page.locator(".prompt").count();
    ev("check", "a11y", `Escape closes inline prompt = ${still === 0}`);
    const focus = await page.evaluate(() => document.activeElement?.tagName + ":" + (document.activeElement?.innerText || "").slice(0, 30));
    ev("check", "a11y", `focus after prompt open = ${focus}`);
  }

  await browser.close();

  fs.writeFileSync(
    path.join(OUT, "..", "walk-log.json"),
    JSON.stringify({ log, fake }, null, 2),
    "utf8",
  );
  console.log("FAKE BUTTONS:", JSON.stringify(fake, null, 2));
  const counts = {};
  for (const e of log) counts[e.kind] = (counts[e.kind] || 0) + 1;
  console.log("EVENT COUNTS:", JSON.stringify(counts, null, 2));
}

run().catch((e) => {
  console.error("HARNESS ERROR", e);
  process.exit(1);
});
