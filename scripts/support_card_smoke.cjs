/**
 * The guard for the customer chat window's data card.
 *
 * Three defects this file exists to catch, none of which the Python suite can
 * see, because each one leaves the API answering correctly:
 *
 *   1. The page renders the receipt, and the receipt is JSON. A `tool` turn's
 *      `text` is the raw receipt; printing it puts `{"order_id": ...}` in a
 *      customer's chat bubble. The API is 200 either way.
 *   2. The page renders a card, but not the one it was given. The count of
 *      stages the API sent and the count drawn on screen are two numbers that
 *      only agree if the render branch is actually wired to `card`.
 *   3. The page stops waiting. A receipt arrives *before* the answer, so a
 *      "reply appeared" condition that accepts a `tool` turn freezes the thread
 *      with a card on screen and no answer under it.
 *
 * Two modes, because they fail for different reasons and a guard that is only
 * runnable when the whole pipeline is healthy is a guard that gets skipped.
 *
 * **Ask mode** (default): open a visitor session, ask the question that makes
 * the platform read an order, and check the card against the timeline.
 *
 *     APP_BASE_URL=http://localhost:5173 APP_TOKEN=pt_<slug>_<user-id> \
 *     node scripts/support_card_smoke.cjs
 *
 * **Adopt mode**: reuse a conversation that already has a receipt, with a
 * visitor token minted for it. This exercises the page and the timeline
 * contract without needing a worker run, so a broken run path cannot hide a
 * broken render - see `scripts/_probe_card_under_app_role.py` for why the two
 * are separable today.
 *
 *     APP_VISITOR_TOKEN=vs_... APP_CONVERSATION_REF=<uuid> \
 *     node scripts/support_card_smoke.cjs
 */

const { chromium } = require("playwright-core");

const BASE = process.env.APP_BASE_URL || "http://localhost:5173";
const TENANT = process.env.APP_TENANT || "admin-demo";
const CHROME = process.env.APP_CHROME || "";
const CHROME_FALLBACK =
  "C:/Users/Rose/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";

/**
 * The question is the fixture. It must select `order.get_status`, which is a
 * keyword decision (`tool_gateway/selector.py`), and it must name an order the
 * configured `business_api` provider actually has. Both defaults come from the
 * shipped demo provider, so this runs on a fresh seed with no arguments.
 *
 * It is in English because it was written while every Chinese phrasing of the
 * same question still routed to `knowledge_qa`, so the tool was never selected
 * and no card could be produced. That gap in `intent.py` was fixed on
 * 2026-09-22, so a Chinese phrasing reaches the tool too now - the measurement
 * and the tables live in `docs/research/chinese-intent-measurement.md` §三.
 *
 * What stops a card here is no longer the question. A visitor session is
 * anonymous, so the read path's ownership gate answers `IDENTITY_REQUIRED`
 * before any connector call, and the card is unreachable until something binds
 * an account to the session (`POST /v1/support/verify`). Check whether the page
 * actually calls it before blaming this script or the render branch.
 */
const QUESTION = process.env.APP_QUESTION || "What is the status of order SO-9001?";
const EXPECTED_ORDER = process.env.APP_ORDER_ID || "SO-9001";
/** The proof the demo provider accepts for `EXPECTED_ORDER`. It comes from
 * `integrations/demo_erp.py`'s `_CONTACT_TAILS` map, not from invention: the
 * provider compares this string and answers `None` (a refusal) on any
 * mismatch, so a wrong value here looks exactly like a broken page. */
const PHONE_TAIL = process.env.APP_PHONE_TAIL || "8888";

const VISITOR_TOKEN = process.env.APP_VISITOR_TOKEN || "";
const CONVERSATION_REF = process.env.APP_CONVERSATION_REF || "";
const ADOPT = Boolean(VISITOR_TOKEN && CONVERSATION_REF);
const VISITOR_ID = process.env.APP_VISITOR_ID || "support-card-smoke";

/** The answer is produced asynchronously by a worker, so this is a wait, not a
 * poll interval: 90s covers a cold model call with room to spare. Measured on
 * this deployment: one run took 88s end to end, so the default is generous on
 * purpose. */
const ANSWER_TIMEOUT_MS = Number(process.env.APP_ANSWER_TIMEOUT_MS || 90000);

/**
 * A guard that hangs reports nothing and blocks whatever runs it - strictly
 * worse than one that fails, because a failure at least says where to look.
 * Measured: an earlier version of this file sat for 18 minutes after the page
 * had already got everything it needed, and printed nothing at all. So: the
 * findings live at module scope, both teardown steps are bounded, and the
 * watchdog below reports whatever is known when the deadline passes.
 */
const notes = [];
const failures = [];
const started = Date.now();
const HARD_DEADLINE_MS = Number(
  process.env.APP_HARD_DEADLINE_MS || ANSWER_TIMEOUT_MS * 2 + 90_000,
);

function report() {
  for (const note of notes) console.log(note);
  console.log("");
  for (const failure of failures) console.log("FAIL " + failure);
  console.log(
    "\nSUMMARY " +
      (process.env.APP_VISITOR_TOKEN ? "[adopt] " : "[ask] ") +
      (failures.length
        ? failures.length + " failure(s)"
        : "the card, the data behind it and the answer agree") +
      ` (${Math.round((Date.now() - started) / 1000)}s)`,
  );
}

const watchdog = setTimeout(() => {
  failures.push(
    `the check did not finish within ${Math.round(HARD_DEADLINE_MS / 1000)}s; reporting what is known`,
  );
  report();
  process.exit(1);
}, HARD_DEADLINE_MS);

async function main() {
  const browser = await chromium.launch({
    executablePath: CHROME || CHROME_FALLBACK,
    headless: true,
  });
  const context = await browser.newContext({ viewport: { width: 900, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);

  const pageErrors = [];
  page.on("pageerror", (err) => pageErrors.push(err.message));

  try {
    if (ADOPT) {
      // Same-origin first, so the page's own storage keys are writable. The
      // visitor id must match the stored session's, because the page treats a
      // mismatch as "a different visitor" and opens a fresh conversation -
      // which would silently test an empty timeline instead of this one.
      await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
      await page.evaluate(
        ({ visitorId, session }) => {
          window.localStorage.setItem("support.visitor.v1", JSON.stringify(visitorId));
          window.localStorage.setItem("support.session.v1", JSON.stringify(session));
        },
        {
          visitorId: VISITOR_ID,
          session: {
            token: VISITOR_TOKEN,
            conversation_ref: CONVERSATION_REF,
            expires_at: Math.floor(Date.now() / 1000) + 3600,
            visitor_id: VISITOR_ID,
            // Adopt mode supplies a token the caller asserts is already bound,
            // so the page must not offer the identity step here: it would sit
            // there unsubmitted and change what this mode actually tests.
            verified_account: process.env.APP_VERIFIED_ACCOUNT || "adopted",
          },
        },
      );
      notes.push("ok   adopted an existing conversation (no run needed)");
    }

    await page.goto(`${BASE}/support?tenant=${encodeURIComponent(TENANT)}`, {
      waitUntil: "domcontentloaded",
    });

    const composer = page.locator(".support-composer input");
    await composer.waitFor({ state: "visible" });
    // The input is disabled until a session is open, and a disabled input is
    // the symptom the visitor-token work was about: it means the page could
    // not authenticate at all.
    await page.waitForFunction(
      () => {
        const el = document.querySelector(".support-composer input");
        return el && !el.disabled;
      },
      null,
      { timeout: 20000 },
    );
    notes.push("ok   session accepted, composer enabled");

    if (!ADOPT) {
      // The identity step is part of the flow now, not an optional extra: an
      // anonymous session cannot read an order at all (the read path answers
      // `IDENTITY_REQUIRED` before calling any connector), so skipping this
      // makes the card unreachable and the guard fail for the wrong reason.
      if (!(await page.locator(".support-verify").count())) {
        failures.push(
          "the page offers no identity step, so an order card can never be reached",
        );
      } else {
        await page.locator(".support-verify-row input").first().fill(EXPECTED_ORDER);
        await page.locator(".support-verify-row input").nth(1).fill(PHONE_TAIL);
        await page.locator(".support-verify-row button").click();
        try {
          await page
            .locator(".support-verified")
            .waitFor({ state: "visible", timeout: 20000 });
          notes.push("ok   identity verified, session bound to an account");
        } catch {
          const shown = (await page.locator(".support-problem").textContent()) || "";
          failures.push(
            `identity verification did not succeed${shown ? `: ${shown.trim()}` : ""}`,
          );
        }
      }
    }

    if (!ADOPT) {
      await composer.fill(QUESTION);
      await page.locator(".support-composer button").click();
    }

    await page.locator(".tool-card").waitFor({ state: "visible", timeout: ANSWER_TIMEOUT_MS });
    notes.push("ok   a card was rendered");

    // The two numbers that have to agree. Read from the API using the token the
    // page itself holds, so this is the customer's own view of the data.
    const token = await page.evaluate(() => {
      try {
        const raw = window.localStorage.getItem("support.session.v1");
        return raw ? JSON.parse(raw).token : null;
      } catch {
        return null;
      }
    });
    if (!token) {
      failures.push("the page holds no visitor session token; cannot cross-check the timeline");
    } else {
      const resp = await fetch(`${BASE}/api/v1/support/timeline`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!resp.ok) {
        failures.push(`GET /api/v1/support/timeline returned ${resp.status}`);
      } else {
        const body = await resp.json();
        const items = Array.isArray(body.items) ? body.items : [];
        const cards = items.filter((t) => t.card);
        if (cards.length === 0) {
          failures.push("the timeline served no card, but the page drew one");
        } else {
          const served = cards[cards.length - 1].card;
          const servedNodes = Array.isArray(served.nodes) ? served.nodes.length : 0;
          const renderedNodes = await page.locator(".tool-card-node").count();
          if (servedNodes !== renderedNodes) {
            failures.push(
              `the timeline served ${servedNodes} stage(s) and the page rendered ${renderedNodes}`,
            );
          } else {
            notes.push(`ok   stages: served=${servedNodes} rendered=${renderedNodes}`);
          }

          const drawnTitle = (await page.locator(".tool-card-title").first().textContent()) || "";
          if (served.title && drawnTitle.trim() !== String(served.title).trim()) {
            failures.push(`the card drew title "${drawnTitle.trim()}", served "${served.title}"`);
          }
          if (isExpectedOrder(served.title)) {
            notes.push(`ok   order ${EXPECTED_ORDER} is on the card`);
          } else {
            failures.push(`the card is for "${served.title}", expected ${EXPECTED_ORDER}`);
          }
        }
      }
    }

    // 1. No raw receipt anywhere a customer can read it. `innerText` is what a
    //    person sees, so JSON hidden in a collapsed element is not a false
    //    pass, but one in a bubble is caught. The markers are receipt keys
    //    rather than a brace heuristic: a guard that cries wolf gets ignored.
    const visible = await page.locator(".support-thread").innerText();
    for (const marker of ['"nodes"', '"found"', '"fetched_at"', '{"']) {
      if (visible.indexOf(marker) >= 0) {
        failures.push(
          "a receipt was rendered as text in the thread (found " +
            marker +
            "): " +
            visible.slice(0, 200),
        );
        break;
      }
    }

    // 3. The answer, not just the card. Only in ask mode: adopt mode reuses a
    //    conversation whose receipt was produced without a question, so there
    //    is nothing that should have answered it.
    if (!ADOPT) {
      await page
        .locator(".support-bubble.is-agent")
        .first()
        .waitFor({ state: "visible", timeout: ANSWER_TIMEOUT_MS })
        .catch(() => failures.push("a card was drawn but no answer ever followed it"));
      if (await page.locator(".support-bubble.is-agent").count()) {
        notes.push("ok   the answer followed the card");
      }
    }

    if (pageErrors.length) {
      failures.push("page errors: " + pageErrors.join(" | "));
    }
  } catch (err) {
    failures.push("SMOKE ERROR: " + (err && err.message));
  } finally {
    // Bounded, because a teardown that hangs must not swallow the result.
    await Promise.race([
      browser.close(),
      new Promise((resolve) => setTimeout(resolve, 10_000)),
    ]);
  }

  clearTimeout(watchdog);
  report();
  return failures.length ? 1 : 0;
}

function isExpectedOrder(title) {
  return typeof title === "string" && title.trim() === EXPECTED_ORDER;
}

main().then(
  (code) => process.exit(code),
  (err) => {
    clearTimeout(watchdog);
    failures.push("SMOKE ERROR: " + (err && err.message));
    report();
    process.exit(2);
  },
);
