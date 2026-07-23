/**
 * Shared TanStack Query keys for the chat feature (see components/chat/),
 * so a mutation (creating a conversation, sending a message) can invalidate
 * or update exactly the cache entries a query component reads, the same
 * pattern lib/documents.ts uses for the document list.
 */
export const CONVERSATIONS_QUERY_KEY = ["conversations"] as const;

export function conversationQueryKey(conversationId: string) {
  return ["conversation", conversationId] as const;
}

export function chunkQueryKey(chunkId: string) {
  return ["chunk", chunkId] as const;
}
