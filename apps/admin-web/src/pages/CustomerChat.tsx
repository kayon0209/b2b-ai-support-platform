/**
 * Customer-facing chat — the surface this product has never had.
 *
 * Follows the pattern every shipped support product converges on, because
 * it is what customers already know how to use:
 *
 *   - a launcher bubble pinned bottom-right (56x56 — Apple's tap-target
 *     guidance, and the size Intercom/Zendesk/Drift all use);
 *   - a 380px tray on desktop, full-screen on mobile. The tray width is not
 *     arbitrary: a 390px phone viewport cannot hold it, so below that the
 *     panel takes the screen instead of shrinking into uselessness;
 *   - height in `dvh`, not `vh`, so a mobile soft keyboard does not push the
 *     composer off screen.
 *
 * It is a real ARIA dialog: focus moves in on open, Escape closes, and focus
 * returns to the launcher on close.
 *
 * The scenario switcher is a review affordance, not a product feature — it
 * exists so every state can be inspected without waiting for a real failure.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiGet, apiPost, isUnauthorized } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { DataCard } from "../components/DataCard";
import { useLang } from "../lib/i18n";
import type { TenantBranding } from "../lib/types";

type Role = "customer" | "agent" | "system" | "tool";
type SendState = "sending" | "sent" | "failed";

/**
 * How long the chat waits for a reply before telling the customer.
 *
 * Configurable so the wait can be shortened when testing it: the behaviour
 * under test is what happens *when time passes*, so the one thing a test
 * cannot do is pretend the time passed. The default stays at 45s, which is
 * longer than the platform's own 30s model timeout - the point is to outlast
 * a slow answer, not to race it.
 */
const WAIT_TIMEOUT_MS = Number(import.meta.env.VITE_CHAT_WAIT_TIMEOUT_MS) || 45_000;


interface Citation {
  label: string;
  sourceUri: string;
  excerpt: string;
}

interface Message {
  id: string;
  role: Role;
  text: string;
  at: number;
  state?: SendState;
  citations?: Citation[];
  abstained?: boolean;
}

type Scenario = "answer" | "abstain" | "error" | "offline";
type View = "collapsed" | "tray" | "expanded";

const COPY = {
  en: {
    open: "Open chat",
    close: "Close chat",
    expand: "Expand",
    collapse: "Collapse",
    online: "Online",
    offline: "Offline",
    offlineBanner:
      "Our team is offline right now. Leave a message and we will reply by email.",
    emptyTitle: "How can we help?",
    emptyBody:
      "Ask about orders, lead times, invoices or technical specs. Answers come with the sources they were drawn from.",
    suggestions: [
      "Where is my order?",
      "What is the lead time for 4-layer boards?",
      "How do I reset my password?",
    ],
    placeholder: "Type your question…",
    send: "Send",
    sending: "Sending…",
    failedRetry: "Not delivered",
    internal: "Internal",
    internalTitle:
      "An internal verification surface, not the customer experience. The customer channel is Chatwoot.",
    waitTimeout:
      "No one has picked this up yet. Your question is saved - ask for a human and an agent will see it.",
    problemAuth:
      "This chat needs an operator access token. Open the console, paste one, then reload.",
    retry: "Retry",
    handoff: "Talk to a human",
    handoffDone: "You are in the queue for a human agent.",
    handoffWait: "A colleague will join shortly. Typical wait is under 5 minutes.",
    sources: "{n} sources",
    hideSources: "Hide sources",
    showSources: "Show sources",
    typing: "Assistant is typing…",
    abstainedTitle: "I could not answer that from our knowledge base.",
    abstainedBody:
      "Rather than guess, I have handed this to a human colleague who can check your account.",
    errorMessage: "We could not reach the support service.",
    poweredBy: "Powered by AI · answers cite their sources",
    scenario: "Review states",
    reset: "Clear",
    you: "You",
    assistant: "Assistant",
  },
  zh: {
    open: "打开客服对话",
    close: "关闭对话",
    expand: "放大",
    collapse: "还原",
    online: "在线",
    offline: "离线",
    offlineBanner: "客服团队目前不在线。请留言，我们会通过邮件回复您。",
    emptyTitle: "有什么可以帮您？",
    emptyBody: "可以询问订单、交期、发票或技术参数。回答会附上引用的来源。",
    suggestions: ["我的订单到哪了？", "四层板的交期是多久？", "如何重置密码？"],
    placeholder: "输入您的问题…",
    send: "发送",
    sending: "发送中…",
    failedRetry: "未送达",
    internal: "内部验证面",
    internalTitle: "内部验证用界面，不是客户侧体验。客户渠道是 Chatwoot。",
    waitTimeout:
      "暂时还没有人应答。你的问题已保存，点「转人工」坐席会看到它。",
    problemAuth: "此界面需要运营访问令牌：请先在后台粘贴令牌，然后刷新。",
    retry: "重试",
    handoff: "转人工",
    handoffDone: "您已进入人工客服队列。",
    handoffWait: "同事很快会接入，通常等待不超过 5 分钟。",
    sources: "{n} 个来源",
    hideSources: "收起来源",
    showSources: "查看来源",
    typing: "客服正在输入…",
    abstainedTitle: "知识库中没有能回答这个问题的依据。",
    abstainedBody: "为避免猜测，我已将问题转给可以查询您账户的人工同事。",
    errorMessage: "无法连接客服服务。",
    poweredBy: "由 AI 提供 · 回答附引用来源",
    scenario: "查看状态",
    reset: "清空",
    you: "我",
    assistant: "客服助手",
  },
} as const;

type Copy = (typeof COPY)["en"] | (typeof COPY)["zh"];

function clock(at: number, lang: "en" | "zh"): string {
  return new Date(at).toLocaleTimeString(lang === "zh" ? "zh-CN" : "en-GB", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

let seq = 0;
const nextId = () => `m${++seq}-${Date.now()}`;

export function CustomerChat() {
  const { lang } = useLang();
  const c = COPY[lang];
  const [view, setView] = useState<View>("collapsed");
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [typing, setTyping] = useState(false);
  const [scenario, setScenario] = useState<Scenario>("answer");
  const [handedOff, setHandedOff] = useState(false);
  // A visible reason for a failure. Without it the only feedback was a bubble
  // reading "Not delivered", which cannot tell "you are not signed in" apart
  // from "the network is down" - and the timeline loader swallowed its errors
  // entirely, so a 401 looked like an empty conversation forever.
  const [problem, setProblem] = useState<string | null>(null);
  const [waitTimedOut, setWaitTimedOut] = useState(false);
  const waitingRef = useRef(false);
  const waitStartedRef = useRef(0);
  const endRef = useRef<HTMLDivElement | null>(null);
  const launcherRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  // `isUnauthorized` first: it is the one failure with a remedy. This
  // surface authenticates with an operator token, so a 401 is not transient
  // and retrying will never help - the customer needs to be told that.
  const describeFailure = (err: unknown): string =>
    isUnauthorized(err) ? c.problemAuth : err instanceof Error ? err.message : String(err);

  const offlineNow = scenario === "offline";
  const open = view !== "collapsed";

  // The tenant's own branding, not a constant. The branding screen says it
  // configures "how this tenant appears to customers", and this is the one
  // customer-facing surface - so a hardcoded name meant that setting had no
  // effect anywhere it mattered. The literal stays as the fallback for a
  // tenant that has not configured anything.
  const [branding, setBranding] = useState<TenantBranding | null>(null);
  useEffect(() => {
    let active = true;
    void apiGet<{ branding: TenantBranding }>("/v1/tenant/branding")
      .then((res) => {
        if (active) setBranding(res.branding);
      })
      .catch(() => {
        // A chat that cannot read branding still has to work; the fallback
        // name is not worth an error banner in front of a customer.
      });
    return () => {
      active = false;
    };
  }, []);

  const brand = useMemo(
    () => ({
      name: branding?.display_name || "Acme Electronics",
      color: branding?.primary_color || "#2f6feb",
    }),
    [branding],
  );

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, typing, view]);

  // An agent turn means the queued run answered; stop polling.
  useEffect(() => {
    if (typing && messages.some((m) => m.role === "agent")) {
      setTyping(false);
      waitingRef.current = false;
      setWaitTimedOut(false);
    }
  }, [messages, typing]);

  // Focus goes into the composer on open and back to the launcher on close —
  // otherwise a keyboard user is left tabbing through the whole host page.
  useEffect(() => {
    if (open) inputRef.current?.focus();
    else launcherRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setView("collapsed");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  function push(msg: Omit<Message, "id" | "at">): Message {
    const full: Message = { ...msg, id: nextId(), at: Date.now() };
    setMessages((prev) => [...prev, full]);
    return full;
  }

  const [conversationRef] = useState<string>(() => {
    // A conversation is identified by a client-generated UUID: the platform
    // keys runs on `conversation_ref` and never creates the conversation
    // itself, so the caller owns the identity. Persisted so a reload keeps
    // the same thread.
    const KEY = "b2b_conversation_ref";
    const saved = localStorage.getItem(KEY);
    if (saved) return saved;
    const fresh = (crypto.randomUUID?.() ?? `conv-${Date.now()}`).toString();
    localStorage.setItem(KEY, fresh);
    return fresh;
  });

  const loadTimeline = useCallback(async () => {
    try {
      const res = await apiGet<{ items: { role: string; text: string; at: number }[] }>(
        `/v1/customer/conversations/${conversationRef}/timeline`,
      );
      // The same question can be written twice: once when the customer's
      // message is persisted, and once by the worker's own memory pass.
      // Showing it twice reads like a duplicate send, so collapse on
      // identical role + text.
      const seen = new Set<string>();
      const rows = res.items.filter((t) => {
        const key = `${t.role}:${t.text}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
      setMessages(
        rows.map((t) => ({
          id: `s-${t.at}-${t.role}-${t.text.length}`,
          // A tool receipt is not the assistant speaking. Labelling it "agent"
          // would put the platform's voice on raw system data.
          role:
            t.role === "customer" ? "customer" : t.role === "tool" ? "tool" : "agent",
          text: t.text,
          at: t.at * 1000,
          state: "sent" as SendState,
        })),
      );
    } catch (err) {
      // A conversation with no turns yet is not an error - but a *failure* is,
      // and swallowing it here meant a 401 rendered as a permanently empty
      // chat, indistinguishable from a new conversation.
      setProblem(describeFailure(err));
    }
  }, [conversationRef]);

  useEffect(() => {
    void loadTimeline();
  }, [loadTimeline]);

  // Poll until the worker has produced a reply. The run is queued
  // asynchronously, so "sent" and "answered" are genuinely separate moments.
  useEffect(() => {
    if (!waitingRef.current) return;
    let attempt = 0;
    let timer = 0;

    const tick = () => {
      // Give up rather than spin forever. The platform can legitimately never
      // answer: shadow mode withholds the send on purpose, the worker may not
      // be running, and a failed run produces no agent turn at all. Without
      // this the customer watched "typing..." indefinitely while the page
      // polled every two seconds for as long as the tab stayed open.
      if (Date.now() - waitStartedRef.current > WAIT_TIMEOUT_MS) {
        waitingRef.current = false;
        setTyping(false);
        setWaitTimedOut(true);
        return;
      }
      void loadTimeline();
      attempt += 1;
      // Back off, so a long wait costs less than a fast one.
      timer = window.setTimeout(tick, Math.min(2000 * 2 ** Math.min(attempt, 3), 10_000));
    };

    timer = window.setTimeout(tick, 2000);
    return () => window.clearTimeout(timer);
  }, [typing, loadTimeline]);

  function send(text: string) {
    const body = text.trim();
    if (!body) return;

    push({ role: "customer", text: body, state: "sending" });
    setDraft("");
    setProblem(null);
    setWaitTimedOut(false);
    setTyping(true);
    waitingRef.current = true;
    waitStartedRef.current = Date.now();

    void (async () => {
      try {
        // Two steps, both idempotent. First persist the question so the
        // platform has its own copy; then queue the run, pointing it at
        // that turn. The orchestrator reads the question back by id, and
        // for platform-originated turns that id resolves locally rather
        // than through Chatwoot.
        const saved = await apiPost<{ turn_id: string }>(
          `/v1/customer/conversations/${conversationRef}/messages`,
          { text: body },
          newIdempotencyKey(),
        );
        await apiPost(
          `/v1/conversations/${conversationRef}/agent-runs`,
          { trigger_message_ref: saved.turn_id, mode: "customer_reply" },
          newIdempotencyKey(),
        );
        await loadTimeline();
      } catch (err) {
        setTyping(false);
        waitingRef.current = false;
        setProblem(describeFailure(err));
        // Mark the optimistic bubble so the customer sees it did not go out
        // and can retry, rather than assuming it was received.
        setMessages((prev) =>
          prev.map((m, idx) =>
            idx === prev.length - 1 ? { ...m, state: "failed" as SendState } : m,
          ),
        );
      }
    })();
  }

  function askForHuman() {
    push({ role: "system", text: c.handoffDone });
    setHandedOff(true);
  }

  return (
    <div
      className="cw-root"
      style={{ ["--cw-accent" as string]: brand.color }}
      data-view={view}
    >
      {!open ? (
        <button
          ref={launcherRef}
          className="cw-launcher"
          onClick={() => setView("tray")}
          aria-label={c.open}
          aria-expanded={false}
        >
          <svg viewBox="0 0 24 24" width="26" height="26" aria-hidden>
            <path
              d="M4 5h16v11H8l-4 4V5z"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      ) : (
        <div
          ref={panelRef}
          className="cw-panel"
          role="dialog"
          aria-modal={view === "expanded"}
          aria-label={brand.name}
        >
          <header className="cw-head">
            <div className="cw-mark">{(brand.name || "A").slice(0, 1)}</div>
            <div className="cw-title">
              <div className="cw-name">{brand.name}</div>
              <div className="cw-status">
                <span className={`cw-dot${offlineNow ? " off" : ""}`} aria-hidden />
                {offlineNow ? c.offline : c.online}
              </div>
            </div>
            {/*
              ADR 0010: this panel is an internal verification surface, not the
              customer experience - the customer surface is Chatwoot. Saying so
              on the surface itself is what stops it being shipped as one, since
              nothing else about it (a real brand, a working send) would.
            */}
            <span className="cw-internal" title={c.internalTitle}>
              {c.internal}
            </span>
            <div className="cw-actions">
              <button
                className="cw-icon"
                onClick={() => setView(view === "expanded" ? "tray" : "expanded")}
                aria-label={view === "expanded" ? c.collapse : c.expand}
                title={view === "expanded" ? c.collapse : c.expand}
              >
                {view === "expanded" ? "⤡" : "⤢"}
              </button>
              <button
                className="cw-icon"
                onClick={() => setView("collapsed")}
                aria-label={c.close}
                title={c.close}
              >
                ✕
              </button>
            </div>
          </header>

          {offlineNow ? (
            <div className="cw-banner" role="status">
              {c.offlineBanner}
            </div>
          ) : null}

          <div className="cw-body">
            {messages.length === 0 && !typing ? (
              <div className="cw-empty">
                <h2>{c.emptyTitle}</h2>
                <p>{c.emptyBody}</p>
                <div className="cw-chips">
                  {c.suggestions.map((s) => (
                    <button key={s} className="cw-chip" onClick={() => send(s)}>
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}

            {messages.map((m) => (
              <div key={m.id} className={`cw-row ${m.role}`}>
                {m.role === "system" ? (
                  <div className="cw-system">{m.text}</div>
                ) : m.role === "tool" ? (
                  <div className="cw-bubble cw-tool">
                    <DataCard text={m.text} />
                  </div>
                ) : (
                  <div className="cw-bubble">
                    <div className="cw-who">
                      {m.role === "customer" ? c.you : c.assistant}
                    </div>
                    <div className="cw-text">{m.text}</div>
                    {m.abstained ? <p className="cw-abstain">{c.abstainedBody}</p> : null}
                    {m.citations && m.citations.length > 0 ? (
                      <Citations items={m.citations} copy={c} />
                    ) : null}
                    <div className="cw-meta">
                      <span>{clock(m.at, lang)}</span>
                      {m.role === "customer" && m.state === "sending" ? (
                        <span>{c.sending}</span>
                      ) : null}
                      {m.role === "customer" && m.state === "failed" ? (
                        <span className="cw-failed">
                          {c.failedRetry}
                          <button className="cw-retry" onClick={() => send(m.text)}>
                            {c.retry}
                          </button>
                        </span>
                      ) : null}
                    </div>
                  </div>
                )}
              </div>
            ))}

            {typing ? (
              <div className="cw-row agent">
                <div className="cw-bubble">
                  <div className="cw-who">{c.assistant}</div>
                  <div className="cw-typing" aria-label={c.typing}>
                    <i />
                    <i />
                    <i />
                  </div>
                </div>
              </div>
            ) : null}

            {problem || (scenario === "error" && messages.some((m) => m.state === "failed")) ? (
              <div className="cw-row">
                <div className="cw-error" role="alert">
                  {problem ?? c.errorMessage}
                </div>
              </div>
            ) : null}

            {waitTimedOut ? (
              <div className="cw-row">
                <div className="cw-system" role="status">
                  {c.waitTimeout}
                </div>
              </div>
            ) : null}

            {handedOff ? (
              <div className="cw-row">
                <div className="cw-system">{c.handoffWait}</div>
              </div>
            ) : null}

            <div ref={endRef} />
          </div>

          {view === "expanded" ? (
            <div className="cw-review">
              <span className="cw-review-label">{c.scenario}</span>
              {(
                [
                  ["answer", "回答 + 引用"],
                  ["abstain", "转人工"],
                  ["error", "发送失败"],
                  ["offline", "离线"],
                ] as const
              ).map(([key, label]) => (
                <button
                  key={key}
                  className={`cw-review-btn${scenario === key ? " on" : ""}`}
                  onClick={() => setScenario(key)}
                >
                  {label}
                </button>
              ))}
              <button
                className="cw-review-btn"
                onClick={() => {
                  setMessages([]);
                  setHandedOff(false);
                }}
              >
                {c.reset}
              </button>
            </div>
          ) : null}

          <footer className="cw-foot">
            {!handedOff ? (
              <button className="cw-human" onClick={askForHuman}>
                {c.handoff}
              </button>
            ) : null}
            <div className="cw-composer">
              <textarea
                ref={inputRef}
                className="cw-input"
                rows={1}
                value={draft}
                placeholder={c.placeholder}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send(draft);
                  }
                }}
              />
              <button
                className="cw-send"
                disabled={!draft.trim()}
                onClick={() => send(draft)}
                aria-label={c.send}
              >
                {c.send}
              </button>
            </div>
            <div className="cw-foot-note">{c.poweredBy}</div>
          </footer>
        </div>
      )}
    </div>
  );
}

function Citations({ items, copy }: { items: Citation[]; copy: Copy }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="cw-cites">
      <button className="cw-cites-toggle" onClick={() => setOpen(!open)}>
        {open ? copy.hideSources : copy.showSources} ·{" "}
        {copy.sources.replace("{n}", String(items.length))}
      </button>
      {open ? (
        <ul>
          {items.map((ci) => (
            <li key={ci.sourceUri}>
              <div className="cw-cite-label">{ci.label}</div>
              <blockquote>{ci.excerpt}</blockquote>
              <code>{ci.sourceUri}</code>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
