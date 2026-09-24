/**
 * The customer-facing chat window (`/support`).
 *
 * Deliberately NOT the operator console's `/chat`: that page authenticates with
 * an operator bearer token and requires `CASE_READ`, so a real customer opening
 * it got 401 and the UI rendered the rejection as an empty conversation
 * (audit F-1/F-2). ADR 0010 responded by declaring the customer experience out
 * of scope; ADR 0011 revisits that, because "the customer cannot open the chat
 * window" is not a scope boundary a product can ship on.
 *
 * How it authenticates
 * --------------------
 * It never touches `lib/api.ts`, on purpose. That module attaches the
 * *operator* token and redirects to the token dialog on 401 — exactly the
 * behaviour that must not happen here. Instead this page opens a visitor
 * session (`POST /v1/support/sessions`), which returns a signed token bound to
 * one (tenant, conversation) pair, and carries that token itself.
 *
 * The visitor id and token live in sessionStorage. A reload of this tab keeps
 * the conversation; closing the browser session does not leave a durable
 * handle to a customer's transcript on a shared workstation.
 *
 * Why it carries its own error copy
 * ---------------------------------
 * Bypassing `lib/api.ts` also meant losing its error taxonomy, and the measured
 * result was a customer staring at a bare `Failed to fetch` with the composer
 * disabled and nothing on the page to click (2026-09-23). `describeFailure`
 * below is the replacement, and it is deliberately narrow: a small set of
 * failures this page can actually cause, each with something to do about it.
 *
 * Language
 * --------
 * Plain Chinese strings rather than the operator i18n bundle: the pilot's
 * customers write Chinese, and pulling this surface into the console's
 * translation set would couple a customer page to an operator bundle. If a
 * second customer language is ever needed it should get its own provider.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ToolCard, type ToolCardData } from "../components/ToolCard";
import "../styles-support.css";

const STORAGE_KEY = "support.session.v1";
const VISITOR_KEY = "support.visitor.v1";

/**
 * Dev serves the SPA from Vite and proxies `/api` to the API, stripping the
 * prefix (see `vite.config.ts`). `lib/api.ts` prepends it; this page bypasses
 * that module on purpose, so it has to prepend it too. Calling `/v1/...`
 * directly gets the SPA fallback's 404 rather than the API's.
 */
const API = "/api";

/** How long to keep polling for an answer before telling the customer. */
const ANSWER_TIMEOUT_MS = 120_000;

type Branding = {
  display_name: string;
  logo_url: string | null;
  primary_color: string | null;
  support_email: string | null;
};

/**
 * Who the platform will answer as.
 *
 * `ai` is the only value that means "a reply is coming from here"; every other
 * value is "a person has it". This page needs the distinction because a
 * conversation handed to a person can never receive an AI reply — the
 * platform's pre-send lease gate refuses one — and without knowing that, this
 * window sat on "客服正在输入…" while nothing was ever going to arrive.
 */
type Owner = "ai" | "human" | "queue" | "expired" | "closed";

type ConversationState = { owner: Owner; mode: string };

/** Whether anyone is there at all. Not the same question as who owns it. */
type SupportWindow = { open: boolean; opens_at_hour: number };

type Session = {
  token: string;
  conversation_ref: string;
  expires_at: number;
  visitor_id: string;
  /**
   * Which tenant this session belongs to.
   *
   * Stored, and checked against the URL before the session is reused, because
   * a session is bound to exactly one (tenant, conversation) pair by its
   * token. Without this the page reused any unexpired session it found and
   * ignored `?tenant=`, so `/support?tenant=other` showed the *previous*
   * tenant's conversation and branding with no error and no hint - measured
   * 2026-09-23. Silently answering as the wrong tenant is the worst of the
   * available outcomes, so a mismatch now opens a new session instead.
   */
  tenant: string;
  /**
   * Absent/null means anonymous — and an anonymous session cannot read an
   * order at all, because the read path's ownership gate answers
   * `IDENTITY_REQUIRED` *before* it calls any connector. So the data card is
   * unreachable until this is set; that is feature 2.2/2.5, not a bug.
   */
  verified_account?: string | null;
  /**
   * Cached so the first paint of a *returning* customer already has the
   * tenant's own name and colour. `/timeline` returns them authoritatively on
   * every load — this copy only covers the moment before that answer arrives.
   */
  branding?: Branding | null;
};

type Turn = {
  role: string;
  text: string;
  at: number;
  source: string;
  /**
   * Present on a `tool` turn when the receipt is a card this platform can
   * render (`agent_runtime/tool_card.py`). Its absence is normal: a receipt
   * for something without a card shape, or a turn from before cards existed.
   *
   * A `tool` turn's `text` is the raw receipt JSON. It is never rendered as a
   * bubble - see the render branch below for why.
   */
  card?: ToolCardData | null;
};

const COPY = {
  /** Only shown when the tenant has configured no name of its own. */
  fallbackBrand: "在线客服",
  pageKind: "在线客服",
  greeting: "有什么可以帮您？",
  greetingBody:
    "可以问订单、交期、发票或技术参数。回答会附上引用的来源；想找人工同事，随时点「转人工」。",
  suggestions: ["我的订单到哪了？", "常规交期是多久？", "发票怎么开？"],
  placeholder: "输入您的问题…",
  /**
   * What the composer says once a person owns the thread.
   *
   * The composer stays usable on purpose - the handoff notice promises the
   * customer's messages will be read, so taking the box away would break that
   * promise. What changes is that the page stops implying the AI will answer:
   * the placeholder says who will read it. Leaving "输入您的问题…" there told
   * the customer they were still talking to something that had already stopped
   * listening.
   */
  placeholderWaiting: "留言给人工同事…",
  hint: "Enter 发送 · Shift+Enter 换行",
  hintWaiting: "消息会直接进入人工队列 · Enter 发送",
  /**
   * The satisfaction ask (feature 7.10).
   *
   * Offered once the conversation has been handed to a person - the one
   * unambiguous "this interaction is over" signal this surface has, and where
   * industry practice puts the ask. A conversation the customer abandoned
   * mid-sentence is never asked about: a score from a non-interaction drags the
   * average down for a reason nobody can act on, which is what `csat.py`'s own
   * docstring says.
   */
  ratingAsk: "这次服务您还满意吗？",
  ratingThanks: "谢谢您的评价",
  ratingSkip: "不用了",
  ratingLow: "很抱歉没能帮上忙，您的反馈会交给同事跟进。",
  conversationClosed: "本次服务已结束。需要继续咨询时，请开启新会话。",
  newConversation: "开启新会话",
  send: "发送",
  sending: "发送中…",
  typing: "客服正在输入…",
  human: "转人工",
  online: "在线",
  offline: "非服务时间",
  emptyThread: "还没有对话。说一句话开始吧。",
  verifyTitle: "想查订单或物流？先核实身份",
  verifyBody: "核实之后可以直接问「我的订单到哪了」，我会把订单进度取给您看。",
  verifyOrderLabel: "订单号",
  verifyOrderPlaceholder: "如 SO-9001",
  verifyPhoneLabel: "下单时预留的手机尾号",
  verifyPhonePlaceholder: "后四位",
  verifySubmit: "核实",
  verifyBusy: "核实中",
  verified: "已通过身份核实",
  retry: "重试",
  handoffQueue: "已转人工，正在等待同事接入。您发的消息他们会看到。",
  handoffHuman: "已有同事接手这条对话，他们会看到您发的消息。",
  handoffExpired: "会话已过期。刷新页面即可继续。",
  problems: {
    network: "连接不上客服服务，请检查网络后重试。",
    timeout: "客服服务响应超时，请稍后重试。",
    server: "客服服务暂时不可用，请稍后重试。",
    session: "找不到这个客服入口，请确认链接是否正确。",
    /**
     * Capacity, not connectivity.
     *
     * A 429 here is the platform declining to start work - the tenant's monthly
     * run budget is spent, or the queue is too deep - and it used to fall
     * through to `network` because only 404 and >=500 were mapped. So a
     * customer was told to check their own connection while the platform was
     * the thing that had stopped, and "retry" could only fail again. Measured
     * 2026-09-23: it made a whole walkthrough read as "the backend is down".
     *
     * It deliberately does not say whose budget: `QUOTA_EXCEEDED` is the
     * tenant's, `QUEUE_BACKPRESSURE` is the platform's, and the customer can
     * act on neither. What they can act on is waiting, so that is what this
     * says.
     */
    busy: "现在咨询的人比较多，暂时无法立刻回答。请稍后再试，或直接点「转人工」。",
    forbidden: "这条信息需要先核实身份才能查看。请先完成上面的身份核实。",
    tooLong: "这条消息太长了，请精简后重新发送。",
  },
} as const;

function offlineBanner(hour: number): string {
  return `客服团队目前不在线。您仍然可以留言，会有同事在 ${String(hour).padStart(
    2,
    "0",
  )}:00 之后跟进。`;
}

function readStored<T>(key: string): T | null {
  try {
    const raw = window.sessionStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function visitorId(): string {
  const existing = readStored<string>(VISITOR_KEY);
  if (typeof existing === "string" && existing) return existing;
  const minted = crypto.randomUUID();
  window.sessionStorage.setItem(VISITOR_KEY, JSON.stringify(minted));
  return minted;
}

function clock(at: number): string {
  return new Date(at * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * The platform's own error codes, mapped to something a customer can act on.
 *
 * A code is a better key than a status: `429` covers both "your tenant's
 * budget is spent" and "the queue is too deep", and 403 covers both "you have
 * not verified" and "these details do not match an order". The status decides
 * the *shape* of the problem, the code decides what to say.
 */
const BY_ERROR_CODE: Record<string, string> = {
  QUOTA_EXCEEDED: "现在咨询的人比较多，暂时无法立刻回答。请稍后再试，或直接点「转人工」。",
  QUEUE_BACKPRESSURE: "现在咨询的人比较多，暂时无法立刻回答。请稍后再试，或直接点「转人工」。",
  IDENTITY_REQUIRED: "查询订单信息前需要先核实身份。请在上方填写订单号和手机尾号后重试。",
  IDENTITY_MISMATCH: "这些信息和当前已验证的账户不一致，请检查后重试，或点「转人工」。",
  CONVERSATION_CLOSED: "本次服务已结束，请开启新会话继续咨询。",
};

/**
 * Turn any failure into something a customer can act on.
 *
 * Written here rather than inherited from `lib/api.ts`, which this page cannot
 * use (see the module docstring). Its default — `String(err)` — is what put the
 * literal `Failed to fetch` in front of a customer. A transport string names a
 * browser API, not a situation, so it is never shown: anything unrecognised
 * gets the network copy, which is the most likely cause and the one that
 * carries an action.
 *
 * `code` is the API's own `error.code`, when the caller could read it. It is
 * checked first because it is the only thing that distinguishes two different
 * problems behind one status.
 */
function describeFailure(err: unknown, status?: number, code?: string): string {
  if (code && BY_ERROR_CODE[code]) return BY_ERROR_CODE[code];
  if (typeof status === "number") {
    if (status === 404) return COPY.problems.session;
    // Capacity before server error: 429 is "not now", not "broken", and the
    // two lead a customer to do different things.
    if (status === 429) return COPY.problems.busy;
    if (status === 403) return COPY.problems.forbidden;
    if (status === 413) return COPY.problems.tooLong;
    if (status >= 500) return COPY.problems.server;
  }
  if (err instanceof DOMException && err.name === "AbortError") return COPY.problems.timeout;
  if (err instanceof TypeError) return COPY.problems.network;
  const text = err instanceof Error ? err.message : "";
  if (!text || /failed to fetch|networkerror|load failed/i.test(text)) {
    return COPY.problems.network;
  }
  return text;
}

/**
 * Read a failed response's error code, then describe it.
 *
 * The body has to be read here because it is the only place the code exists,
 * and reading it consumes it - so this is called exactly once per failed
 * response, and the callers below do not read the body themselves.
 */
async function describeResponse(resp: Response): Promise<string> {
  let code = "";
  try {
    const body = await resp.json();
    code = body?.error?.code ?? "";
  } catch {
    // A body that is not JSON carries no code. Not an error: the status is
    // still enough to say something useful, which is the point of the
    // fallback chain above.
  }
  return describeFailure(null, resp.status, code);
}

export function SupportChat() {
  const tenant = useRef(
    new URLSearchParams(window.location.search).get("tenant") ??
      (import.meta.env.DEV ? "admin-demo" : ""),
  ).current;

  const [session, setSession] = useState<Session | null>(null);
  const [branding, setBranding] = useState<Branding | null>(null);
  const [supportWindow, setSupportWindow] = useState<SupportWindow | null>(null);
  const [conversation, setConversation] = useState<ConversationState>({
    owner: "ai",
    mode: "AI_ACTIVE",
  });
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [orderId, setOrderId] = useState("");
  const [phoneTail, setPhoneTail] = useState("");
  const [verifying, setVerifying] = useState(false);
  /**
   * Which operation the current `problem` came from.
   *
   * The failure banner carries one button, so that button has to re-do the
   * thing that failed. It previously always reloaded the timeline unless a
   * *send* was pending, which meant a failed identity check offered "重试" that
   * reloaded the thread and left the verification exactly as it was - a button
   * whose label promises one action and whose code performs another.
   */
  const [problemKind, setProblemKind] = useState<"load" | "send" | "verify">("load");
  /**
   * The score this conversation already has, and whether the ask is dismissed.
   *
   * `rating` comes from `/timeline`, so a reload shows the score the customer
   * gave rather than an empty survey they already answered. `ratingDismissed`
   * is local and deliberately not persisted: "not now" should not silence the
   * ask forever, but it must not re-appear on the next render either.
   */
  const [rating, setRating] = useState<number | null>(null);
  const [ratingDismissed, setRatingDismissed] = useState(false);
  const [ratingBusy, setRatingBusy] = useState(false);
  const [ratingEligible, setRatingEligible] = useState(false);
  /**
   * The text of a send that failed, so "重试" re-sends the same question. Held
   * in a ref rather than state: nothing renders from it, and putting it in
   * state would re-render the thread for a value only the click handler reads.
   */
  const pendingRetry = useRef("");
  /**
   * How many assistant turns the conversation had when this question was
   * queued.
   *
   * The wait has to end when a *new* answer arrives, and the check used to be
   * "does the timeline contain an agent turn" - which is true the moment the
   * conversation has ever been answered. So from the second question onwards
   * the poll ended on its first tick, two seconds in, and the reply did not
   * appear until the customer sent something else. Measured 2026-09-23: a
   * question answered in 13 seconds was still invisible two minutes later, and
   * the third question is what revealed it.
   */
  const answeredBaseline = useRef(0);
  const waitDeadline = useRef(0);
  const threadRef = useRef<HTMLElement | null>(null);

  const finished = conversation.owner === "closed";
  const handedOff = conversation.owner !== "ai" && !finished;
  const closed = Boolean(supportWindow && !supportWindow.open);
  const accent = branding?.primary_color ?? "#1a393d";

  const applyBranding = useCallback((next: Branding | null) => {
    // Only overwrite with something real: a `/timeline` answer that omits
    // branding must not blank out a name the page is already showing.
    if (next?.display_name) setBranding(next);
  }, []);

  const applyWindow = useCallback((next: SupportWindow | null) => {
    if (next) setSupportWindow(next);
  }, []);

  const openSession = useCallback(async (): Promise<Session> => {
    const resp = await fetch(`${API}/v1/support/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant_slug: tenant, visitor_id: visitorId() }),
    });
    if (!resp.ok) {
      // A 404 here is "unknown tenant" and is deliberately not distinguished
      // from a suspended one, so the page cannot be used to enumerate tenants.
      // The *copy* is the customer's, though: `tenant_slug` is an internal
      // parameter name and a customer has never heard of it.
      throw new Error(await describeResponse(resp));
    }
    const body = await resp.json();
    const opened: Session = {
      token: body.token,
      conversation_ref: body.conversation_ref,
      expires_at: body.expires_at,
      visitor_id: visitorId(),
      tenant,
      // `/sessions` mints an anonymous credential; only `/verify` binds an
      // account. Re-opening after expiry therefore drops verification, which
      // is why the expiry path tells the customer to confirm again.
      verified_account: null,
      branding: body.branding ?? null,
    };
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(opened));
    applyBranding(opened.branding ?? null);
    applyWindow(body.support_window ?? null);
    return opened;
  }, [tenant, applyBranding, applyWindow]);

  /**
   * Load the thread and, with it, who owns the conversation.
   *
   * Both come from one call because they are one answer: "here is what was
   * said, and here is whether this window is the thing that will answer". A
   * second request for the ownership could arrive out of order with the turns
   * and render a state that was never true.
   */
  const loadTimeline = useCallback(
    async (active: Session): Promise<Turn[]> => {
      const resp = await fetch(`${API}/v1/support/timeline`, {
        headers: { Authorization: `Bearer ${active.token}` },
      });
      if (resp.status === 401) throw new Error("EXPIRED");
      if (!resp.ok) throw new Error(await describeResponse(resp));
      const body = await resp.json();
      const items: Turn[] = body.items ?? [];
      setTurns(items);
      if (body.conversation) setConversation(body.conversation as ConversationState);
      applyBranding(body.branding ?? null);
      applyWindow(body.support_window ?? null);
      // Absent and null both mean "no score yet" - an older payload does not
      // carry the field at all, and neither case should clear a score the
      // customer just gave in this tab.
      if (body.rating != null) setRating(Number(body.rating));
      setRatingEligible(Boolean(body.rating_eligible));
      return items;
    },
    [applyBranding, applyWindow],
  );

  // Open (or resume) the session, then load the thread through the session that
  // came back. Kept as one effect because `loadTimeline` needs a token and the
  // expiry path has to re-open before it can retry: splitting them across two
  // effects is what previously left the restore branch with no branding - it
  // took `stored` and never called `openSession`, so the header fell back to the
  // hardcoded name on every reload (measured 2026-09-23, A9).
  useEffect(() => {
    let cancelled = false;
    setProblemKind("load");
    if (!tenant) {
      setProblem("客服链接缺少企业标识，请从企业提供的客服入口进入。");
      return () => { cancelled = true; };
    }
    void (async () => {
      const stored = readStored<Session>(STORAGE_KEY);
      // `stored.tenant === tenant` is not a nicety. The token names one
      // (tenant, conversation) pair, so reusing a session opened for another
      // tenant shows that tenant's conversation under this tenant's URL -
      // silently, because nothing fails. A stored session from before `tenant`
      // was recorded has `undefined` here, which is also a mismatch, and
      // opening a fresh session is the correct recovery for both.
      const usable = Boolean(
        stored &&
          stored.tenant === tenant &&
          stored.expires_at * 1000 > Date.now() &&
          stored.visitor_id === visitorId(),
      );
      try {
        // Paint the cached brand before the network answers, so a returning
        // customer never watches the header change from one name to another.
        if (usable && stored) applyBranding(stored.branding ?? null);
        const active = usable && stored ? stored : await openSession();
        if (cancelled) return;
        setSession(active);
        await loadTimeline(active);
      } catch (err) {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : "";
        if (message === "EXPIRED" && usable) {
          // The stored token aged out between the check above and the request.
          try {
            const reopened = await openSession();
            if (cancelled) return;
            setSession(reopened);
            await loadTimeline(reopened);
            return;
          } catch (inner) {
            if (!cancelled) setProblem(describeFailure(inner));
            return;
          }
        }
        if (!cancelled) setProblem(describeFailure(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [openSession, loadTimeline, applyBranding]);

  // AI answers asynchronously; human replies also arrive after the original
  // send. Keep the customer window current while either party owns it, with a
  // slower human cadence and a visibility guard so background tabs do no work.
  useEffect(() => {
    if (!session || finished || (!waiting && !handedOff)) return;
    let failures = 0;
    let inFlight = false;
    const timer = window.setInterval(() => {
      if (document.hidden || inFlight) return;
      inFlight = true;
      void (async () => {
        try {
          const items = await loadTimeline(session);
          // Counted, not "is there one": an agent turn from an earlier question
          // is not an answer to this one. A `tool` or `system` turn does not
          // count either - a card arrives *before* the answer, and a handoff
          // notice is the platform saying somebody else will answer.
          const answers = items.filter((t) => t.role === "agent").length;
          if (waiting && (answers > answeredBaseline.current || Date.now() > waitDeadline.current)) {
            setWaiting(false);
          }
          failures = 0;
        } catch (err) {
          failures += 1;
          // Not silent any more. Giving up on the first blip meant a single
          // failed poll ended the wait with no answer and no explanation, which
          // is indistinguishable from the platform having nothing to say.
          // Three in a row is a real failure, and the customer is told.
          if (failures >= 3) {
            setWaiting(false);
            setProblem(describeFailure(err));
          }
        } finally {
          inFlight = false;
        }
      })();
    }, waiting ? 2000 : 5000);
    return () => window.clearInterval(timer);
  }, [waiting, session, loadTimeline, handedOff, finished]);

  useEffect(() => {
    if (handedOff && waiting) setWaiting(false);
  }, [handedOff, waiting]);

  // Keep the newest turn in view without stealing focus from the composer.
  useEffect(() => {
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns, waiting]);

  // The browser tab's title.
  //
  // Without this the page inherits `index.html`'s `B2B AI Support · Admin`,
  // because `document.title` was only ever set by `Layout.tsx` - the operator
  // shell, which this page is deliberately outside of. So a customer's tab,
  // bookmark and history entry all read "Admin", and none of them said which
  // company they were talking to or that this was a support window. Measured
  // 2026-09-23.
  useEffect(() => {
    document.title = `${branding?.display_name || COPY.fallbackBrand} · ${COPY.pageKind}`;
  }, [branding]);

  const send = useCallback(
    async (text: string) => {
      const body = text.trim();
      if (!body || !session || busy || finished) return;
      setBusy(true);
      setProblem(null);
      setProblemKind("send");
      setDraft("");
      // Optimistic bubble, so the customer sees the message leave immediately.
      setTurns((prev) => [
        ...prev,
        { role: "customer", text: body, at: Date.now() / 1000, source: "local" },
      ]);
      try {
        const resp = await fetch(`${API}/v1/support/messages`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${session.token}`,
            // Required by the API, and it is what makes a double-tap on send a
            // single question rather than two for an agent to answer.
            "Idempotency-Key": crypto.randomUUID(),
          },
          body: JSON.stringify({ text: body }),
        });
        if (resp.status === 401) throw new Error("EXPIRED");
        if (!resp.ok) throw new Error(await describeResponse(resp));
        const result = await resp.json();
        if (result.conversation) setConversation(result.conversation as ConversationState);
        pendingRetry.current = "";
        // Read the baseline from the server's own timeline, not from local
        // state: the optimistic bubble is already in `turns`, and a stale
        // local count is what made the answer to this question look like an
        // answer to a previous one.
        const items = await loadTimeline(session);
        answeredBaseline.current = items.filter((t) => t.role === "agent").length;
        if (result.status === "waiting_for_human") {
          // No run was queued, so no reply is coming from here. Waiting would
          // put "客服正在输入…" on screen for a conversation a person owns -
          // which is the exact silence this path exists to replace with an
          // explanation.
          setWaiting(false);
          return;
        }
        waitDeadline.current = Date.now() + ANSWER_TIMEOUT_MS;
        setWaiting(true);
      } catch (err) {
        const message = err instanceof Error ? err.message : "";
        pendingRetry.current = body;
        if (message === "EXPIRED") {
          // The token is a bearer credential with an expiry; re-open silently
          // rather than showing the customer a session error they cannot act on.
          try {
            const reopened = await openSession();
            setSession(reopened);
            setProblem("会话已续期，请重新发送。");
          } catch {
            setProblem(COPY.handoffExpired);
          }
        } else {
          setProblem(describeFailure(err));
        }
      } finally {
        setBusy(false);
      }
    },
    [session, busy, finished, loadTimeline, openSession],
  );

  /**
   * Bind this session to the account that owns an order (feature 2.2/2.5).
   *
   * Without this the customer is anonymous, the read path refuses to look
   * anything up, and every order question answers "I could not verify" — so
   * the page's headline feature, the data card, is unreachable. The proof is
   * the order number plus the phone tail on file; the *provider* decides
   * whether it matches, and the platform never compares the tail itself.
   */
  const verify = useCallback(async () => {
    const order = orderId.trim();
    const tail = phoneTail.trim();
    if (!order || tail.length < 4 || !session || verifying) return;
    setVerifying(true);
    setProblem(null);
    setProblemKind("verify");
    try {
      const resp = await fetch(`${API}/v1/support/verify`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${session.token}`,
        },
        body: JSON.stringify({ order_id: order, phone_tail: tail }),
      });
      if (resp.status === 401) throw new Error("EXPIRED");
      if (!resp.ok) {
        // 403 covers unknown order, wrong tail and an unreachable provider -
        // deliberately the same response, so this cannot be used as an order
        // oracle. So say the same thing back rather than guessing which.
        throw new Error(
          resp.status === 403
            ? "无法用这些信息核实该订单，请检查订单号与手机尾号后重试。"
            : await describeResponse(resp),
        );
      }
      const body = await resp.json();
      const renewed: Session = {
        ...session,
        token: body.token,
        expires_at: body.expires_at,
        verified_account: body.verified_account ?? null,
      };
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(renewed));
      setSession(renewed);
      setOrderId("");
      setPhoneTail("");
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "验证失败");
    } finally {
      setVerifying(false);
    }
  }, [orderId, phoneTail, session, verifying]);

  /**
   * Re-run whatever failed: the verification, the send, or the load.
   *
   * Declared after `verify` and `send` because it depends on both - a
   * `useCallback` dependency array is evaluated during render, so referencing
   * a `const` declared further down would throw before the component ever
   * mounted.
   */
  const retry = useCallback(() => {
    setProblem(null);
    if (problemKind === "verify") {
      // `verify()` reads the order number and phone tail from state, and the
      // customer has not cleared them, so this repeats the same attempt rather
      // than asking them to type it in again.
      void verify();
      return;
    }
    const text = pendingRetry.current;
    if (text) {
      void send(text);
      return;
    }
    void (async () => {
      try {
        const active = session ?? (await openSession());
        setSession(active);
        await loadTimeline(active);
      } catch (err) {
        setProblem(describeFailure(err));
      }
    })();
  }, [problemKind, session, openSession, loadTimeline, send, verify]);

  /**
   * Send the satisfaction score (feature 7.10).
   *
   * The score is kept locally only after the server has accepted it: showing
   * the stars as filled before the write lands would tell the customer their
   * opinion was recorded when it may not have been, and this is the one control
   * on the page whose whole value is that the answer is kept.
   */
  const rate = useCallback(
    async (score: number) => {
      if (!session || ratingBusy) return;
      setRatingBusy(true);
      setProblem(null);
      try {
        const resp = await fetch(`${API}/v1/support/rating`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${session.token}`,
            "Idempotency-Key": crypto.randomUUID(),
          },
          body: JSON.stringify({ score }),
        });
        if (resp.status === 401) throw new Error("EXPIRED");
        if (!resp.ok) throw new Error(await describeResponse(resp));
        const body = await resp.json();
        setRating(Number(body.score));
      } catch (err) {
        setProblemKind("send");
        setProblem(describeFailure(err));
      } finally {
        setRatingBusy(false);
      }
    },
    [session, ratingBusy],
  );

  const startNewConversation = useCallback(async () => {
    window.sessionStorage.removeItem(STORAGE_KEY);
    window.sessionStorage.removeItem(VISITOR_KEY);
    setSession(null);
    setTurns([]);
    setRating(null);
    setRatingEligible(false);
    setRatingDismissed(false);
    setConversation({ owner: "ai", mode: "AI_ACTIVE" });
    setDraft("");
    setProblem(null);
    try {
      const active = await openSession();
      setSession(active);
      await loadTimeline(active);
    } catch (err) {
      setProblem(describeFailure(err));
    }
  }, [openSession, loadTimeline]);

  const brandName = branding?.display_name || COPY.fallbackBrand;

  return (
    <div className="support-shell" style={{ ["--support-accent" as string]: accent }}>
      <header className="support-head">
        {branding?.logo_url ? (
          <img className="support-logo" src={branding.logo_url} alt="" />
        ) : null}
        <div className="support-head-text">
          {/*
            One `<h1>`, and it names the page as well as the tenant. It used to
            be the brand name alone, which meant a screen reader announced the
            document's only heading as "Acme & Co" and said nothing about what
            the page was for.
          */}
          <h1 className="support-title">
            {/* The brand span is dropped when it would repeat the kind.
                `fallbackBrand` and `pageKind` are both 在线客服, so an unknown
                tenant rendered the heading as "在线客服 在线客服" - measured
                2026-09-23 on `?tenant=nope-nope`. The kind always renders, so
                the heading still names the page for a screen reader. */}
            {brandName !== COPY.pageKind ? (
              <span className="support-title-brand">{brandName}</span>
            ) : null}
            <span className="support-title-kind">{COPY.pageKind}</span>
          </h1>
          <p className={`support-status${closed || (!session && problem) ? " off" : ""}`}>
            <span className="support-dot" aria-hidden="true" />
            {!session && problem ? "暂不可用" : !session ? "连接中" : closed ? COPY.offline : COPY.online}
          </p>
        </div>
      </header>

      {closed && supportWindow ? (
        <p className="support-banner" role="status">
          {offlineBanner(supportWindow.opens_at_hour)}
        </p>
      ) : null}

      {/*
        `role="log"` + `aria-live="polite"` so a screen-reader user is told when
        a reply arrives. Without it a new turn appeared silently and the only way
        to find it was to navigate the thread again. `aria-relevant="additions"`
        because the customer's own optimistic bubble is also an addition, and
        re-announcing the whole thread on every poll would be worse than saying
        nothing.
      */}
      <main
        className="support-thread"
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label="对话记录"
        ref={threadRef}
      >
        {turns.length === 0 && !problem && !waiting ? (
          <div className="support-empty">
            <h2 className="support-empty-title">{COPY.greeting}</h2>
            <p className="support-empty-body">{COPY.greetingBody}</p>
            <div className="support-chips">
              {COPY.suggestions.map((s) => (
                <button
                  key={s}
                  type="button"
                  className="support-chip"
                  onClick={() => void send(s)}
                  disabled={!session || busy}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {turns.map((turn, index) => {
          const key = `${turn.at}-${index}`;
          // A tool turn carries the receipt, and the receipt is data, not a
          // sentence: printing `text` here is what put raw JSON in a chat
          // bubble. No card means no bubble - the answer's prose already says
          // what the receipt says, in words.
          if (turn.role === "tool") {
            return turn.card ? <ToolCard key={key} card={turn.card} /> : null;
          }
          if (turn.role === "system") {
            return (
              <div key={key} className="support-system">
                {turn.text}
              </div>
            );
          }
          return (
            <div
              key={key}
              className={`support-bubble ${turn.role === "customer" ? "is-customer" : "is-agent"}`}
            >
              {turn.text}
              <time className="support-time" dateTime={new Date(turn.at * 1000).toISOString()}>
                {clock(turn.at)}
              </time>
            </div>
          );
        })}

        {waiting ? (
          <div className="support-typing" role="status">
            <span className="support-typing-dots" aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
            {COPY.typing}
          </div>
        ) : null}
      </main>

      {/* The identity step, and the reason it is on this page at all: until an
          account is bound, an order question can only be answered with "I
          could not verify", and the data card never appears. Shown only while
          the session is anonymous, so a verified customer is never nagged.

          Hidden once a person owns the thread. Verifying then does nothing:
          the AI will not answer in this conversation again, so the form would
          accept the customer's order number, report "已通过身份核实", and
          deliver nothing - a control that looks like it works. Measured
          2026-09-23, and it is worse than no form: the green success state is
          a promise the page cannot keep. */}
      {session && !session.verified_account && !handedOff && !finished ? (
        <form
          className="support-verify"
          onSubmit={(event) => {
            event.preventDefault();
            void verify();
          }}
        >
          <h2 className="support-verify-title">{COPY.verifyTitle}</h2>
          <p className="support-verify-lead">{COPY.verifyBody}</p>
          <div className="support-verify-row">
            <div className="support-field">
              {/*
                A real `<label>`, not a placeholder. A placeholder disappears the
                moment the customer types, which leaves someone using a screen
                reader - or anyone relying on the visible cue - with two
                unlabelled boxes.
              */}
              <label className="support-label" htmlFor="support-order-id">
                {COPY.verifyOrderLabel}
              </label>
              <input
                id="support-order-id"
                value={orderId}
                onChange={(event) => setOrderId(event.target.value)}
                placeholder={COPY.verifyOrderPlaceholder}
                maxLength={63}
                autoComplete="off"
                disabled={verifying}
              />
            </div>
            <div className="support-field">
              <label className="support-label" htmlFor="support-phone-tail">
                {COPY.verifyPhoneLabel}
              </label>
              <input
                id="support-phone-tail"
                value={phoneTail}
                onChange={(event) => setPhoneTail(event.target.value)}
                placeholder={COPY.verifyPhonePlaceholder}
                maxLength={8}
                inputMode="numeric"
                autoComplete="off"
                disabled={verifying}
              />
            </div>
            <button
              type="submit"
              className="support-verify-submit"
              disabled={verifying || !orderId.trim() || phoneTail.trim().length < 4}
            >
              {verifying ? COPY.verifyBusy : COPY.verifySubmit}
            </button>
          </div>
        </form>
      ) : null}

      {/*
        One status area, not two stacked chips.
        
        "已通过身份核实" and "已转人工…" used to render as two independent blocks
        with a gap each, so a customer read them as unrelated and had to work
        out for themselves which one answered "is anyone going to reply?".
        They answer different questions - what the platform knows about you, and
        who owns the conversation - so they belong in one place, in that order,
        with the ownership line given the emphasis because it is the one that
        explains a silence.
      */}
      <div className="support-status-stack">
        {/*
          The receipt's account is the ERP's own slug ("acme"), which means
          nothing to the customer whose order it is. What they need to know is
          that the gate is satisfied.
        */}
        {session?.verified_account ? (
          <p className="support-verified" role="status">
            {COPY.verified}
          </p>
        ) : null}

        {/*
          Who will answer. Persistent rather than a one-off message, because it
          stays true for the rest of the conversation and it is the only thing
          on this page that explains a silence.
        */}
        {handedOff ? (
          <p className="support-handoff" role="status">
            {conversation.owner === "expired"
              ? COPY.handoffExpired
              : conversation.owner === "human"
                ? COPY.handoffHuman
                : COPY.handoffQueue}
          </p>
        ) : null}

        {finished ? (
          <div className="support-ended" role="status">
            <span>{COPY.conversationClosed}</span>
            <button type="button" onClick={() => void startNewConversation()}>{COPY.newConversation}</button>
          </div>
        ) : null}

        {/*
          The satisfaction ask, after the ownership notice.

          It sits here rather than in a floating toast because it is about the
          conversation, and it is offered only once a person owns the thread -
          the interaction is over, which is when the question is answerable.
          Once answered it stays visible as a receipt: the customer can see the
          platform kept their score, and a re-tap replaces it rather than
          adding a second opinion.
        */}
        {finished && ratingEligible && rating !== null ? (
          <p className="support-rating is-done" role="status">
            <span className="support-rating-thanks">{COPY.ratingThanks}</span>
            <span className="support-rating-stars" aria-label={`${rating} / 5`}>
              {[1, 2, 3, 4, 5].map((n) => (
                <span key={n} aria-hidden className={n <= rating ? "is-on" : ""}>
                  ★
                </span>
              ))}
            </span>
            {rating <= 2 ? <span className="support-rating-low">{COPY.ratingLow}</span> : null}
          </p>
        ) : null}

        {finished && ratingEligible && rating === null && !ratingDismissed ? (
          <div className="support-rating" role="group" aria-label={COPY.ratingAsk}>
            <span className="support-rating-ask">{COPY.ratingAsk}</span>
            <div className="support-rating-buttons">
              {[1, 2, 3, 4, 5].map((n) => (
                <button
                  key={n}
                  type="button"
                  className="support-rating-star"
                  // The accessible name is the score, not the glyph: a screen
                  // reader announcing five identical "★" buttons tells the
                  // customer nothing about which is which.
                  aria-label={`${n} 分`}
                  disabled={ratingBusy}
                  onClick={() => void rate(n)}
                >
                  <span aria-hidden>★</span>
                  <span className="visually-hidden">{n}</span>
                </button>
              ))}
              <button
                type="button"
                className="support-rating-skip"
                disabled={ratingBusy}
                onClick={() => setRatingDismissed(true)}
              >
                {COPY.ratingSkip}
              </button>
            </div>
          </div>
        ) : null}
      </div>

      {problem ? (
        <p className="support-problem" role="alert">
          <span>{problem}</span>
          {/*
            The old failure state was a line of red text and a disabled
            composer, so the only recovery was for the customer to guess that a
            refresh might help.
          */}
          <button type="button" className="support-problem-retry" onClick={retry}>
            {COPY.retry}
          </button>
        </p>
      ) : null}

      {!finished ? <form
        className="support-composer"
        onSubmit={(event) => {
          event.preventDefault();
          void send(draft);
        }}
      >
        <label className="visually-hidden" htmlFor="support-composer-input">
          输入您的问题
        </label>
        {/*
          A `<textarea>`, not `<input>`: an `<input>` cannot hold a newline, so a
          customer pasting a stack trace, an address or a set of process
          parameters had it silently collapsed onto one line. Enter still sends
          and Shift+Enter inserts a newline, which is what every chat window
          does.
        */}
        <textarea
          id="support-composer-input"
          className="support-input"
          rows={1}
          value={draft}
          placeholder={handedOff ? COPY.placeholderWaiting : COPY.placeholder}
          maxLength={4000}
          disabled={!session || busy}
          onChange={(event) => {
            setDraft(event.target.value);
            const el = event.target;
            // Grow to fit, up to the CSS `max-height`, which is what keeps a
            // long paste from pushing the thread off screen.
            el.style.height = "auto";
            el.style.height = `${el.scrollHeight}px`;
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void send(draft);
            }
          }}
        />
        {/*
          Talk to a human. It sends the customer's own words rather than minting
          a handoff locally: the platform already classifies every phrasing of
          this as `human_required` -> `handoff`, so the request travels the path
          that actually transfers the conversation. The internal verification
          panel does the opposite - its button appends "you are in the queue" and
          never calls the API - and copying that would have shipped a promise
          nothing keeps.

          Hidden once the conversation is already with a person: the transfer is
          one-way, so a second request is a button that cannot change anything.
        */}
        {!handedOff ? (
          <button
            type="button"
            className="support-human"
            onClick={() => void send(COPY.human)}
            disabled={!session || busy}
          >
            {COPY.human}
          </button>
        ) : null}
        <button
          type="submit"
          className="support-send"
          disabled={!session || busy || !draft.trim()}
        >
          {busy ? COPY.sending : COPY.send}
        </button>
      </form> : null}
      {!finished ? <p className="support-foot-note">{handedOff ? COPY.hintWaiting : COPY.hint}</p> : null}
    </div>
  );
}
