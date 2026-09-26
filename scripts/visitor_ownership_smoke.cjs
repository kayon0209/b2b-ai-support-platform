/**
 * Visitor ownership gate smoke test — the feature-list 2.2/2.5 red line.
 *
 * Drives the running deployment through its real API (no browser, no Chatwoot)
 * and proves three states of the read path:
 *
 *   1. ANONYMOUS visitor asks "where is my order" -> the run is refused with
 *      IDENTITY_REQUIRED *before* any order data is read, and nothing about any
 *      order reaches the timeline.
 *   2. VERIFIED visitor (proved SO-9001 with phone tail 8888 -> account "acme")
 *      asks about SO-9001 -> the run completes and a card for SO-9001 is shown.
 *   3. The SAME verified "acme" visitor asks about SO-9002 (account "other-co")
 *      -> the receipt's account does not match the proven one, so the run is
 *      refused with IDENTITY_MISMATCH and no card for SO-9002 appears.
 *
 * Run against a stack where the worker is up and admin-demo is seeded:
 *
 *   node scripts/visitor_ownership_smoke.cjs
 *
 * It only needs the API (:8000) and the worker; Chatwoot is irrelevant.
 */

const BASE = process.env.APP_API_BASE || "http://127.0.0.1:8000/v1/support";
const TENANT = process.env.APP_TENANT || "admin-demo";
const RUN_TIMEOUT_MS = Number(process.env.APP_RUN_TIMEOUT_MS || 150000);

const notes = [];
const failures = [];
const started = Date.now();
const HARD_DEADLINE_MS = Number(process.env.APP_HARD_DEADLINE_MS || RUN_TIMEOUT_MS * 3 + 60_000);

const watchdog = setTimeout(() => {
  failures.push(`did not finish within ${Math.round(HARD_DEADLINE_MS / 1000)}s; reporting what is known`);
  report();
  process.exit(1);
}, HARD_DEADLINE_MS);

async function post(path, body, headers = {}) {
  const resp = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
  const text = await resp.text();
  let json = null;
  try { json = JSON.parse(text); } catch {}
  return { status: resp.status, json, text };
}

async function openSession(visitorId) {
  const r = await post("/sessions", { tenant_slug: TENANT, visitor_id: visitorId });
  if (r.status !== 200 || !r.json || !r.json.token) {
    throw new Error(`openSession failed: ${r.status} ${r.text}`);
  }
  return r.json;
}

async function verify(token, orderId, tail) {
  const r = await post("/verify", { order_id: orderId, phone_tail: tail }, {
    Authorization: `Bearer ${token}`,
  });
  return r;
}

async function sendMessage(token, text) {
  const idem = (globalThis.crypto || require("crypto")).randomUUID();
  const r = await post("/messages", { text }, {
    Authorization: `Bearer ${token}`,
    "Idempotency-Key": idem,
  });
  return r;
}

async function timeline(token) {
  const resp = await fetch(`${BASE}/timeline`, { headers: { Authorization: `Bearer ${token}` } });
  if (!resp.ok) return [];
  const body = await resp.json();
  return Array.isArray(body.items) ? body.items : [];
}

async function waitFor(token, predicate, label) {
  const deadline = Date.now() + RUN_TIMEOUT_MS;
  for (;;) {
    const items = await timeline(token);
    const hit = predicate(items);
    if (hit) return items;
    if (Date.now() > deadline) {
      failures.push(`timeout waiting for: ${label}`);
      return items;
    }
    await new Promise((r) => setTimeout(r, 2500));
  }
}

function lastAgentText(items) {
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].role === "agent") return items[i].text || "";
  }
  return "";
}

function lastCard(items) {
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].role === "tool" && items[i].card) return items[i].card;
  }
  return null;
}

function hasOrderCard(items, orderId) {
  return items.some((t) => t.role === "tool" && t.card && String(t.card.title) === orderId);
}

// States 2 and 3 share one verified conversation (that is the point: the
// verified session is the thing being tested), so the timeline already holds
// earlier replies. Waiting on "an agent message exists" therefore returns the
// *previous* state's answer instantly and the assertion reads a stale reply -
// which is how a passing gate looked like a failure. Count instead, and wait
// for a reply that did not exist before the question was sent.
function agentReplyCount(items) {
  return items.filter((t) => t.role === "agent" && t.text && t.text.length > 0).length;
}

async function agentReplyCountNow(token) {
  return agentReplyCount(await timeline(token));
}

const IDENTITY_REQUIRED_MARK = "验证身份";
const IDENTITY_MISMATCH_MARK = "不一致";

async function main() {
  const vid = `ownership-smoke-${Date.now()}`;

  // --- State 1: anonymous must be refused, no data leaks ---
  const s0 = await openSession(vid);
  notes.push(`ok   anonymous session opened (token len ${s0.token.length})`);
  const m1 = await sendMessage(s0.token, "What is the status of order SO-9001?");
  if (m1.status !== 200) {
    failures.push(`anonymous /messages returned ${m1.status}`);
    return;
  }
  const items1 = await waitFor(
    s0.token,
    (it) => it.some((t) => t.role === "agent"),
    "anonymous agent reply",
  );
  const reply1 = lastAgentText(items1);
  if (!reply1.includes(IDENTITY_REQUIRED_MARK)) {
    failures.push(`anonymous was NOT refused: "${reply1.slice(0, 80)}"`);
  } else {
    notes.push("ok   anonymous refused with identity-verification prompt");
  }
  if (hasOrderCard(items1, "SO-9001") || hasOrderCard(items1, "SO-9002")) {
    failures.push("anonymous got an order card — the red line is broken");
  } else {
    notes.push("ok   no order data reached the timeline for an anonymous visitor");
  }

  // --- State 2: verify ownership of SO-9001 (acme) then read it ---
  const v = await verify(s0.token, "SO-9001", "8888");
  if (v.status !== 200 || !v.json || v.json.verified_account !== "acme") {
    failures.push(`/verify SO-9001/8888 failed: ${v.status} ${JSON.stringify(v.json || v.text)}`);
    return;
  }
  const s1 = { token: v.json.token, account: v.json.verified_account };
  notes.push(`ok   verified as account "${s1.account}" via /verify`);
  const base2 = await agentReplyCountNow(s1.token);
  await sendMessage(s1.token, "What is the status of order SO-9001?");
  const items2 = await waitFor(
    s1.token,
    (it) => hasOrderCard(it, "SO-9001") && agentReplyCount(it) > base2,
    "verified card + fresh reply for SO-9001",
  );
  const card2 = lastCard(items2);
  if (!card2 || String(card2.title) !== "SO-9001") {
    failures.push(`verified acme could not read SO-9001 (its own order): card=${JSON.stringify(card2)}`);
  } else {
    notes.push("ok   verified acme read SO-9001 (its own order) and got the card");
  }

  // --- State 3: same acme visitor asks about SO-9002 (other-co) ---
  const base3 = await agentReplyCountNow(s1.token);
  await sendMessage(s1.token, "What is the status of order SO-9002?");
  const items3 = await waitFor(
    s1.token,
    (it) => agentReplyCount(it) > base3,
    "a NEW agent reply for the SO-9002 question",
  );
  const reply3 = lastAgentText(items3);
  if (!reply3.includes(IDENTITY_MISMATCH_MARK)) {
    failures.push(`cross-account read was NOT refused: "${reply3.slice(0, 80)}"`);
  } else {
    notes.push("ok   acme asking about other-co's SO-9002 was refused (IDENTITY_MISMATCH)");
  }
  if (hasOrderCard(items3, "SO-9002")) {
    failures.push("SO-9002 card leaked to a non-owner — the gate failed");
  } else {
    notes.push("ok   no SO-9002 card delivered to a non-owner");
  }

  // --- Negative: wrong tail must not verify ---
  const bad = await verify(s0.token, "SO-9001", "0000");
  if (bad.status === 200 && bad.json && bad.json.verified_account) {
    failures.push("wrong phone tail still verified an account");
  } else {
    notes.push("ok   wrong phone tail is refused by /verify");
  }
}

function report() {
  for (const note of notes) console.log(note);
  console.log("");
  for (const failure of failures) console.log("FAIL " + failure);
  console.log(
    `\nSUMMARY ownership gate: ${failures.length ? failures.length + " failure(s)" : "all three states correct"}` +
    ` (${Math.round((Date.now() - started) / 1000)}s)`,
  );
}

main().then(
  () => {
    clearTimeout(watchdog);
    report();
    process.exit(failures.length ? 1 : 0);
  },
  (err) => {
    clearTimeout(watchdog);
    failures.push("SMOKE ERROR: " + (err && err.message));
    report();
    process.exit(2);
  },
);
