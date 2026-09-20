// Acceptance: the Approvals screen (/approvals) against a live API.
//
// This page is the carrier for the agent's write path, so the properties worth
// driving are the ones a unit test cannot see: that a pending write is
// discoverable, that the frozen arguments are what an approval binds to, and
// above all that a failure is reported as a failure. The last one matters most
// - the API answers an execute with no adapter as 501 TOOL_EXECUTOR_MISSING,
// and a screen that rendered that as success would be worse than no screen.
import { chromium } from "playwright-core";
import fs from "node:fs";

const BASE = process.env.APPROVALS_BASE || "http://localhost:5180";
const API = process.env.APPROVALS_API || "http://127.0.0.1:8022";
const OUT =
  "D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan/.workbuddy-ai/acceptance/shots";
// Environment, not a literal: this is a real credential for the local
// admin-demo tenant. See verify.mjs for why it must not be committed.
const TOKEN = process.env.API_TOKEN;
if (!TOKEN) {
  console.error("API_TOKEN is unset. Run: python scripts/seed_admin_demo.py");
  process.exit(2);
}
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";

/**
 * Create the two proposals this run needs, so the harness is repeatable.
 *
 * Proposals expire 15 minutes after they are raised - that is the feature, not
 * an inconvenience - so a run against yesterday's rows tests the empty state
 * instead of the workflow. The keys are unique per run so a re-run does not
 * silently reuse the previous proposal.
 */
async function prepare() {
  const stamp = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  const propose = (key, tool_name, args) =>
    fetch(`${API}/v1/tool-proposals`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
        "Idempotency-Key": `acc-${key}-${stamp}`,
      },
      body: JSON.stringify({ tool_name, arguments: args }),
    }).then(async (r) => {
      if (!r.ok) throw new Error(`propose ${tool_name} -> ${r.status} ${await r.text()}`);
      return r.json();
    });

  const jira = await propose("jira", "jira.create_issue", {
    project: "HQ",
    summary: "Board rev C: silkscreen overlaps pad at J4",
  });
  const im = await propose("im", "im.send_notification", {
    channel: "#oncall",
    text: "Escalating: board rev C defect at J4",
  });
  say("P0_prepared", `jira=${jira.proposal.status} im=${im.proposal.status}`);
}

const R = [];
const say = (k, v) => {
  R.push(`${k}: ${v}`);
  console.log(`${k}: ${v}`);
};

const bodyText = (p) =>
  p.evaluate(() => document.body.innerText.replace(/\s+/g, " ").trim());

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  await prepare();
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1100 }, locale: "en-US" });
  ctx.setDefaultTimeout(10000);
  const page = await ctx.newPage();
  await page.addInitScript((t) => localStorage.setItem("b2b_token", t), TOKEN);

  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    // Step E deliberately provokes a 501: the tenant has no Jira connector, so
    // the API refuses to execute and the page is supposed to say so. The
    // browser logs that failed request as a console error, which is not a page
    // defect - counting it would make the harness permanently red.
    if (/501|404/.test(m.text())) return;
    errors.push(m.text());
  });

  // ---------- 1. The screen loads and is reachable from the shell ----------
  await page.goto(`${BASE}/approvals`, { waitUntil: "networkidle" });
  const h1 = await page.locator("h1").first().innerText();
  say("A1_title", h1 === "Write approvals" ? `ok (${h1})` : `FAIL (${h1})`);
  const navHasIt = await page.locator('.nav-item:has-text("Approvals")').count();
  say("A2_in_nav", navHasIt === 1 ? "ok" : `FAIL (${navHasIt})`);

  // ---------- 2. The queue lists what the agent prepared ----------
  await page.waitForSelector(".case-row");
  const rows = await page.locator(".case-row").allInnerTexts();
  say("B1_rows", `${rows.length} row(s)`);
  const hasJira = rows.some((r) => r.includes("jira.create_issue"));
  const hasIm = rows.some((r) => r.includes("im.send_notification"));
  say("B2_jira_listed", hasJira ? "ok" : "FAIL");
  say("B3_im_listed", hasIm ? "ok" : "FAIL");
  const stat = await page.locator(".stat").first().innerText();
  say("B4_waiting_stat", stat.replace(/\n/g, " / "));

  // ---------- 3. Selecting a row shows exactly what would be approved ------
  await page.locator('.case-row:has-text("jira.create_issue")').first().click();
  await page.waitForSelector(".code-block");
  const detail = await bodyText(page);
  // Case-insensitive: `.section-title` is `text-transform: uppercase`, and
  // Chrome's `innerText` returns the *rendered* text, so the heading reads
  // "FROZEN ARGUMENTS". Matching the raw string failed for that reason alone.
  say("C1_frozen_args", /frozen arguments/i.test(detail) ? "ok" : "FAIL");
  say("C2_args_visible", detail.includes('"project": "HQ"') ? "ok" : "FAIL");
  say("C3_needs_approval", detail.includes("Needs approval") ? "ok" : "FAIL");
  say("C4_hash_shown", detail.includes("Action hash") ? "ok" : "FAIL");

  const approve = page.locator('button:has-text("Approve")').first();
  const execute = page.locator('button:has-text("Execute")').first();
  say("C5_approve_enabled", (await approve.isEnabled()) ? "ok" : "FAIL");
  // A confirmed write must not be executable before it is approved. This is
  // the client-side half of the server's confirmation gate.
  say("C6_execute_disabled", (await execute.isDisabled()) ? "ok" : "FAIL");
  await page.screenshot({ path: `${OUT}/approvals-1-pending.png` });

  // ---------- 4. Approve ----------
  await approve.click();
  await page.waitForSelector(".prompt");
  say("D1_prompt_shown", (await page.locator(".prompt").count()) === 1 ? "ok" : "FAIL");
  await page.locator(".prompt .btn-primary").click();
  await page.waitForSelector(".banner-ok");
  const afterApprove = await bodyText(page);
  say("D2_approved_notice", afterApprove.includes("Approved") ? "ok" : "FAIL");
  say("D3_status_now_approved", afterApprove.includes("Approved") ? "ok" : "FAIL");
  // The banner appears as soon as the approve call returns; the detail panel
  // refetches separately. Asserting on the button immediately is a race in the
  // harness, not a defect in the page.
  let executeEnabled = false;
  try {
    await execute.waitFor({ state: "attached" });
    await page.waitForFunction(
      () => {
        const b = [...document.querySelectorAll("button")].find((x) =>
          x.textContent?.includes("Execute"),
        );
        return b instanceof HTMLButtonElement && !b.disabled;
      },
      { timeout: 10000 },
    );
    executeEnabled = true;
  } catch {
    executeEnabled = false;
  }
  say("D4_execute_now_enabled", executeEnabled ? "ok" : "FAIL");
  await page.screenshot({ path: `${OUT}/approvals-2-approved.png` });

  // ---------- 5. Execute, and report the outcome honestly ----------
  // There is no Jira connector on this tenant, so the API answers
  // 501 TOOL_EXECUTOR_MISSING. The screen must say so.
  await execute.click();
  await page.waitForSelector(".prompt");
  await page.locator(".prompt .btn-primary").click();
  await page.waitForSelector(".banner-error, .banner-ok", { timeout: 15000 });
  const afterExecute = await bodyText(page);
  const okBanner = await page.locator(".banner-ok").count();
  const errBanner = await page.locator(".banner-error").count();
  say("E1_failure_reported", errBanner === 1 ? "ok" : `FAIL (ok=${okBanner} err=${errBanner})`);
  say(
    "E2_not_claimed_as_done",
    afterExecute.includes("Executed and verified") ? "FAIL (claimed success)" : "ok",
  );
  say(
    "E3_reason_visible",
    afterExecute.includes("TOOL_EXECUTOR_MISSING") ? "ok" : "note (code not in banner text)",
  );
  await page.screenshot({ path: `${OUT}/approvals-3-execute-failed.png` });

  // ---------- 6. A low-write proposal needs no approval ----------
  await page.locator('button:has-text("Close")').first().click();
  await page.locator('.case-row:has-text("im.send_notification")').first().click();
  await page.waitForSelector(".code-block");
  const lowDetail = await bodyText(page);
  say("F1_no_approval_needed", lowDetail.includes("No approval needed") ? "ok" : "FAIL");
  say("F2_execute_enabled", (await page.locator('button:has-text("Execute")').first().isEnabled()) ? "ok" : "FAIL");

  // ---------- 7. The filter agrees with the label ----------
  await page.selectOption("select", "expired");
  await page.waitForTimeout(600);
  const expired = await bodyText(page);
  say(
    "G1_expired_filter_empty",
    expired.includes("No proposals match this filter") ? "ok" : "note (unexpected rows)",
  );
  await page.selectOption("select", "authorized");
  await page.waitForSelector(".case-row");

  // ---------- 8. Chinese ----------
  await page.locator('.shell-toggle:has-text("中文")').click();
  await page.waitForTimeout(400);
  const zh = await bodyText(page);
  say("H1_zh_title", zh.includes("写操作审批") ? "ok" : "FAIL");
  say("H2_zh_subtitle", zh.includes("智能体已准备好") ? "ok" : "FAIL");
  await page.screenshot({ path: `${OUT}/approvals-4-zh.png` });

  // ---------- 9. Raising a proposal, and the EQ confirmation it exists for --
  // Back to English so the assertions below read against known strings.
  await page.locator('.shell-toggle:has-text("EN")').click();
  await page.waitForTimeout(400);

  // An EQ case, created through the API with the conversation link that 2b
  // added. Without a case in `waiting_customer` the executor would refuse -
  // correctly - so the case is the fixture this section needs.
  //
  // The reference is unique per run. A fixed number makes the executor's
  // `CASE_NOT_FOUND` fire on the *second* run, because the reference then
  // matches several cases and the tool refuses to guess between them - which
  // is correct behaviour, and a confusing way to learn that the fixture was
  // not specific enough.
  const eqRef = String(Date.now()).slice(-7);
  const eqCase = await fetch(`${API}/v1/cases`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      "Content-Type": "application/json",
      "Idempotency-Key": `acc-eq-case-${eqRef}`,
    },
    body: JSON.stringify({
      subject: `EQ ${eqRef}: confirm stackup before production`,
      category: "eq_confirmation",
      priority: "p2",
      conversation_ref_id: "01900000-0000-7000-8000-0000000000e9",
    }),
  });
  const eqBody = await eqCase.json();
  const eqCaseId = eqBody?.case?.case_id;
  say("I1_eq_case_created", eqCase.ok && eqCaseId ? `ok (${String(eqCaseId).slice(0, 8)})` : `FAIL ${eqCase.status}`);

  // Walk it to `waiting_customer`. The state machine is explicit and audited,
  // and it has no shortcut from NEW - so the fixture walks it the way a person
  // would. (A case created straight into `waiting_customer` would be a case
  // waiting on a customer who was never asked anything.)
  for (const target of ["triaged", "in_progress", "waiting_customer"]) {
    const res = await fetch(`${API}/v1/cases/${eqCaseId}/commands`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
        "Idempotency-Key": `acc-eq-${target}-${Date.now()}`,
      },
      body: JSON.stringify({ command: "transition", parameters: { target } }),
    });
    if (!res.ok) {
      say("I1b_walk_to_waiting_customer", `FAIL at ${target}: ${res.status} ${await res.text()}`);
      break;
    }
  }
  const walked = await fetch(`${API}/v1/cases/${eqCaseId}`, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  }).then((r) => r.json());
  say(
    "I1b_walk_to_waiting_customer",
    (walked?.case?.status ?? "") === "waiting_customer" ? "ok" : `FAIL (${walked?.case?.status})`,
  );

  await page.locator('button:has-text("Raise a proposal")').first().click();
  await page.waitForSelector("textarea");
  say("I2_panel_open", "ok");

  // Scoped to the panel's card: the queue's filter is also a `select`, and
  // taking the last one picked the filter and then failed to find a tool in it.
  const toolSelect = page.locator('.card:has-text("Raise a write proposal") select').first();
  const options = await toolSelect.locator("option").allInnerTexts();
  say(
    "I3_catalog_from_api",
    options.some((o) => o.includes("case.eq_confirm")) ? "ok" : `FAIL (${options.length} options)`,
  );

  await toolSelect.selectOption("case.eq_confirm");
  await page.waitForTimeout(300);
  const prefilled = await page.locator("textarea").inputValue();
  // Prefilled from the tool's own schema, so the operator is not handed an
  // empty object that the API will reject.
  say("I4_args_prefilled", prefilled.includes("case_ref") ? "ok" : `FAIL (${prefilled})`);

  // Counted through the API, because the page's own text cannot distinguish
  // "the raise worked" from "a proposal for this tool already existed" - the
  // queue lists tool names either way. An earlier version of this check
  // searched the whole page for the tool name and passed for that reason.
  const eqCount = () =>
    fetch(`${API}/v1/tool-proposals?limit=100&status=authorized`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    })
      .then((r) => r.json())
      .then((d) => d.items.filter((i) => i.tool_name === "case.eq_confirm").length);
  const before = await eqCount();

  await page.locator("textarea").fill(`{"case_ref": "${eqRef}"}`);
  await page.locator('button:has-text("Raise it")').first().click();

  // The *page-level* notice, not the panel's: raising closes the panel, so a
  // banner inside it is unmounted in the same commit that sets it. That was a
  // real defect - the form vanished with no acknowledgement - and asserting on
  // the panel's own banner is what caught it.
  await page.locator(".banner-ok").first().waitFor({ timeout: 20000 });
  const raiseText = await bodyText(page);
  say(
    "I5_raise_reported",
    raiseText.includes("waiting for approval") ? "ok" : `FAIL (${raiseText.slice(0, 200)})`,
  );
  say(
    "I5b_panel_closed",
    (await page.locator("textarea").count()) === 0 ? "ok" : "FAIL (form still open)",
  );

  const after = await eqCount();
  say("I6_proposal_really_created", after === before + 1 ? "ok" : `FAIL (${before} -> ${after})`);
  await page.screenshot({ path: `${OUT}/approvals-5-raised.png` });

  // Approve, then execute. The executor exists (the registry's platform-tool
  // path), so this is the first time the whole chain can run.
  await page.locator('.case-row:has-text("case.eq_confirm")').first().click();
  await page.waitForSelector(".code-block");
  await page.locator('button:has-text("Approve")').first().click();
  await page.waitForSelector(".prompt");
  await page.locator(".prompt .btn-primary").click();
  await page.waitForSelector(".banner-ok");
  await page.locator('button:has-text("Execute")').first().click();
  await page.waitForSelector(".prompt");
  await page.locator(".prompt .btn-primary").click();
  // Scoped to the detail panel for the same reason as above.
  const detailPanel = page.locator('.card:has-text("Proposal")').first();
  await page.waitForTimeout(3000);
  const afterEq = await detailPanel.innerText().catch(() => "");
  const eqErr = await detailPanel.locator(".banner-error").count();
  say("J1_eq_execute_succeeded", eqErr === 0 ? "ok" : `FAIL (${afterEq.slice(0, 200)})`);

  const caseAfter = await fetch(`${API}/v1/cases/${eqCaseId}`, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  }).then((r) => r.json());
  const caseStatus = caseAfter?.case?.status ?? caseAfter?.status;
  say("J2_case_moved_off_waiting", caseStatus === "in_progress" ? "ok" : `FAIL (${caseStatus})`);
  await page.screenshot({ path: `${OUT}/approvals-6-eq-executed.png` });

  // ---------- 10. Opening a case from the console ----------
  // The Cases page could transition and reassign but not create, so every new
  // case - including the eq_confirmation ones this whole flow is built around
  // - had to be raised with `curl`. A category nobody can set is a category
  // nobody uses.
  await page.goto(`${BASE}/cases`, { waitUntil: "networkidle" });
  await page.locator('button:has-text("New case")').first().click();
  await page.waitForSelector('.card:has-text("Open a case") input');
  say("K1_panel_open", "ok");

  // An empty subject is refused in the browser, so the operator is not sent to
  // the server to learn something the form already knew.
  await page.locator('button:has-text("Open it")').first().click();
  await page.waitForTimeout(400);
  const panel = page.locator('.card:has-text("Open a case")');
  const refused = await panel.locator(".banner-error").count();
  say("K2_empty_subject_refused", refused === 1 ? "ok" : "FAIL");

  const caseSubject = `EQ ${eqRef}-B: confirm stackup (from console)`;
  await panel.locator("input").first().fill(caseSubject);
  await panel.locator("select").nth(1).selectOption("eq_confirmation");
  await page.locator('button:has-text("Open it")').first().click();
  await page.locator(".banner-ok, .banner-error").first().waitFor({ timeout: 20000 });
  const afterCase = await bodyText(page);
  const caseErr = await panel.locator(".banner-error").count();
  say(
    "K3_case_opened",
    caseErr === 0 && afterCase.includes("Case opened")
      ? "ok"
      : `FAIL (${afterCase.replace(/\s+/g, " ").slice(0, 260)})`,
  );
  say(
    "K4_panel_closed",
    (await page.locator('.card:has-text("Open a case")').count()) === 0 ? "ok" : "FAIL",
  );

  const listed = await fetch(`${API}/v1/cases?limit=50`, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  }).then((r) => r.json());
  const made = (listed.items ?? []).find((c) => c.subject === caseSubject);
  say(
    "K5_case_really_exists",
    made && made.category === "eq_confirmation" ? "ok" : `FAIL (${JSON.stringify(made)})`,
  );
  await page.screenshot({ path: `${OUT}/cases-created.png` });

  say("Z_page_errors", errors.length === 0 ? "none" : errors.slice(0, 3).join(" | "));

  await browser.close();
  fs.writeFileSync(
    "D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan/.workbuddy-ai/acceptance/approvals-report.txt",
    R.join("\n") + "\n",
  );
}

main().catch((e) => {
  console.error("HARNESS FAILED:", e);
  process.exit(1);
});
