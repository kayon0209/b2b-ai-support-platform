import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Bell, BookOpenText, Check, ChevronDown, ChevronLeft, ChevronRight, CircleHelp,
  Clock3, FileText, Filter, Headset, ImagePlus, MessageCircle, MoreHorizontal,
  Paperclip, Search, Send, Smile, Sparkles, Ticket, UserRound,
  UsersRound, X,
} from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { isOneOf, useUrlState } from "../lib/urlState";
import { ToolCard, type ToolCardData } from "../components/ToolCard";
import { apiGet, apiPost, apiUpload } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useLang } from "../lib/i18n";
import "../styles-workbench.css";

type Tab = "queue" | "mine" | "waiting";
/** Every tab the page can render. Kept beside the type so a new tab cannot
 *  be added to the union without also becoming a valid `?tab=` value. */
const ALL_TABS = ["queue", "mine", "waiting"] as const satisfies readonly Tab[];
type RightTab = "reply" | "knowledge" | "tools";
type Action = "claim" | "release" | "transfer" | "close";
type Origin = "free" | "canned" | "ai_suggestion";

interface Lease {
  owner: string;
  owner_ref: string | null;
  mode: string;
  version: number;
  updated_at: number;
}
interface CaseInfo {
  case_id: string;
  subject: string;
  status: string;
  priority: string;
  category: string;
  assignee_ref: string | null;
  team_ref: string | null;
  enterprise_account_id: string | null;
  version: number;
  first_response_due_at: number | null;
  first_responded_at: number | null;
  resolution_due_at: number | null;
  opened_at: number;
}
interface Turn {
  turn_id: string;
  role: string;
  text: string;
  at: number;
  source: string;
  card?: ToolCardData | null;
}
interface QueueItem {
  conversation_ref: string;
  title: string;
  preview: string;
  last_at: number | null;
  channel: string | null;
  contact_ref: string | null;
  case: CaseInfo | null;
  lease: Lease;
}
interface QueueResponse {
  items: QueueItem[];
  counts: Record<Tab, number>;
  total: number;
  limit: number;
  offset: number;
  actor_ref: string | null;
  agent_name: string | null;
  agent_status: string | null;
}
interface AccountInfo {
  name: string;
  tier: string;
  contract_status: string;
  attributes: Record<string, string>;
  missing: string[];
  contacts: Array<{ external_contact_id: string; channel: string | null }>;
}
interface Detail {
  conversation_ref: string;
  lease: Lease;
  case: CaseInfo | null;
  channel: string | null;
  contact_ref: string | null;
  account: AccountInfo | null;
  turns: Turn[];
  older_before: string | null;
  ai_suggestion: { text: string; sources: string[] } | null;
}
interface Agent {
  user_ref: string;
  display_name: string;
  status: string;
}
interface CannedReply {
  id: string;
  title: string;
  body: string;
  shortcut: string | null;
}
interface Attachment {
  attachment_id: string;
  filename: string;
  size_bytes: number;
  url: string | null;
}
interface RelatedCase {
  case_id: string;
  subject: string;
  status: string;
  match: string;
}

const CHANNEL: Record<string, string> = {
  web: "网页", email: "邮件", wechat: "微信", sms: "短信",
};
const STATUS: Record<string, string> = {
  new: "新工单", triaged: "已分诊", in_progress: "处理中",
  waiting_customer: "等待客户", waiting_internal: "内部处理中",
  waiting_vendor: "等待供应商", resolved: "已解决", closed: "已关闭",
  reopened: "重新打开",
};
const EMOJIS = ["🙂", "😊", "👍", "🙏", "✅", "📦", "🔧", "💡"];

function clock(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}
function countdown(due: number | null | undefined, now: number): string | null {
  if (!due) return null;
  const seconds = Math.abs(due - now);
  const hours = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  return `${due < now ? "超时" : "还剩"} ${String(hours).padStart(2, "0")}:${String(mins).padStart(2, "0")}`;
}
function channelName(channel: string | null): string {
  return channel ? (CHANNEL[channel] ?? channel) : "渠道未记录";
}
function displayName(item: { contact_ref: string | null; account?: AccountInfo | null }): string {
  return item.contact_ref || item.account?.name || "访客会话";
}
function uniqueTurns(older: Turn[], latest: Turn[]): Turn[] {
  const seen = new Set<string>();
  return [...older, ...latest].filter((turn) => {
    if (seen.has(turn.turn_id)) return false;
    seen.add(turn.turn_id);
    return true;
  });
}

export function Workbench() {
  const { conversationRef, caseId } = useParams<{ conversationRef?: string; caseId?: string }>();
  const navigate = useNavigate();
  const { lang } = useLang();
  // Tab and search go in the URL. This page already put `caseId` and
  // `conversationRef` in the path, so half of it was linkable and half was
  // not: an operator could share a case but not "my queue, filtered to this
  // customer", which is the view they actually want a second pair of eyes on.
  //
  // `isOneOf` is not decoration. A hand-edited or stale `?tab=whatever` would
  // otherwise put the page into a state no render path handles.
  const [tabParam, setTabParam] = useUrlState("tab", "queue");
  const tab: Tab = isOneOf(tabParam, ALL_TABS) ? tabParam : "queue";
  const setTab = (next: Tab) => setTabParam(next);
  const [query, setQuery] = useUrlState("q", "");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [queue, setQueue] = useState<QueueResponse | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [olderTurns, setOlderTurns] = useState<Turn[]>([]);
  const [olderBefore, setOlderBefore] = useState<string | null>(null);
  const [loadingQueue, setLoadingQueue] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [origin, setOrigin] = useState<Origin>("free");
  const [cannedId, setCannedId] = useState<string | null>(null);
  const [rightTab, setRightTab] = useState<RightTab>("reply");
  const [rightOpen, setRightOpen] = useState(() => window.innerWidth > 1320);
  const [mobileQueue, setMobileQueue] = useState(!conversationRef);
  const [menuOpen, setMenuOpen] = useState(false);
  const [emojiOpen, setEmojiOpen] = useState(false);
  const [cannedOpen, setCannedOpen] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [transferOpen, setTransferOpen] = useState(false);
  const [targetAgent, setTargetAgent] = useState("");
  const [confirmClose, setConfirmClose] = useState(false);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [canned, setCanned] = useState<CannedReply[]>([]);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [related, setRelated] = useState<RelatedCase[]>([]);
  const [now, setNow] = useState(Math.floor(Date.now() / 1000));
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const pendingSend = useRef<{ text: string; key: string } | null>(null);
  const detailRequest = useRef(0);
  const queueRequest = useRef(0);
  const selectedRef = conversationRef ?? null;

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [query]);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Math.floor(Date.now() / 1000)), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    const onResize = () => {
      if (window.innerWidth <= 760) setRightOpen(false);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Legacy case URLs remain valid; the new address names the actual conversation.
  useEffect(() => {
    if (!caseId || conversationRef) return;
    let active = true;
    void apiGet<{ conversation_ref: string | null }>(`/v1/cases/${caseId}/workbench`)
      .then((result) => {
        if (!active) return;
        if (result.conversation_ref) {
          navigate(`/admin/workbench/conversation/${result.conversation_ref}`, { replace: true });
        } else {
          setError("此工单尚未关联客户会话，可在工单页处理。");
        }
      })
      .catch((reason: unknown) => { if (active) setError(String(reason)); });
    return () => { active = false; };
  }, [caseId, conversationRef, navigate]);

  const loadQueue = useCallback(async (silent = false) => {
    const request = ++queueRequest.current;
    if (!silent) setLoadingQueue(true);
    try {
      const params = new URLSearchParams({ tab, limit: "50", offset: String(offset) });
      if (debouncedQuery) params.set("q", debouncedQuery);
      const result = await apiGet<QueueResponse>(`/v1/workbench/conversations?${params}`);
      if (queueRequest.current === request) {
        setQueue(result);
        if (!silent) setError(null);
      }
    } catch (reason) {
      if (!silent && queueRequest.current === request) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (!silent && queueRequest.current === request) setLoadingQueue(false);
    }
  }, [tab, offset, debouncedQuery]);

  useEffect(() => {
    void loadQueue();
    const timer = window.setInterval(() => {
      if (!document.hidden) void loadQueue(true);
    }, 8_000);
    return () => window.clearInterval(timer);
  }, [loadQueue]);

  useEffect(() => {
    if (selectedRef || caseId || !queue?.items.length) return;
    navigate(`/admin/workbench/conversation/${queue.items[0].conversation_ref}`, { replace: true });
  }, [selectedRef, caseId, queue, navigate]);

  const loadDetail = useCallback(async (ref: string, silent = false) => {
    const request = ++detailRequest.current;
    if (!silent) setLoadingDetail(true);
    try {
      const result = await apiGet<Detail>(`/v1/workbench/conversations/${ref}`);
      if (detailRequest.current !== request) return;
      setDetail(result);
      if (!silent) setOlderBefore(result.older_before);
      if (!silent) setError(null);
    } catch (reason) {
      if (!silent && detailRequest.current === request) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (!silent && detailRequest.current === request) setLoadingDetail(false);
    }
  }, []);

  useEffect(() => {
    if (!selectedRef) {
      setDetail(null);
      return;
    }
    setOlderTurns([]);
    setOlderBefore(null);
    setDraft("");
    setOrigin("free");
    setCannedId(null);
    setMobileQueue(false);
    void loadDetail(selectedRef);
    const timer = window.setInterval(() => {
      if (!document.hidden) void loadDetail(selectedRef, true);
    }, 5_000);
    return () => {
      window.clearInterval(timer);
      detailRequest.current += 1;
    };
  }, [selectedRef, loadDetail]);

  useEffect(() => {
    void apiGet<{ items: Agent[] }>("/v1/agents").then((data) => setAgents(data.items)).catch(() => undefined);
    void apiGet<{ items: CannedReply[] }>("/v1/canned-replies?limit=100")
      .then((data) => setCanned(data.items)).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!detail?.case?.case_id) {
      setAttachments([]);
      setRelated([]);
      return;
    }
    const id = detail.case.case_id;
    let active = true;
    void apiGet<{ items: Attachment[] }>(`/v1/cases/${id}/attachments`)
      .then((data) => { if (active) setAttachments(data.items); }).catch(() => undefined);
    void apiGet<{ related_cases: { items: RelatedCase[] } }>(`/v1/cases/${id}/workbench`)
      .then((data) => { if (active) setRelated(data.related_cases.items); }).catch(() => undefined);
    return () => { active = false; };
  }, [detail?.case?.case_id]);

  const turns = useMemo(() => uniqueTurns(olderTurns, detail?.turns ?? []), [olderTurns, detail?.turns]);
  useEffect(() => {
    const pane = transcriptRef.current;
    if (pane && !olderTurns.length) pane.scrollTop = pane.scrollHeight;
  }, [selectedRef, detail?.turns.length]);

  const myRef = queue?.actor_ref ?? null;
  const canReply = Boolean(detail && myRef && detail.lease.owner === "human" && detail.lease.owner_ref === myRef);
  const due = detail?.case?.first_responded_at
    ? detail.case.resolution_due_at
    : detail?.case?.first_response_due_at ?? detail?.case?.resolution_due_at;
  const slaText = countdown(due, now);
  const humanName = queue?.agent_name || (myRef ? `坐席 ${myRef.slice(0, 8)}` : "未登录");

  function selectConversation(ref: string) {
    setError(null);
    setNotice(null);
    setMobileQueue(false);
    navigate(`/admin/workbench/conversation/${ref}`);
  }
  function selectTab(next: Tab) {
    setTab(next);
    setOffset(0);
    setMobileQueue(true);
  }
  async function loadOlder() {
    if (!selectedRef || !olderBefore || busy) return;
    setBusy(true);
    try {
      const result = await apiGet<{ items: Turn[]; older_before: string | null }>(
        `/v1/workbench/conversations/${selectedRef}/timeline?before=${olderBefore}`,
      );
      setOlderTurns((previous) => uniqueTurns(result.items, previous));
      setOlderBefore(result.older_before);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }
  async function runAction(operation: Action, targetRef?: string) {
    if (!detail || busy) return;
    setBusy(true);
    setError(null);
    try {
      await apiPost(
        `/v1/workbench/conversations/${detail.conversation_ref}/actions`,
        { operation, expected_version: detail.lease.version, target_ref: targetRef ?? null },
        newIdempotencyKey(),
      );
      const messages: Record<Action, string> = {
        claim: "已接入会话。", release: "会话已返回待认领队列。",
        transfer: "会话已转交给指定坐席。", close: "会话已结束；有人工回复时客户可以评价服务。",
      };
      setNotice(messages[operation]);
      setConfirmClose(false);
      setTransferOpen(false);
      setMenuOpen(false);
      await Promise.all([loadQueue(true), loadDetail(detail.conversation_ref, true)]);
      if (operation === "release" || operation === "transfer" || operation === "close") {
        setTab("queue");
      } else {
        setTab("mine");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      await loadDetail(detail.conversation_ref, true);
    } finally {
      setBusy(false);
    }
  }
  async function sendReply() {
    if (!detail || !canReply || busy) return;
    const text = draft.trim();
    if (!text || text.length > 4000) return;
    const previous = pendingSend.current;
    const key = previous?.text === text ? previous.key : newIdempotencyKey();
    pendingSend.current = { text, key };
    setBusy(true);
    setError(null);
    try {
      await apiPost(
        `/v1/conversations/${detail.conversation_ref}/replies`,
        { text, origin, canned_reply_id: origin === "canned" ? cannedId : null },
        key,
      );
      pendingSend.current = null;
      setDraft("");
      setOrigin("free");
      setCannedId(null);
      setNotice("回复已记录并提交投递。");
      await Promise.all([loadDetail(detail.conversation_ref, true), loadQueue(true)]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }
  async function insertCanned(reply: CannedReply) {
    try {
      const used = await apiPost<CannedReply>(`/v1/canned-replies/${reply.id}/use`, undefined);
      setDraft(used.body);
      setOrigin("canned");
      setCannedId(reply.id);
      setCannedOpen(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }
  async function uploadAttachment(file: File) {
    if (!detail?.case || busy) return;
    const form = new FormData();
    form.append("file", file);
    setBusy(true);
    try {
      await apiUpload(`/v1/cases/${detail.case.case_id}/attachments`, form, newIdempotencyKey());
      const data = await apiGet<{ items: Attachment[] }>(`/v1/cases/${detail.case.case_id}/attachments`);
      setAttachments(data.items);
      setRightTab("tools");
      setNotice("文件已添加到工单证据；不会作为聊天消息发送给客户。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <div className={`wb-root${mobileQueue ? " wb-show-queue" : ""}${rightOpen ? " wb-right-open" : ""}`}>
      <header className="wb-topbar">
        <div className="wb-topbar-title">
          <button className="wb-mobile-back" type="button" onClick={() => {
            if (detail?.lease.owner === "human" && detail.lease.owner_ref === myRef) {
              setTab(detail.lease.mode === "HUMAN_WAITING_CUSTOMER" ? "waiting" : "mine");
            } else {
              setTab("queue");
            }
            setOffset(0);
            setMobileQueue(true);
          }} aria-label="返回会话队列">
            <ChevronLeft size={20} />
          </button>
          <h1>{lang === "zh" ? "坐席工作台" : "Agent workbench"}</h1>
          <span className={`wb-presence${queue?.agent_status === "active" ? " is-online" : ""}`}>
            <span className="wb-presence-dot" />
            {queue?.agent_status === "active" ? "在线" : queue?.agent_status === "inactive" ? "离线" : "未登记坐席"}
          </span>
        </div>
        <div className="wb-topbar-actions">
          <label className="wb-global-search">
            <Search size={17} aria-hidden="true" />
            <span className="sr-only">搜索会话、订单或关键词</span>
            <input value={query} onChange={(event) => { setQuery(event.target.value); setOffset(0); }} placeholder="搜索会话、订单或关键词…" />
          </label>
          <div className="wb-popover-anchor">
            <button type="button" className="wb-icon-btn wb-bell" onClick={() => setNotificationsOpen((open) => !open)} aria-label="查看待处理提醒" aria-expanded={notificationsOpen}>
              <Bell size={20} />{queue && queue.counts.queue > 0 ? <i /> : null}
            </button>
            {notificationsOpen ? (
              <div className="wb-notifications" role="dialog" aria-label="待处理提醒">
                <strong>待处理提醒</strong>
                <button type="button" onClick={() => { selectTab("queue"); setNotificationsOpen(false); }}>待认领会话 <b>{queue?.counts.queue ?? 0}</b></button>
                <button type="button" onClick={() => { selectTab("waiting"); setNotificationsOpen(false); }}>等待客户 <b>{queue?.counts.waiting ?? 0}</b></button>
              </div>
            ) : null}
          </div>
          <div className="wb-avatar" aria-hidden="true">{humanName.slice(0, 1)}</div>
          <div className="wb-agent-name"><strong>{humanName}</strong><span>客服坐席</span></div>
        </div>
      </header>

      {error ? <div className="wb-alert is-error" role="alert"><span>{error}</span><button type="button" onClick={() => { setError(null); void loadQueue(); if (selectedRef) void loadDetail(selectedRef); }}>重试</button></div> : null}
      {notice ? <div className="wb-alert is-success" role="status"><span>{notice}</span><button type="button" onClick={() => setNotice(null)} aria-label="关闭提示"><X size={15} /></button></div> : null}

      <div className="wb-body">
        <section className="wb-queue" aria-label="会话队列">
          <div className="wb-queue-heading"><h2>会话队列</h2><Filter size={18} aria-hidden="true" /></div>
          <div className="wb-queue-tabs" role="tablist" aria-label="会话分类">
            {(["queue", "mine", "waiting"] as const).map((key) => (
              <button key={key} type="button" role="tab" aria-selected={tab === key} className={tab === key ? "active" : ""} onClick={() => selectTab(key)}>
                {key === "queue" ? "待认领" : key === "mine" ? "我的会话" : "等待中"}
                <span>{queue?.counts[key] ?? 0}</span>
              </button>
            ))}
          </div>
          <label className="wb-queue-search">
            <Search size={16} aria-hidden="true" />
            <span className="sr-only">搜索当前队列</span>
            <input value={query} onChange={(event) => { setQuery(event.target.value); setOffset(0); }} placeholder="搜索会话、客户或订单号…" />
          </label>
          <div className="wb-queue-scroll">
            {loadingQueue && !queue ? <p className="wb-muted wb-padding">正在加载会话…</p> : null}
            {!loadingQueue && queue?.items.length === 0 ? <div className="wb-queue-empty"><MessageCircle size={25} /><p>{query ? "没有匹配的会话" : "当前队列暂无会话"}</p></div> : null}
            {queue?.items.map((item) => {
              const sla = countdown(item.case?.first_responded_at
                ? item.case.resolution_due_at
                : item.case?.first_response_due_at ?? item.case?.resolution_due_at, now);
              const urgent = Boolean(item.case?.priority === "p0" || item.case?.priority === "p1");
              return (
                <button type="button" key={item.conversation_ref} className={`wb-queue-row${selectedRef === item.conversation_ref ? " selected" : ""}`} onClick={() => selectConversation(item.conversation_ref)}>
                  <span className="wb-queue-avatar">{(item.contact_ref || item.title).slice(0, 1)}</span>
                  <span className="wb-queue-row-main">
                    <span className="wb-queue-row-top"><strong>{item.title}</strong><time>{clock(item.last_at)}</time></span>
                    <span className="wb-queue-row-meta"><span className={urgent ? "is-urgent" : ""}>{item.lease.owner === "queue" ? "待回复" : item.lease.mode === "HUMAN_WAITING_CUSTOMER" ? "等待客户" : "进行中"}</span>{item.contact_ref ? ` · ${item.contact_ref}` : ""}</span>
                    <span className="wb-queue-row-preview">{item.preview || "等待客户消息"}</span>
                    <span className="wb-queue-row-foot"><span>{channelName(item.channel)}</span>{sla ? <span className={sla.startsWith("超时") ? "is-urgent" : ""}>{sla}</span> : null}</span>
                  </span>
                </button>
              );
            })}
            {queue && offset + queue.items.length < queue.total ? (
              <button type="button" className="wb-load-more" onClick={() => setOffset(offset + 50)}>加载更多会话</button>
            ) : null}
            {offset > 0 ? <button type="button" className="wb-load-more" onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</button> : null}
          </div>
        </section>

        <section className="wb-conversation" aria-label="当前会话">
          {!selectedRef ? <div className="wb-center-empty"><Headset size={36} /><h2>选择一条会话开始接待</h2><p>待认领的对话会出现在左侧队列。</p></div> : null}
          {selectedRef && loadingDetail && !detail ? <div className="wb-center-empty">正在打开会话…</div> : null}
          {detail && selectedRef === detail.conversation_ref ? <>
            <header className="wb-conversation-head">
              <div className="wb-contact-icon"><UsersRound size={19} /></div>
              <div className="wb-contact-name"><strong>{displayName({ contact_ref: detail.contact_ref, account: detail.account })}</strong><span>{detail.account?.name || detail.case?.subject || "未关联工单"}</span></div>
              <span className="wb-channel"><MessageCircle size={14} />{channelName(detail.channel)}</span>
              {detail.case ? <span className={`wb-priority ${detail.case.priority}`}>优先级 {detail.case.priority.toUpperCase()}</span> : null}
              {slaText ? <span className={`wb-sla${slaText.startsWith("超时") ? " is-overdue" : ""}`}><Clock3 size={15} />SLA {slaText}</span> : null}
              <div className="wb-head-spacer" />
              <div className="wb-popover-anchor">
                <button type="button" className="wb-icon-btn" aria-label="更多会话操作" aria-expanded={menuOpen} onClick={() => setMenuOpen((open) => !open)}><MoreHorizontal size={21} /></button>
                {menuOpen ? <div className="wb-action-menu">
                  {canReply ? <button type="button" onClick={() => void runAction("release")}>返回待认领队列</button> : null}
                  {detail.case ? <Link to={`/admin/cases?case=${detail.case.case_id}`}>查看关联工单</Link> : null}
                  <Link to={`/admin/conversations`}>打开会话回放</Link>
                </div> : null}
              </div>
              {detail.lease.owner === "queue" ? <button className="wb-primary-small" type="button" disabled={busy} onClick={() => void runAction("claim")}>接入会话</button> : null}
              {canReply ? <>
                <button className="wb-secondary-small" type="button" onClick={() => { setTransferOpen(true); setTargetAgent(""); }}>转接</button>
                <button className="wb-secondary-small" type="button" onClick={() => setConfirmClose(true)}>结束会话</button>
              </> : null}
              <button className="wb-icon-btn wb-right-toggle" type="button" onClick={() => setRightOpen((open) => !open)} aria-label={rightOpen ? "收起 AI 副驾" : "展开 AI 副驾"}><Sparkles size={20} /></button>
            </header>
            <div className="wb-transcript" role="log" aria-label="对话记录" aria-live="polite" ref={transcriptRef}>
              {olderBefore ? <button type="button" className="wb-load-history" disabled={busy} onClick={() => void loadOlder()}>查看更早消息</button> : null}
              {turns.length === 0 ? <div className="wb-thread-empty">暂无对话内容。接入后可直接回复客户。</div> : null}
              {turns.map((turn) => {
                if (turn.role === "tool") return turn.card ? <div className="wb-tool-wrap" key={turn.turn_id}><ToolCard card={turn.card} /></div> : null;
                if (turn.role === "system") return <div className="wb-system-turn" key={turn.turn_id}>{turn.text}</div>;
                const outgoing = turn.role === "agent";
                return <article className={`wb-turn ${outgoing ? "is-outgoing" : "is-customer"}`} key={turn.turn_id}>
                  {!outgoing ? <div className="wb-turn-avatar">客</div> : null}
                  <div className="wb-turn-main"><div className="wb-turn-meta"><strong>{outgoing ? (turn.source === "agent" ? "人工坐席" : "AI 助手") : (detail.contact_ref || "客户")}</strong><time>{clock(turn.at)}</time></div><div className="wb-bubble">{turn.text}</div></div>
                  {outgoing ? <div className="wb-turn-avatar is-agent">{turn.source === "agent" ? humanName.slice(0, 1) : "AI"}</div> : null}
                </article>;
              })}
            </div>
            <div className="wb-compose-area">
              {detail.lease.owner === "closed" ? <div className="wb-ownership-note">会话已结束。已发生人工回复的服务可由客户评价。</div>
                : detail.lease.owner === "queue" ? <div className="wb-ownership-note">客户正在等待人工接入。点击“接入会话”后即可发送回复。</div>
                : !canReply ? <div className="wb-ownership-note">当前由其他坐席接待，回复区为只读。</div> : null}
              <div className="wb-composer">
                <label className="sr-only" htmlFor="wb-reply-input">回复客户</label>
                <textarea id="wb-reply-input" value={draft} onChange={(event) => { setDraft(event.target.value); if (origin !== "free") setOrigin("free"); }} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void sendReply(); } }} placeholder="输入回复…  Shift + Enter 换行" maxLength={4000} disabled={!canReply || busy} />
                <div className="wb-composer-bar">
                  <div className="wb-composer-tools">
                    <input ref={fileRef} type="file" hidden accept=".pdf,.png,.jpg,.jpeg,.txt,.docx" onChange={(event) => { const file = event.target.files?.[0]; if (file) void uploadAttachment(file); }} />
                    <button type="button" aria-label="上传工单证据" title={detail.case ? "上传工单证据，不会发送到客户聊天" : "此会话无关联工单，暂不能上传证据"} disabled={!canReply || !detail.case || busy} onClick={() => fileRef.current?.click()}><Paperclip size={19} /></button>
                    <button type="button" aria-label="上传工单图片" title="上传工单图片，不会发送到客户聊天" disabled={!canReply || !detail.case || busy} onClick={() => fileRef.current?.click()}><ImagePlus size={19} /></button>
                    <div className="wb-popover-anchor"><button type="button" aria-label="插入表情" disabled={!canReply} onClick={() => setEmojiOpen((open) => !open)}><Smile size={19} /></button>{emojiOpen ? <div className="wb-emoji-popover">{EMOJIS.map((emoji) => <button key={emoji} type="button" onClick={() => { setDraft((text) => text + emoji); setEmojiOpen(false); }}>{emoji}</button>)}</div> : null}</div>
                  </div>
                  <div className="wb-composer-send">
                    <div className="wb-popover-anchor"><button className="wb-canned-btn" type="button" disabled={!canReply} onClick={() => setCannedOpen((open) => !open)}>插入话术 <ChevronDown size={15} /></button>{cannedOpen ? <div className="wb-canned-popover"><strong>常用话术</strong>{canned.length ? canned.map((reply) => <button key={reply.id} type="button" onClick={() => void insertCanned(reply)}><span>{reply.title}</span><small>{reply.shortcut || ""}</small></button>) : <p>暂无可用话术</p>}</div> : null}</div>
                    <button className="wb-send" type="button" onClick={() => void sendReply()} disabled={!canReply || busy || !draft.trim()}><Send size={17} />{busy ? "处理中" : "发送"}</button>
                  </div>
                </div>
              </div>
            </div>
          </> : null}
        </section>

        {detail && rightOpen ? <aside className="wb-right" aria-label="AI 副驾与客户上下文">
          <div className="wb-right-header"><Sparkles size={20} /><strong>AI 副驾</strong><button type="button" className="wb-right-close" aria-label="收起 AI 副驾" onClick={() => setRightOpen(false)}><X size={17} /></button><div className="wb-right-tabs" role="tablist" aria-label="副驾内容">{(["reply", "knowledge", "tools"] as const).map((key) => <button type="button" role="tab" aria-selected={rightTab === key} className={rightTab === key ? "active" : ""} key={key} onClick={() => setRightTab(key)}>{key === "reply" ? "话术" : key === "knowledge" ? "知识" : "工具"}</button>)}</div></div>
          <div className="wb-right-scroll">
            {rightTab === "reply" ? <>
              <section className="wb-panel"><h3><FileText size={18} />建议回复</h3><p className="wb-muted">基于已记录的会话与引用来源</p>{detail.ai_suggestion?.text ? <><div className="wb-suggestion">{detail.ai_suggestion.text}</div><div className="wb-panel-actions"><button className="wb-primary-small" type="button" disabled={!canReply} onClick={() => { setDraft(detail.ai_suggestion?.text ?? ""); setOrigin("ai_suggestion"); setCannedId(null); }}>插入到回复框</button><button type="button" className="wb-secondary-small" onClick={() => void loadDetail(detail.conversation_ref)}>刷新建议</button></div></> : <p className="wb-muted">当前没有可引用的 AI 建议，请结合会话记录人工回复。</p>}</section>
              <Sources sources={detail.ai_suggestion?.sources ?? []} />
              <CustomerPanel detail={detail} />
            </> : null}
            {rightTab === "knowledge" ? <><Sources sources={detail.ai_suggestion?.sources ?? []} /><section className="wb-panel"><h3><Ticket size={18} />相似工单</h3>{related.length ? related.map((item) => <Link className="wb-resource-row" key={item.case_id} to={`/admin/cases?case=${item.case_id}`}><span><strong>{item.subject}</strong><small>{item.match === "subject" ? "标题相似" : "同类目"} · {STATUS[item.status] ?? item.status}</small></span><ChevronRight size={16} /></Link>) : <p className="wb-muted">暂无相似工单。</p>}</section></> : null}
            {rightTab === "tools" ? <><section className="wb-panel"><h3><CircleHelp size={18} />已查业务数据</h3>{turns.some((turn) => turn.card) ? turns.filter((turn) => turn.card).map((turn) => <div className="wb-right-tool" key={turn.turn_id}><ToolCard card={turn.card!} /></div>) : <p className="wb-muted">当前会话没有业务数据卡片。</p>}</section><section className="wb-panel"><h3><Paperclip size={18} />工单证据</h3>{attachments.length ? attachments.map((item) => item.url ? <a className="wb-resource-row" href={item.url} target="_blank" rel="noreferrer" key={item.attachment_id}><span>{item.filename}</span><ChevronRight size={16} /></a> : <p key={item.attachment_id}>{item.filename}</p>) : <p className="wb-muted">暂无附件。上传文件只保存为工单证据。</p>}</section><Link className="wb-panel-link" to="/admin/approvals">查看待审批操作 <ChevronRight size={16} /></Link></> : null}
          </div>
        </aside> : null}
      </div>

      {transferOpen ? <div className="wb-modal-backdrop" role="presentation" onClick={() => setTransferOpen(false)}><div className="wb-modal" role="dialog" aria-modal="true" aria-label="转接会话" onClick={(event) => event.stopPropagation()}><h2>转接会话</h2><p>目标坐席接手后，当前坐席将无法继续回复。</p><label>目标坐席<select value={targetAgent} onChange={(event) => setTargetAgent(event.target.value)}><option value="">选择在线坐席</option>{agents.filter((agent) => agent.user_ref !== myRef && agent.status === "active").map((agent) => <option key={agent.user_ref} value={agent.user_ref}>{agent.display_name}</option>)}</select></label><div className="wb-modal-actions"><button type="button" onClick={() => setTransferOpen(false)}>取消</button><button type="button" className="wb-primary-small" disabled={!targetAgent || busy} onClick={() => void runAction("transfer", targetAgent)}>确认转接</button></div></div></div> : null}
      {confirmClose ? <div className="wb-modal-backdrop" role="presentation" onClick={() => setConfirmClose(false)}><div className="wb-modal" role="dialog" aria-modal="true" aria-label="结束会话" onClick={(event) => event.stopPropagation()}><h2>结束会话</h2><p>结束且已有人工回复时，客户可以评价本次服务。若有相关工单，请另行确认工单状态。</p><div className="wb-modal-actions"><button type="button" onClick={() => setConfirmClose(false)}>继续接待</button><button type="button" className="wb-primary-small" disabled={busy} onClick={() => void runAction("close")}>确认结束</button></div></div></div> : null}
    </div>
  );
}

function Sources({ sources }: { sources: string[] }) {
  return <section className="wb-panel"><h3><BookOpenText size={18} />依据来源</h3>{sources.length ? sources.map((source, index) => <div className="wb-resource-row" key={`${source}-${index}`}><span><strong>{source}</strong><small>来自已引用资料</small></span><Check size={16} /></div>) : <p className="wb-muted">当前建议没有可展示的引用来源。</p>}</section>;
}

function CustomerPanel({ detail }: { detail: Detail }) {
  return <section className="wb-panel wb-customer-panel"><h3><UserRound size={18} />客户资料{detail.case?.enterprise_account_id ? <Link to={`/admin/customers?account=${detail.case.enterprise_account_id}`}>查看 <ChevronRight size={14} /></Link> : null}</h3>{detail.account ? <><div className="wb-customer-company"><div className="wb-company-avatar">企</div><span><strong>{detail.account.name}</strong><small>{detail.account.tier} · {detail.account.contract_status}</small></span></div><dl><div><dt>行业</dt><dd>{detail.account.attributes.industry || "未记录"}</dd></div><div><dt>客户经理</dt><dd>{detail.account.attributes.account_manager || "未记录"}</dd></div><div><dt>付款条款</dt><dd>{detail.account.attributes.payment_terms || "未记录"}</dd></div></dl>{detail.account.contacts.length ? <><h4>已绑定联系人</h4>{detail.account.contacts.map((contact) => <p className="wb-contact-row" key={contact.external_contact_id}>{contact.external_contact_id}<span>{channelName(contact.channel)}</span></p>)}</> : null}</> : <p className="wb-muted">尚未关联企业账户。客户信息以会话记录为准。</p>}{detail.case ? <Link className="wb-panel-link" to={`/admin/cases?case=${detail.case.case_id}`}><Ticket size={16} />查看工单 · {STATUS[detail.case.status] ?? detail.case.status}<ChevronRight size={15} /></Link> : null}</section>;
}
