import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Chat | Nexus",
};

export default function ChatPage() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-2 text-center">
      <h1 className="text-xl font-semibold">Chat</h1>
      <p className="max-w-sm text-sm text-muted-foreground">
        No conversations yet. Ask a question once the chat interface ships in a future update.
      </p>
    </div>
  );
}
