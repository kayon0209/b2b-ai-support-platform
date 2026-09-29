export type WorkbenchDraftOrigin = "free" | "canned" | "ai_suggestion";

export interface WorkbenchComposerDraft {
  schema_version: 1;
  actor_ref: string;
  conversation_ref: string;
  body: string;
  origin: WorkbenchDraftOrigin;
  canned_reply_id: string | null;
  copilot_job_id: string | null;
  updated_at_ms: number;
}

export interface WorkbenchDraftSyncMessage {
  schema_version: 1;
  kind: "draft" | "clear" | "request";
  sender_tab_id: string;
  actor_ref: string;
  conversation_ref: string;
  updated_at_ms: number;
  draft?: WorkbenchComposerDraft;
}

export const WORKBENCH_DRAFT_TTL_MS = 30 * 60 * 1000;
const MAX_CLOCK_SKEW_MS = 60 * 1000;

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseDraft(
  value: unknown,
  actorRef: string,
  conversationRef: string,
  nowMs: number,
): WorkbenchComposerDraft | null {
  if (!isObject(value)) return null;
  const origin = value.origin;
  const body = value.body;
  const updatedAtMs = value.updated_at_ms;
  const cannedReplyId = value.canned_reply_id;
  const copilotJobId = value.copilot_job_id;
  if (
    value.schema_version !== 1 ||
    value.actor_ref !== actorRef ||
    value.conversation_ref !== conversationRef ||
    typeof body !== "string" || body.length > 4000 ||
    !(origin === "free" || origin === "canned" || origin === "ai_suggestion") ||
    !(cannedReplyId === null || (typeof cannedReplyId === "string" && cannedReplyId.length <= 255)) ||
    !(copilotJobId === null || (typeof copilotJobId === "string" && copilotJobId.length <= 64)) ||
    typeof updatedAtMs !== "number" || !Number.isSafeInteger(updatedAtMs) ||
    updatedAtMs > nowMs + MAX_CLOCK_SKEW_MS || nowMs - updatedAtMs > WORKBENCH_DRAFT_TTL_MS
  ) {
    return null;
  }
  return {
    schema_version: 1,
    actor_ref: actorRef,
    conversation_ref: conversationRef,
    body,
    origin,
    canned_reply_id: cannedReplyId,
    copilot_job_id: copilotJobId,
    updated_at_ms: updatedAtMs,
  };
}

export function workbenchDraftStorageKey(actorRef: string, conversationRef: string): string {
  return `workbench.reply-draft.v1.${actorRef}.${conversationRef}`;
}

export function workbenchDraftChannelName(actorRef: string, conversationRef: string): string {
  return `workbench.reply-draft.v1.${actorRef}.${conversationRef}`;
}

export function parseWorkbenchComposerDraft(
  serialized: string | null,
  actorRef: string,
  conversationRef: string,
  nowMs = Date.now(),
): WorkbenchComposerDraft | null {
  if (!serialized) return null;
  try {
    return parseDraft(JSON.parse(serialized) as unknown, actorRef, conversationRef, nowMs);
  } catch {
    return null;
  }
}

export function parseWorkbenchDraftSyncMessage(
  value: unknown,
  actorRef: string,
  conversationRef: string,
  nowMs = Date.now(),
): WorkbenchDraftSyncMessage | null {
  if (!isObject(value)) return null;
  const updatedAtMs = value.updated_at_ms;
  const senderTabId = value.sender_tab_id;
  if (
    value.schema_version !== 1 ||
    value.actor_ref !== actorRef ||
    value.conversation_ref !== conversationRef ||
    !(value.kind === "draft" || value.kind === "clear" || value.kind === "request") ||
    typeof senderTabId !== "string" || senderTabId.length > 128 ||
    typeof updatedAtMs !== "number" || !Number.isSafeInteger(updatedAtMs) ||
    updatedAtMs > nowMs + MAX_CLOCK_SKEW_MS || nowMs - updatedAtMs > WORKBENCH_DRAFT_TTL_MS
  ) {
    return null;
  }
  if (value.kind === "clear" || value.kind === "request") {
    return {
      schema_version: 1,
      kind: value.kind,
      sender_tab_id: senderTabId,
      actor_ref: actorRef,
      conversation_ref: conversationRef,
      updated_at_ms: updatedAtMs,
    };
  }
  const draft = parseDraft(value.draft, actorRef, conversationRef, nowMs);
  if (!draft) return null;
  return {
    schema_version: 1,
    kind: "draft",
    sender_tab_id: senderTabId,
    actor_ref: actorRef,
    conversation_ref: conversationRef,
    updated_at_ms: updatedAtMs,
    draft,
  };
}

export function remoteDraftDecision(
  currentBody: string,
  locallyEdited: boolean,
  incomingBody: string,
  currentUpdatedAtMs = -1,
  incomingUpdatedAtMs = 0,
): "ignore" | "apply" | "conflict" {
  if (currentBody === incomingBody) return "ignore";
  if (incomingUpdatedAtMs < currentUpdatedAtMs) return "ignore";
  if (incomingUpdatedAtMs === currentUpdatedAtMs) return "conflict";
  return locallyEdited ? "conflict" : "apply";
}

export function isSubmittedDraftSnapshot(
  snapshot: WorkbenchComposerDraft | null,
  submittedBody: string,
  submittedUpdatedAtMs: number,
): boolean {
  return Boolean(
    snapshot &&
    snapshot.body === submittedBody &&
    snapshot.updated_at_ms === submittedUpdatedAtMs,
  );
}
