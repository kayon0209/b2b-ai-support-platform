/**
 * 真实用户旅程走查（Playwright + 本机已装 Chromium）。
 *
 * 目的：从「系统入口」开始，按真实用户的操作顺序点击，记录每一步的
 * 可观察结果（URL、可见文本、控制台错误）并截图。
 *
 * 与 admin_render_check.cjs 的区别：那个脚本枚举页面并断言「渲染成功」；
 * 这个脚本扮演人 —— 打开入口、找线索、输入、等待、受挫。
 *
 * 硬编码而非走环境变量：本机 harness 下 `VAR=x node script.js` 会静默死亡。
 * require 用绝对路径：Node 从脚本所在目录解析，工作区的 node_modules 不在这一侧。
 */

const fs = require("fs");
const path = require("path");

const { chromium } = require("C:/Users/Rose/.workbuddy/binaries/node/workspace/node_modules/playwright-core");

const CHROME =
  "D:/migrated/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const BASE = "http://127.0.0.1:5173";
const OUT = "D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan/outputs/journey-audit";

fs.mkdirSync(OUT, { recursive: true });

const log = [];
function note(line) {
  log.push(line);
  console.log(line);
}

const errors = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function shot(page, name) {
  const file = path.join(OUT, `${name}.png`);
  await page.screenshot({ path: file, fullPage: true });
  note(`  [截图] ${path.basename(file)}`);
}

function wire(page, label) {
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(`${label} | console.error: ${msg.text()}`);
  });
  page.on("pageerror", (err) => errors.push(`${label} | pageerror: ${err.message}`));
  page.on("requestfailed", (req) => {
    errors.push(`${label} | requestfailed: ${req.method()} ${req.url()} :: ${req.failure()?.errorText}`);
  });
}

async function bodyText(page) {
  return (await page.locator("body").innerText()).replace(/\n{2,}/g, "\n").trim();
}

/**
 * 等到「回答真正落地」。
 *
 * 不能等「正文有变化」—— 乐观气泡会立刻让正文变化，于是每次都只拍到
 * 「发送中」。也不能等「等待指示器消失」—— `waiting` 是在时间线拉取
 * 回来之后才置 true 的，点击后瞬间它是 false，等于没等。
 *
 * 可靠的判据是**事实**：客服气泡数超过基线，或出现错误块。
 */
async function sendAndWait(page, composer, sendBtn, text, timeoutMs, label) {
  const before = await page.locator(".support-bubble.is-agent").count();
  const t0 = Date.now();
  await composer.fill(text);
  await sendBtn.click();
  const deadline = t0 + timeoutMs;
  while (Date.now() < deadline) {
    const agents = await page.locator(".support-bubble.is-agent").count();
    const prob = await page.locator(".support-problem").count();
    if (agents > before || prob > 0) {
      await sleep(2500);
      note(
        `  [${label}] 回答落地于 ${Math.round((Date.now() - t0) / 1000)}s` +
          `（客服气泡 ${before}→${agents}，错误块 ${prob}）`
      );
      return await bodyText(page);
    }
    await sleep(1500);
  }
  note(`  [${label}] ${timeoutMs / 1000}s 内未出现回答（基线气泡 ${before}）`);
  return await bodyText(page);
}

/** 等到核实表单消失（成功）或出现错误提示（失败）。 */
async function waitForVerify(page, timeoutMs, label) {
  const t0 = Date.now();
  const deadline = t0 + timeoutMs;
  while (Date.now() < deadline) {
    const form = await page.locator(".support-verify").count();
    const prob = await page.locator(".support-problem").count();
    if (form === 0 || prob > 0) {
      await sleep(1200);
      note(`  [${label}] 结果出现在 ${Math.round((Date.now() - t0) / 1000)}s（表单剩余=${form}, 错误块=${prob}）`);
      return await bodyText(page);
    }
    await sleep(1200);
  }
  return await bodyText(page);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });

  /* ================================================================== */
  /* 旅程 A：客户侧（第一次访问 → 成功提问）                              */
  /* ================================================================== */
  note("=".repeat(72));
  note("旅程 A｜客户侧：从系统入口到成功发起提问");
  note("=".repeat(72));

  const ctxA = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const a = await ctxA.newPage();
  wire(a, "A");

  note("\n[A1] 打开根入口 http://127.0.0.1:5173/ （用户会先输域名）");
  await a.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await sleep(3000);
  note(`  最终 URL = ${a.url()}`);
  note(`  页面标题 = ${await a.title()}`);
  note(`  可见文本（前 400 字）：\n---\n${(await bodyText(a)).slice(0, 400)}\n---`);
  await shot(a, "A1-入口首页");

  note("\n[A2] 首页上有没有指向「客户对话」的入口？");
  const links = await a.locator("a[href]").evaluateAll((els) =>
    els.map((e) => `${e.getAttribute("href")} :: ${e.innerText.trim().slice(0, 40)}`)
  );
  note(`  发现 ${links.length} 个链接，其中指向 /support 的：`);
  links.filter((l) => l.includes("/support")).forEach((l) => note(`    - ${l}`));
  if (!links.some((l) => l.includes("/support"))) note("    （无）");

  note("\n[A3] 客户直接打开 /support");
  await a.goto(BASE + "/support", { waitUntil: "domcontentloaded" });
  await sleep(3500);
  note(`  页面可见文本：\n---\n${await bodyText(a)}\n---`);
  await shot(a, "A3-客户会话页");

  note("\n[A4] 手机视口（390x844）");
  const ctxMobile = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
  });
  const m = await ctxMobile.newPage();
  wire(m, "A-mobile");
  await m.goto(BASE + "/support", { waitUntil: "domcontentloaded" });
  await sleep(3500);
  await shot(m, "A4-手机视口");

  /* ---------------- A5：第一次提问（短中文问句） ---------------- */
  note("\n[A5] 用最自然的中文短句提问：『交期是多久？』（6 字）");
  const composer = a.locator(".support-input").first();
  const sendBtn = a.locator(".support-send").first();
  const after5 = await sendAndWait(a, composer, sendBtn, "交期是多久？", 75_000, "A5");
  note(`  最终正文：\n---\n${after5}\n---`);
  note(`  >>> 回答是否含中文：${/[\u4e00-\u9fff]/.test(after5) ? "是" : "否"}`);
  await shot(a, "A5-回答后");

  /* ---------------- A6：身份核实（错误凭证） ---------------- */
  note("\n[A6] 用错误凭证核实身份（模拟客户记错手机尾号）");
  const vInputs = a.locator(".support-verify input");
  const vBtn = a.locator(".support-verify button[type=submit]");
  note(`  核实表单输入框数 = ${await vInputs.count()}`);
  await vInputs.nth(0).fill("SO-9001");
  await vInputs.nth(1).fill("1234");
  await vBtn.click();
  const a6 = await waitForVerify(a, 30_000, "A6");
  note(`  错误凭证结果：\n---\n${a6}\n---`);
  await shot(a, "A6-核实失败");

  /* ---------------- A7：身份核实（正确凭证） ---------------- */
  note("\n[A7] 用正确凭证核实：SO-9001 + 8888");
  await vInputs.nth(0).fill("SO-9001");
  await vInputs.nth(1).fill("8888");
  await vBtn.click();
  const a7 = await waitForVerify(a, 30_000, "A7");
  note(`  正确凭证结果：\n---\n${a7}\n---`);
  await shot(a, "A7-核实成功");

  /* ---------------- A8：订单查询（头条能力） ---------------- */
  note("\n[A8] 已核实身份后问：『我的订单 SO-9001 到哪了？』");
  const a8 = await sendAndWait(a, composer, sendBtn, "我的订单 SO-9001 到哪了？", 130_000, "A8");
  note(`  结果：\n---\n${a8}\n---`);
  await shot(a, "A8-订单查询");
  note(`  >>> 数据卡元素数 = ${await a.locator(".tool-card").count()}`);
  note(`  >>> 客服气泡数 = ${(await a.locator(".support-bubble.is-agent").allInnerTexts()).length}`);

  /* ---------------- A8b：第二次提问（知识问答，词面重合） ---------------- */
  note("\n[A8b] 再问一个与知识库措辞接近的中文问题：『常规交期和加急分别是几个工作日？』");
  const a8b = await sendAndWait(
    a,
    composer,
    sendBtn,
    "常规交期和加急分别是几个工作日？",
    90_000,
    "A8b"
  );
  note(`  结果：\n---\n${a8b}\n---`);
  await shot(a, "A8b-知识问答");

  /* ---------------- A8c：转人工之后界面是否还在说话 ---------------- */
  note("\n[A8c] 点「转人工」，然后照常提问，检查界面是否有可见反馈");
  await a.locator(".support-human").first().click();
  await sleep(6000);
  const handoffText = await a
    .locator(".support-handoff")
    .innerText()
    .catch(() => "(没有出现状态条)");
  const ownerNow = await bodyText(a);
  note(`  >>> 「已转人工」状态条：${handoffText}`);
  note(`  >>> 输入框是否仍可输入：${(await composer.isDisabled()) ? "否（被禁用）" : "是"}`);
  await shot(a, "A8c-转人工");

  // The question that used to vanish: after a handoff the AI may not answer,
  // and before the fix nothing said so.
  const a8c = await sendAndWait(
    a,
    composer,
    sendBtn,
    "常规交期和加急分别是几个工作日？",
    45_000,
    "A8c",
  );
  const systemTurns = await a.locator(".support-system").allInnerTexts();
  note(`  >>> 平台提示条数 = ${systemTurns.length}`);
  systemTurns.forEach((t) => note(`      · ${t}`));
  note(`  >>> 客服气泡数 = ${(await a.locator(".support-bubble.is-agent").allInnerTexts()).length}`);
  note(`  最终正文：\n---\n${a8c}\n---`);
  await shot(a, "A8c2-转人工后提问");

  /* ---------------- A9：刷新后是否续上会话 ---------------- */
  note("\n[A9] 刷新页面，检查会话与品牌是否恢复");
  const h1Before = (await a.locator(".support-title").innerText()).trim();
  await a.reload({ waitUntil: "domcontentloaded" });
  await sleep(5000);
  const a9 = await bodyText(a);
  const h1 = (await a.locator(".support-title").innerText()).trim();
  note(`  刷新前 H1 = ${JSON.stringify(h1Before)}`);
  note(`  刷新后 H1 = ${JSON.stringify(h1)}`);
  note(`  >>> 品牌名是否保持：${h1 === h1Before ? "一致" : "**不一致**"}`);
  const stillCard = (await a.locator(".tool-card").count()) > 0;
  const stillHandoff = (await a.locator(".support-handoff").count()) > 0;
  const stillVerified = (await a.locator(".support-verified").count()) > 0;
  note(`  >>> 刷新后：数据卡=${stillCard} 转人工状态条=${stillHandoff} 已核实=${stillVerified}`);
  note(`  刷新后正文：\n---\n${a9}\n---`);
  await shot(a, "A9-刷新后");

  /* ---------------- A10：找不到的地址 ---------------- */
  note("\n[A10] 客户输错地址 http://127.0.0.1:5173/suport （拼写错误）");
  await a.goto(BASE + "/suport", { waitUntil: "domcontentloaded" });
  await sleep(2500);
  note(`  最终 URL = ${a.url()}`);
  note(`  页面可见文本：\n---\n${await bodyText(a)}\n---`);
  await shot(a, "A10-输错地址");

  await ctxMobile.close();
  await ctxA.close();

  /* ================================================================== */
  /* 旅程 B：运营侧（后台入口 → 工作台处理会话）                          */
  /* ================================================================== */
  note("\n" + "=".repeat(72));
  note("旅程 B｜运营侧：从后台入口到工作台处理");
  note("=".repeat(72));

  const ctxB = await browser.newContext({ viewport: { width: 1600, height: 950 } });
  const b = await ctxB.newPage();
  wire(b, "B");

  note("\n[B1] 打开根入口（运营人员视角）");
  await b.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await sleep(3500);
  note(`  最终 URL = ${b.url()}`);
  note(`  可见文本（前 600 字）：\n---\n${(await bodyText(b)).slice(0, 600)}\n---`);
  await shot(b, "B1-后台首页");

  const navItems = await b.locator("aside a").evaluateAll((els) =>
    els.map((e) => `${e.getAttribute("href")} :: ${e.innerText.trim()}`)
  );
  note(`  侧边导航（${navItems.length} 项）：`);
  navItems.forEach((x) => note(`    - ${x.replace(/\n/g, " ")}`));

  note("\n[B2] 逐个打开侧边导航页面，收集控制台/网络错误");
  const routes = ["/admin/quality", "/admin/gaps", "/admin/knowledge", "/admin/conversations",
                  "/admin/prompts", "/admin/flags", "/admin/channels", "/admin/experiments",
                  "/admin/cases", "/admin/workbench", "/admin/approvals", "/admin/members",
                  "/admin/usage", "/admin/branding"];
  for (const r of routes) {
    const before = errors.length;
    await b.goto(BASE + r, { waitUntil: "domcontentloaded" });
    await sleep(2500);
    const t = await bodyText(b);
    const err = errors.length - before;
    const first = t.split("\n").filter(Boolean).slice(0, 3).join(" | ");
    note(`  ${r.padEnd(15)} 错误新增=${err}  首屏: ${first.slice(0, 120)}`);
    await shot(b, `B2-${r.replace(/\//g, "")}`);
  }

  note("\n[B3] 工作台：能否选中一个会话并回复");
  await b.goto(BASE + "/admin/workbench", { waitUntil: "domcontentloaded" });
  await sleep(4000);
  const wb = await bodyText(b);
  note(`  工作台文本（前 1200 字）：\n---\n${wb.slice(0, 1200)}\n---`);
  await shot(b, "B3-工作台");
  const buttons = await b.locator("button").evaluateAll((els) =>
    els.map((e) => e.innerText.trim()).filter(Boolean)
  );
  note(`  页面按钮（${buttons.length}）：${buttons.slice(0, 40).join(" / ")}`);

  note("\n[B4] 会话回放页");
  await b.goto(BASE + "/conversations", { waitUntil: "domcontentloaded" });
  await sleep(3500);
  note(`  会话页文本（前 900 字）：\n---\n${(await bodyText(b)).slice(0, 900)}\n---`);
  await shot(b, "B4-会话");

  note("\n[B5] 工单与 SLA");
  await b.goto(BASE + "/admin/cases", { waitUntil: "domcontentloaded" });
  await sleep(3000);
  note(`  工单页文本（前 600 字）：\n---\n${(await bodyText(b)).slice(0, 600)}\n---`);
  await shot(b, "B5-工单");

  await ctxB.close();
  await browser.close();

  note("\n" + "=".repeat(72));
  note(`汇总：控制台/页面/网络错误共 ${errors.length} 条`);
  note("=".repeat(72));
  errors.forEach((e) => note("  " + e));

  fs.writeFileSync(path.join(OUT, "audit.log"), log.join("\n"), "utf8");
  console.log(`\n日志已写入 ${path.join(OUT, "audit.log")}`);
  process.exit(0);
})().catch(async (err) => {
  console.error("走查脚本异常终止：", err);
  fs.writeFileSync(path.join(OUT, "audit.log"), log.join("\n") + "\n\nFATAL: " + err.stack, "utf8");
  process.exit(1);
});
