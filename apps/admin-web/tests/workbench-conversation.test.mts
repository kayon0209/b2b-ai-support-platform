import assert from "node:assert/strict";
import {
  canClearSubmittedDraft,
  forgetPendingReply,
  isCurrentWorkbenchConversation,
  pendingReplyIdempotencyKey,
} from "../src/lib/workbenchConversation.ts";

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

console.log("workbench conversation guards: 4 identity, 6 idempotency, and 2 draft revision checks passed");
