"use client";

import { useState } from "react";

import { ConversationSidebar } from "./ConversationSidebar";
import { ConversationThread } from "./ConversationThread";

/**
 * Top-level chat feature: a conversation picker/sidebar plus the selected
 * conversation's thread. Keyed on `selectedConversationId` so switching
 * conversations remounts `ConversationThread` fresh (its in-progress
 * streaming state belongs to one specific conversation and must not leak
 * into another).
 */
export function ChatWorkspace() {
  const [selectedConversationId, setSelectedConversationId] = useState<string | null>(null);

  return (
    <div className="grid min-h-[70vh] grid-cols-[240px_1fr] gap-6">
      <aside className="border-r pr-4">
        <ConversationSidebar
          selectedConversationId={selectedConversationId}
          onSelectConversation={setSelectedConversationId}
        />
      </aside>
      <section className="flex min-h-0 flex-col">
        {selectedConversationId ? (
          <ConversationThread key={selectedConversationId} conversationId={selectedConversationId} />
        ) : (
          <div className="flex flex-1 items-center justify-center text-center">
            <p className="max-w-sm text-sm text-muted-foreground">
              Select a conversation from the sidebar, or start a new one, to begin asking
              questions.
            </p>
          </div>
        )}
      </section>
    </div>
  );
}
