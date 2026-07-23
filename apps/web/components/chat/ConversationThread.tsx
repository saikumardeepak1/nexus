"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { MessageContent } from "./MessageContent";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, getConversation, streamMessage } from "@/lib/api-client";
import { conversationQueryKey } from "@/lib/chat";
import type { ConversationDetailResponse, MessageResponse } from "@/lib/types";

interface ConversationThreadProps {
  conversationId: string;
}

/**
 * A single conversation's message history plus the composer that sends new
 * ones. History loads via GET /v1/conversations/{id} (so a reload of the
 * page, or re-selecting this conversation later, shows the exact same
 * messages -- the acceptance criteria's history-persistence requirement).
 * Sending a message POSTs to /v1/conversations/{id}/messages and consumes
 * its SSE stream (see lib/api-client.ts's streamMessage): the user's
 * message appears immediately, each `delta` event appends to the
 * in-progress assistant message as it arrives, and the terminal `done`
 * event finalizes it with real citations, at which point both messages are
 * folded into the same TanStack Query cache entry the history view reads,
 * so no separate render path exists for "just sent" vs. "loaded from
 * history" messages once streaming completes.
 */
export function ConversationThread({ conversationId }: ConversationThreadProps) {
  const queryClient = useQueryClient();

  const [draft, setDraft] = useState("");
  const [pendingUserContent, setPendingUserContent] = useState<string | null>(null);
  const [streamingContent, setStreamingContent] = useState<string | null>(null);
  const [isSending, setIsSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  const query = useQuery({
    queryKey: conversationQueryKey(conversationId),
    queryFn: () => getConversation(conversationId),
  });

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || isSending) return;

    setDraft("");
    setSendError(null);
    setPendingUserContent(content);
    setStreamingContent("");
    setIsSending(true);

    // Accumulated outside React state: `onDone` needs the final text the
    // instant it fires, and reading back the latest `streamingContent`
    // state inside that closure would risk a stale value from the render
    // the closure was created in.
    let accumulated = "";

    try {
      await streamMessage(conversationId, content, {
        onDelta: (text) => {
          accumulated += text;
          setStreamingContent(accumulated);
        },
        onDone: (data) => {
          const now = new Date().toISOString();
          queryClient.setQueryData<ConversationDetailResponse>(
            conversationQueryKey(conversationId),
            (old) => {
              const base: ConversationDetailResponse = old ?? {
                id: conversationId,
                organization_id: "",
                user_id: "",
                title: null,
                created_at: now,
                messages: [],
              };
              const userMessage: MessageResponse = {
                id: `local-user-${data.message_id}`,
                conversation_id: conversationId,
                role: "user",
                content,
                created_at: now,
                citations: [],
              };
              const assistantMessage: MessageResponse = {
                id: data.message_id,
                conversation_id: conversationId,
                role: "assistant",
                content: accumulated,
                created_at: now,
                citations: data.citations,
              };
              return {
                ...base,
                messages: [...base.messages, userMessage, assistantMessage],
              };
            },
          );
          setPendingUserContent(null);
          setStreamingContent(null);
        },
      });
    } catch (error) {
      setSendError(
        error instanceof ApiError
          ? error.message
          : "Something went wrong while generating a response.",
      );
      setPendingUserContent(null);
      setStreamingContent(null);
    } finally {
      setIsSending(false);
    }
  }

  if (query.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading conversation...</p>;
  }

  if (query.isError) {
    return (
      <p role="alert" className="text-sm font-medium text-destructive">
        {query.error instanceof ApiError ? query.error.message : "Failed to load conversation."}
      </p>
    );
  }

  const messages = query.data?.messages ?? [];
  const isEmpty = messages.length === 0 && pendingUserContent === null;

  return (
    <div className="flex h-full flex-col gap-4">
      <div className="flex-1 space-y-4 overflow-y-auto">
        {isEmpty ? (
          <p className="text-sm text-muted-foreground">
            No messages yet. Ask a question to get started.
          </p>
        ) : null}

        {messages.map((message) => (
          <div key={message.id} data-role={message.role}>
            <p className="mb-1 text-xs font-medium uppercase text-muted-foreground">
              {message.role === "user" ? "You" : "Nexus"}
            </p>
            <MessageContent content={message.content} citations={message.citations} />
          </div>
        ))}

        {pendingUserContent !== null ? (
          <div data-role="user">
            <p className="mb-1 text-xs font-medium uppercase text-muted-foreground">You</p>
            <p className="whitespace-pre-wrap text-sm">{pendingUserContent}</p>
          </div>
        ) : null}

        {streamingContent !== null ? (
          <div data-role="assistant" aria-live="polite">
            <p className="mb-1 text-xs font-medium uppercase text-muted-foreground">Nexus</p>
            <p className="whitespace-pre-wrap text-sm">{streamingContent}</p>
            {isSending ? (
              <p className="mt-1 text-xs text-muted-foreground">Generating...</p>
            ) : null}
          </div>
        ) : null}
      </div>

      {sendError ? (
        <p role="alert" className="text-sm font-medium text-destructive">
          {sendError}
        </p>
      ) : null}

      <form onSubmit={handleSubmit} className="flex gap-2">
        <Input
          aria-label="Message"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Ask a question..."
          disabled={isSending}
        />
        <Button type="submit" disabled={isSending || draft.trim().length === 0}>
          {isSending ? "Sending..." : "Send"}
        </Button>
      </form>
    </div>
  );
}
