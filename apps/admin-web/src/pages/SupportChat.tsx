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
 * The visitor id is an opaque handle in localStorage, not an identity: it only
 * lets a returning browser resume the same conversation. Authorization comes
 * from the token, and the token names no role, so this page cannot reach any
 * operator endpoint even if a route were mis-wired.
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

type Branding = {
  display_name: string;
  logo_url: string | null;
  primary_color: string | null;
  support_email: string | null;
};

type Session = {
  token: string;
  conversation_ref: string;
  expires_at: number;
  visitor_id: string;
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

function readStored<T>(key: string): T | null {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function visitorId(): string {
  const existing = readStored<string>(VISITOR_KEY);
  if (typeof existing === "string" && existing) return existing;
  const minted = crypto.randomUUID();
  window.localStorage.setItem(VISITOR_KEY, JSON.stringify(minted));
  return minted;
}

export function SupportChat() {
  const tenant = useRef(
    new URLSearchParams(window.location.search).get("tenant") ?? "admin-demo",
  ).current;

  const [session, setSession] = useState<Session | null>(null);
  const [branding, setBranding] = useState<Branding | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const waitDeadline = useRef(0);

  const openSession = useCallback(async (): Promise<Session> => {
    const resp = await fetch(`${API}/v1/support/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant_slug: tenant, visitor_id: visitorId() }),
    });
    if (!resp.ok) {
      // A 404 here is "unknown tenant" and is deliberately not distinguished
      // from a suspended one, so the page cannot be used to enumerate tenants.
      throw new Error(
        resp.status === 404 ? "找不到该租户（tenant_slug）" : `无法开启会话（HTTP ${resp.status}）`,
      );
    }
    const body = await resp.json();
    const opened: Session = {
      token: body.token,
      conversation_ref: body.conversation_ref,
      expires_at: body.expires_at,
      visitor_id: visitorId(),
    };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(opened));
    setBranding(body.branding ?? null);
    return opened;
  }, [tenant]);

  const loadTimeline = useCallback(async (active: Session): Promise<Turn[]> => {
    const resp = await fetch(`${API}/v1/support/timeline`, {
      headers: { Authorization: `Bearer ${active.token}` },
    });
    if (resp.status === 401) throw new Error("EXPIRED");
    if (!resp.ok) throw new Error(`读取对话失败（HTTP ${resp.status}）`);
    const body = await resp.json();
    const items: Turn[] = body.items ?? [];
    setTurns(items);
    return items;
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const stored = readStored<Session>(STORAGE_KEY);
        const usable =
          stored && stored.expires_at * 1000 > Date.now() && stored.visitor_id === visitorId();
        const active = usable ? stored : await openSession();
        if (cancelled) return;
        setSession(active);
        await loadTimeline(active);
      } catch (err) {
        if (!cancelled) setProblem(err instanceof Error ? err.message : "初始化失败");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [openSession, loadTimeline]);

  // Poll while the agent is working. The platform answers asynchronously (the
  // run is queued and a worker picks it up), so the reply arrives as a new
  // turn rather than as the send response.
  useEffect(() => {
    if (!waiting || !session) return;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const items = await loadTimeline(session);
          // Only the assistant's own turn ends the wait. A `tool` turn must
          // not: it arrives *before* the answer, so treating it as "replied"
          // stops polling while the card is on screen and the customer is
          // still waiting for someone to say something.
          const answered = items.some(
            (t) => t.role === "agent" || t.role === "system",
          );
          if (answered || Date.now() > waitDeadline.current) {
            setWaiting(false);
          }
        } catch {
          setWaiting(false);
        }
      })();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [waiting, session, loadTimeline]);

  const send = useCallback(async () => {
    const text = draft.trim();
    if (!text || !session || busy) return;
    setBusy(true);
    setProblem(null);
    setDraft("");
    // Optimistic bubble, so the customer sees the message leave immediately.
    setTurns((prev) => [...prev, { role: "customer", text, at: Date.now() / 1000, source: "local" }]);
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
        body: JSON.stringify({ text }),
      });
      if (resp.status === 401) throw new Error("EXPIRED");
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(body?.error?.message ?? `发送失败（HTTP ${resp.status}）`);
      }
      await loadTimeline(session);
      waitDeadline.current = Date.now() + 120_000;
      setWaiting(true);
    } catch (err) {
      const message = err instanceof Error ? err.message : "发送失败";
      if (message === "EXPIRED") {
        // The token is a bearer credential with an expiry; re-open silently
        // rather than showing the customer a session error they cannot act on.
        try {
          const reopened = await openSession();
          setSession(reopened);
          setProblem("会话已续期，请重新发送。");
        } catch {
          setProblem("会话已过期，刷新页面即可继续。");
        }
      } else {
        setProblem(message);
      }
    } finally {
      setBusy(false);
    }
  }, [draft, session, busy, loadTimeline, openSession]);

  const accent = branding?.primary_color ?? "#1a393d";

  return (
    <div className="support-shell" style={{ ["--support-accent" as string]: accent }}>
      <header className="support-head">
        {branding?.logo_url ? (
          <img className="support-logo" src={branding.logo_url} alt="" />
        ) : null}
        <div>
          <h1>{branding?.display_name ?? "在线客服"}</h1>
          <p className="support-sub">
            {waiting ? "正在为您查询…" : "有问题请直接说，我会尽力解答"}
          </p>
        </div>
      </header>

      <main className="support-thread">
        {turns.length === 0 && !problem ? (
          <p className="support-empty">还没有对话。说一句话开始吧。</p>
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
            </div>
          );
        })}
        {waiting ? <div className="support-typing">客服正在输入…</div> : null}
      </main>

      {problem ? <p className="support-problem">{problem}</p> : null}

      <form
        className="support-composer"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="输入您的问题…"
          maxLength={4000}
          disabled={!session || busy}
        />
        <button type="submit" disabled={!session || busy || !draft.trim()}>
          {busy ? "发送中" : "发送"}
        </button>
      </form>
    </div>
  );
}
