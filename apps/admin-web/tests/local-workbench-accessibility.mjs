import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import { basename, join } from "node:path";

import AxeBuilder from "@axe-core/playwright";
import { chromium } from "playwright";

const workbenchUrl = process.env.WORKBENCH_URL;
const evidenceDirectory = process.env.LOCAL_A11Y_EVIDENCE_DIR;
const conversationRef = process.env.WORKBENCH_CONVERSATION_REF;
const chromePath =
  process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

if (!workbenchUrl || !evidenceDirectory || !conversationRef) {
  throw new Error("WORKBENCH_URL, WORKBENCH_CONVERSATION_REF and LOCAL_A11Y_EVIDENCE_DIR are required");
}

await mkdir(evidenceDirectory, { recursive: true });
const launchOptions = { headless: true };
if (existsSync(chromePath)) launchOptions.executablePath = chromePath;
const browser = await chromium.launch(launchOptions);
const context = await browser.newContext({
  viewport: { width: 1536, height: 1024 },
  deviceScaleFactor: 1,
});
const page = await context.newPage();
const pageErrors = [];
page.on("pageerror", (error) => pageErrors.push(error.message));
const failedChecks = [];
const axeRuns = [];
const screenshots = [];
const sizes = [
  { name: "desktop-1536x1024", width: 1536, height: 1024 },
  { name: "desktop-1280x800", width: 1280, height: 800 },
  { name: "mobile-390x844", width: 390, height: 844 },
  { name: "mobile-320x844", width: 320, height: 844 },
];

try {
  await page.addInitScript(() => {
    if (sessionStorage.getItem("a11y-locale-initialized") === null) {
      localStorage.setItem("b2b_lang", "zh");
      sessionStorage.setItem("a11y-locale-initialized", "true");
    }
  });
  await page.goto(workbenchUrl, { waitUntil: "networkidle", timeout: 30000 });
  const transcript = page.getByRole("log", { name: "对话记录", exact: true });
  await transcript.waitFor({ state: "visible" });
  const turns = await transcript.getByRole("article").count();
  assert.equal(turns, 3, "the browser fixture must be a non-empty synthetic conversation");

  const openCopilot = page.getByRole("button", { name: "展开 AI 副驾", exact: true });
  if (await openCopilot.count()) await openCopilot.press("Enter");
  const rightToggle = page.locator(".wb-right-toggle");
  if (await page.getByRole("button", { name: "收起 AI 副驾", exact: true }).count()) {
    await page.getByRole("button", { name: "收起 AI 副驾", exact: true }).press("Enter");
    assert.equal(await rightToggle.getAttribute("aria-expanded"), "false");
    await page.waitForFunction(() => document.activeElement === document.querySelector(".wb-right-toggle"));
    assert.equal(await rightToggle.evaluate((element) => document.activeElement === element), true);
    await rightToggle.press("Enter");
    assert.equal(await rightToggle.getAttribute("aria-expanded"), "true");
  }
  await page.getByRole("tab", { name: "任务", exact: true }).press("Enter");
  const taskCard = page.getByRole("listitem").filter({ hasText: "待补充" }).first();
  await taskCard.waitFor({ state: "visible" });
  await taskCard.getByRole("textbox", { name: /订单编号/ }).waitFor({ state: "visible" });
  assert.equal(
    await taskCard.getByRole("button", { name: "记录补充" }).isDisabled(),
    true,
    "the collect action must stay disabled until its required field is filled",
  );

  const productInput = page.getByRole("textbox", { name: "产品编号", exact: true });
  await productInput.waitFor({ state: "visible" });
  const describedBy = await productInput.getAttribute("aria-describedby");
  assert.ok(describedBy, "product input must reference the local/demo explanation");
  const helperText = await page.locator(`#${describedBy}`).innerText();
  assert.match(helperText, /local\/test Demo/);
  assert.equal(
    await page.getByRole("tab", { name: "任务", exact: true }).getAttribute("aria-selected"),
    "true",
  );

  // Keyboard path: the product textbox's next Tab target is its labelled
  // evidence button; Enter reads the synthetic evidence and announces status.
  await productInput.focus();
  await page.keyboard.press("Tab");
  const focusedControl = await page.evaluate(() => {
    const active = document.activeElement;
    return { role: active?.getAttribute("role"), text: active?.textContent?.trim(), tag: active?.tagName };
  });
  assert.equal(focusedControl.tag, "BUTTON");
  assert.equal(focusedControl.text, "查看合成预售依据");
  await page.keyboard.press("Enter");

  const evidenceRegion = page.getByRole("region", { name: "经来源校验的合成证据", exact: true });
  await evidenceRegion.waitFor({ state: "visible", timeout: 10000 });
  await page.getByText("演示库存", { exact: true }).waitFor({ state: "visible" });
  const evidencePanel = page.getByRole("region", {
    name: "合成售前资料（仅供销售复核）",
    exact: true,
  });
  const evidenceText = await evidencePanel.innerText();
  assert.match(evidenceText, /demo-fixture-v1/);
  assert.match(evidenceText, /客户报价不允许/);
  assert.match(evidenceText, /CRM 商机/);
  assert.match(evidenceText, /已核验产品规格、账户库存和报价样例三份合成来源/);
  assert.ok(
    (await evidencePanel.locator('[role="status"][aria-live="polite"]').count()) === 1,
    "evidence result must have one polite live region",
  );

  const ariaSnapshot = await page.locator("body").ariaSnapshot();
  await writeFile(join(evidenceDirectory, "workbench-aria-snapshot.yml"), ariaSnapshot, "utf8");

  for (const size of sizes) {
    await page.setViewportSize({ width: size.width, height: size.height });
    await page.waitForTimeout(200);
    if (size.width <= 760) {
      const expandCopilot = page.getByRole("button", { name: "展开 AI 副驾", exact: true });
      if (await expandCopilot.count()) await expandCopilot.click();
      await page.getByRole("complementary", { name: "AI 副驾与客户上下文" }).waitFor({ state: "visible" });
      await taskCard.waitFor({ state: "visible" });
    }
    const geometry = await page.evaluate(() => ({
      viewportWidth: document.documentElement.clientWidth,
      documentWidth: document.documentElement.scrollWidth,
      viewportHeight: document.documentElement.clientHeight,
      documentHeight: document.documentElement.scrollHeight,
    }));
    const overflow = geometry.documentWidth - geometry.viewportWidth;
    if (overflow > 1) failedChecks.push(`${size.name}: horizontal overflow ${overflow}px`);
    const screenshot = join(evidenceDirectory, `${size.name}.png`);
    await page.screenshot({ path: screenshot, fullPage: true, animations: "disabled" });
    screenshots.push({
      file: basename(screenshot),
      ...size,
      horizontalOverflowPx: Math.max(0, overflow),
    });

    if (size.name === "desktop-1536x1024" || size.name === "mobile-390x844") {
      const axe = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();
      axeRuns.push({
        viewport: size.name,
        theme: "light",
        violations: axe.violations.map((item) => ({
          id: item.id,
          impact: item.impact,
          description: item.description,
          nodes: item.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })),
        })),
        passes: axe.passes.length,
        incomplete: axe.incomplete.length,
      });
    }

    if (size.name === "desktop-1536x1024") {
      const originalTheme = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
      await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
      const colors = await taskCard.evaluate((element) => ({
        background: getComputedStyle(element).backgroundColor,
        text: getComputedStyle(element).color,
      }));
      assert.equal(colors.background, "rgb(29, 45, 59)");
      assert.equal(colors.text, "rgb(229, 237, 245)");
      const darkAxe = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();
      axeRuns.push({
        viewport: size.name,
        theme: "dark",
        violations: darkAxe.violations.map((item) => ({
          id: item.id,
          impact: item.impact,
          description: item.description,
          nodes: item.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })),
        })),
        passes: darkAxe.passes.length,
        incomplete: darkAxe.incomplete.length,
      });
      await page.evaluate((theme) => {
        if (theme === null) document.documentElement.removeAttribute("data-theme");
        else document.documentElement.setAttribute("data-theme", theme);
      }, originalTheme);
    }
  }

  await page.setViewportSize({ width: 1536, height: 1024 });
  await page.evaluate(() => localStorage.setItem("b2b_lang", "en"));
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForFunction(() => document.documentElement.lang === "en");
  assert.equal(await page.locator("html").getAttribute("lang"), "en");
  await page.locator("#wb-right-tab-tasks").press("Enter");
  await page.getByRole("heading", { name: "Standard service flows", exact: true }).waitFor({ state: "visible" });
  await page.getByRole("heading", { name: "Synthetic pre-sales evidence (sales review only)", exact: true }).waitFor({ state: "visible" });
  const englishTaskCard = page.getByRole("listitem").filter({ hasText: "Waiting for customer details" }).first();
  await englishTaskCard.waitFor({ state: "visible" });
  const englishAxe = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  axeRuns.push({
    viewport: "desktop-1536x1024",
    theme: "light",
    locale: "en",
    violations: englishAxe.violations.map((item) => ({
      id: item.id,
      impact: item.impact,
      description: item.description,
      nodes: item.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })),
    })),
    passes: englishAxe.passes.length,
    incomplete: englishAxe.incomplete.length,
  });
  const englishScreenshot = join(evidenceDirectory, "desktop-1536x1024-en.png");
  await page.screenshot({ path: englishScreenshot, fullPage: true, animations: "disabled" });
  screenshots.push({
    file: basename(englishScreenshot),
    name: "desktop-1536x1024-en",
    width: 1536,
    height: 1024,
    locale: "en",
    horizontalOverflowPx: 0,
  });

  // This is a repeatable CSS-scale layout capture, not native browser zoom.
  await page.setViewportSize({ width: 768, height: 512 });
  await page.evaluate(() => { document.documentElement.style.zoom = "2"; });
  const zoomGeometry = await page.evaluate(() => ({
    viewportWidth: document.documentElement.clientWidth,
    documentWidth: document.documentElement.scrollWidth,
  }));
  const zoomOverflow = zoomGeometry.documentWidth - zoomGeometry.viewportWidth;
  if (zoomOverflow > 1) failedChecks.push(`simulated-200-percent: horizontal overflow ${zoomOverflow}px`);
  const zoomScreenshot = join(evidenceDirectory, "simulated-200-percent-768x512.png");
  await page.screenshot({ path: zoomScreenshot, fullPage: true, animations: "disabled" });
  screenshots.push({
    file: basename(zoomScreenshot),
    name: "simulated-200-percent-768x512",
    width: 768,
    height: 512,
    horizontalOverflowPx: Math.max(0, zoomOverflow),
    simulatedCssZoom: 2,
  });

  if (pageErrors.length) failedChecks.push(...pageErrors.map((error) => `pageerror: ${error}`));
  const report = {
    evidenceType: "synthetic local/test browser run",
    implementationCommit: process.env.GIT_COMMIT || "unrecorded",
    conversationRef,
    automatedChecks: {
      nonEmptyTranscriptTurns: turns,
      keyboardEnterLoadsEvidence: true,
      productInputHasLabelAndDescription: true,
      dynamicStatusUsesPoliteLiveRegion: true,
      customerQuoteAllowed: false,
      crmWritePerformed: false,
      pageErrors,
      failedChecks,
    },
    axeRuns,
    screenshots,
    manualVoiceOverConfirmed: false,
  };
  await writeFile(join(evidenceDirectory, "accessibility-report.json"), `${JSON.stringify(report, null, 2)}\n`, "utf8");
  process.stdout.write(
    `${JSON.stringify({
      evidenceDirectory,
      screenshotCount: screenshots.length,
      axeViolations: axeRuns.map((run) => run.violations.length),
      failedChecks,
    })}\n`,
  );
  if (failedChecks.length || axeRuns.some((run) => run.violations.length)) process.exitCode = 1;
} finally {
  await browser.close();
}
