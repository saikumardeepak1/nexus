import type { Metadata } from "next";

import { ChatWorkspace } from "@/components/chat/ChatWorkspace";

export const metadata: Metadata = {
  title: "Chat | Nexus",
};

export default function ChatPage() {
  return (
    <div className="flex h-full flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold">Chat</h1>
        <p className="text-sm text-muted-foreground">
          Ask a question and get a cited answer from your organization&apos;s documents.
        </p>
      </div>
      <ChatWorkspace />
    </div>
  );
}
