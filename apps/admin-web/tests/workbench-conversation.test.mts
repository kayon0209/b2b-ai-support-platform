import assert from "node:assert/strict";
import {
  canClearSubmittedDraft,
  forgetPendingReply,
  isCurrentWorkbenchConversation,
  pendingReplyIdempotencyKey,
} from "../src/lib/workbenchConversation.ts";
import {
  parseWorkbenchComposerDraft,
  parseWorkbenchDraftSyncMessage,
  remoteDraftDecision,
  workbenchDraftChannelName,
  workbenchDraftStorageKey,
  WORKBENCH_DRAFT_TTL_MS,
} from "../src/lib/workbenchDraftSync.ts";

assert.equal(isCurrentWorkbenchConversation("conversation-a", "conversation-a"), true);
assert.equal(isCurrentWorkbenchConversation("conversation-a", "conversation-b"), false);
assert.equal(isCurrentWorkbenchConversation("conversation-a", null), false);
assert.equal(isCurrentWorkbenchConversation(undefined, "conversation-a"), false);

const pending = new Map<string, { text: string; key: string }>();
let nextKey = 0;
const createKey = () => `key-${++nextKey}`;
const firstA = pendingReplyIdempotencyKey(pending, "conversation-a", "same text", createKey);
assert.equal(pendingReplyIdempotencyKey(pending, "conversation-a", "same text", createKey), firstA);
const firstB = pendingReplyIdempotencyKey(pending, "conversation-b", "same text", createKey);
assert.notEqual(firstB, firstA);
assert.equal(pendingReplyIdempotencyKey(pending, "conversation-a", "same text", createKey), firstA);
forgetPendingReply(pending, "conversation-a", "stale-key");
assert.equal(pending.get("conversation-a")?.key, firstA);
forgetPendingReply(pending, "conversation-a", firstA);
assert.equal(pending.has("conversation-a"), false);
assert.equal(canClearSubmittedDraft(4, 4), true);
assert.equal(canClearSubmittedDraft(4, 5), false);

const now = 1_800_000_000_000;
const storedDraft = {
  schema_version: 1,
  actor_ref: "agent-a",
  conversation_ref: "conversation-a",
  body: "Synthetic recovery draft",
  origin: "free",
  canned_reply_id: null,
  copilot_job_id: null,
  updated_at_ms: now - 1000,
};
assert.equal(workbenchDraftStorageKey("agent-a", "conversation-a"), "workbench.reply-draft.v1.agent-a.conversation-a");
assert.equal(workbenchDraftChannelName("agent-a", "conversation-a"), "workbench.reply-draft.v1.agent-a.conversation-a");
assert.deepEqual(parseWorkbenchComposerDraft(JSON.stringify(storedDraft), "agent-a", "conversation-a", now), storedDraft);
assert.equal(parseWorkbenchComposerDraft(JSON.stringify(storedDraft), "agent-b", "conversation-a", now), null);
assert.equal(parseWorkbenchComposerDraft(JSON.stringify({ ...storedDraft, updated_at_ms: now - WORKBENCH_DRAFT_TTL_MS - 1 }), "agent-a", "conversation-a", now), null);
assert.equal(parseWorkbenchComposerDraft(JSON.stringify({ ...storedDraft, body: "x".repeat(4001) }), "agent-a", "conversation-a", now), null);
const message = {
  schema_version: 1,
  kind: "draft",
  sender_tab_id: "tab-b",
  actor_ref: "agent-a",
  conversation_ref: "conversation-a",
  updated_at_ms: now - 1000,
  draft: storedDraft,
};
assert.equal(parseWorkbenchDraftSyncMessage(message, "agent-a", "conversation-a", now)?.sender_tab_id, "tab-b");
assert.equal(parseWorkbenchDraftSyncMessage(message, "agent-b", "conversation-a", now), null);
assert.equal(parseWorkbenchDraftSyncMessage({ ...message, kind: "request", draft: undefined }, "agent-a", "conversation-a", now)?.kind, "request");
assert.equal(parseWorkbenchDraftSyncMessage({ ...message, kind: "clear", draft: undefined }, "agent-a", "conversation-a", now)?.kind, "clear");
assert.equal(remoteDraftDecision("", false, "from another tab"), "apply");
assert.equal(remoteDraftDecision("local edit", true, "remote edit"), "conflict");
assert.equal(remoteDraftDecision("same", false, "same"), "ignore");

console.log("workbench guards: 4 identity, 6 idempotency, 2 revision, and 13 draft sync checks passed");
