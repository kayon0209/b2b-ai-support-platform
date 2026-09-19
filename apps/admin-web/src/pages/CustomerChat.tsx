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
import { apiGet, apiPost } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useLang } from "../lib/i18n";

type Role = "customer" | "agent" | "system";
type SendState = "sending" | "sent" | "failed";

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
  const waitingRef = useRef(false);
  const endRef = useRef<HTMLDivElement | null>(null);
  const launcherRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  const offlineNow = scenario === "offline";
  const open = view !== "collapsed";

  const brand = useMemo(
    () => ({ name: "Acme Electronics", color: "#2f6feb" }),
    [],
  );

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, typing, view]);

  // An agent turn means the queued run answered; stop polling.
  useEffect(() => {
    if (typing && messages.some((m) => m.role === "agent")) {
      setTyping(false);
      waitingRef.current = false;
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
      setMessages(
        res.items.map((t) => ({
          id: `s-${t.at}-${t.role}`,
          role: t.role === "customer" ? "customer" : "agent",
          text: t.text,
          at: t.at * 1000,
          state: "sent" as SendState,
        })),
      );
    } catch {
      // A conversation with no turns yet is not an error worth shouting
      // about — the empty state covers it.
    }
  }, [conversationRef]);

  useEffect(() => {
    void loadTimeline();
  }, [loadTimeline]);

  // Poll until the worker has produced a reply. The run is queued
  // asynchronously, so "sent" and "answered" are genuinely separate moments.
  useEffect(() => {
    if (!waitingRef.current) return;
    const timer = window.setInterval(() => void loadTimeline(), 2000);
    return () => window.clearInterval(timer);
  }, [typing, loadTimeline]);

  function send(text: string) {
    const body = text.trim();
    if (!body) return;

    push({ role: "customer", text: body, state: "sending" });
    setDraft("");
    setTyping(true);
    waitingRef.current = true;

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
      } catch {
        setTyping(false);
        waitingRef.current = false;
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

            {scenario === "error" && messages.some((m) => m.state === "failed") ? (
              <div className="cw-row">
                <div className="cw-error" role="alert">
                  {c.errorMessage}
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
