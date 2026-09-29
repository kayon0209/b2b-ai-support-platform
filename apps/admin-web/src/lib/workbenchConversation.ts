/** A detail or asynchronous result must still belong to the active route. */
export function isCurrentWorkbenchConversation(
  candidateConversationRef: string | null | undefined,
  selectedConversationRef: string | null,
): boolean {
  return selectedConversationRef !== null && candidateConversationRef === selectedConversationRef;
}

export interface PendingReplySubmission {
  text: string;
  key: string;
}

export function canClearSubmittedDraft(submittedRevision: number, currentRevision: number): boolean {
  return submittedRevision === currentRevision;
}

export function pendingReplyIdempotencyKey(
  pending: Map<string, PendingReplySubmission>,
  conversationRef: string,
  text: string,
  createKey: () => string,
): string {
  const previous = pending.get(conversationRef);
  if (previous?.text === text) return previous.key;
  const key = createKey();
  pending.set(conversationRef, { text, key });
  return key;
}

export function forgetPendingReply(
  pending: Map<string, PendingReplySubmission>,
  conversationRef: string,
  key: string,
): void {
  if (pending.get(conversationRef)?.key === key) pending.delete(conversationRef);
}
