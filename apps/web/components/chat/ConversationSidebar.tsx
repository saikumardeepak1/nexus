"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { ApiError, createConversation, listConversations } from "@/lib/api-client";
import { CONVERSATIONS_QUERY_KEY } from "@/lib/chat";
import { cn } from "@/lib/utils";
import type { ConversationListResponse } from "@/lib/types";

interface ConversationSidebarProps {
  selectedConversationId: string | null;
  onSelectConversation: (conversationId: string) => void;
}

/**
 * Conversation picker: lists every conversation owned by the signed-in
 * user (GET /v1/conversations) and a "new conversation" action (POST
 * /v1/conversations). A freshly created conversation is written straight
 * into the shared query cache and selected immediately, the same
 * cache-update-over-refetch pattern DocumentUploadForm uses for the
 * document list.
 */
export function ConversationSidebar({
  selectedConversationId,
  onSelectConversation,
}: ConversationSidebarProps) {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: CONVERSATIONS_QUERY_KEY,
    queryFn: listConversations,
  });

  const createMutation = useMutation({
    mutationFn: () => createConversation(),
    onSuccess: (conversation) => {
      queryClient.setQueryData<ConversationListResponse>(CONVERSATIONS_QUERY_KEY, (old) => ({
        conversations: [conversation, ...(old?.conversations ?? [])],
      }));
      onSelectConversation(conversation.id);
    },
  });

  const conversations = query.data?.conversations ?? [];

  return (
    <div className="flex h-full flex-col gap-3">
      <Button
        type="button"
        size="sm"
        onClick={() => createMutation.mutate()}
        disabled={createMutation.isPending}
      >
        {createMutation.isPending ? "Creating..." : "New conversation"}
      </Button>

      {createMutation.isError ? (
        <p role="alert" className="text-xs font-medium text-destructive">
          {createMutation.error instanceof ApiError
            ? createMutation.error.message
            : "Failed to create conversation."}
        </p>
      ) : null}

      {query.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading conversations...</p>
      ) : null}

      {query.isError ? (
        <p role="alert" className="text-sm font-medium text-destructive">
          {query.error instanceof ApiError ? query.error.message : "Failed to load conversations."}
        </p>
      ) : null}

      {!query.isLoading && !query.isError && conversations.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No conversations yet. Start one to ask a question.
        </p>
      ) : null}

      <ul className="flex-1 space-y-1 overflow-y-auto">
        {conversations.map((conversation) => (
          <li key={conversation.id}>
            <button
              type="button"
              onClick={() => onSelectConversation(conversation.id)}
              aria-current={selectedConversationId === conversation.id ? "true" : undefined}
              className={cn(
                "w-full truncate rounded-md px-3 py-2 text-left text-sm transition-colors",
                selectedConversationId === conversation.id
                  ? "bg-secondary text-secondary-foreground"
                  : "text-muted-foreground hover:bg-secondary/60 hover:text-secondary-foreground",
              )}
            >
              {conversation.title ?? "Untitled conversation"}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
