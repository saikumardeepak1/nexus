import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConversationThread } from "./ConversationThread";

vi.mock("@/lib/api-client", () => {
  class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }
  return {
    ApiError,
    getConversation: vi.fn(),
    streamMessage: vi.fn(),
    getChunk: vi.fn(),
  };
});

import { getChunk, getConversation, streamMessage } from "@/lib/api-client";

function renderWithQueryClient(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

/** A promise the test controls the resolution of, so a mocked streamMessage
 * can be paused mid-stream and resumed one delta at a time -- the
 * deterministic way to prove intermediate render states exist, rather than
 * racing against React's own microtask flushing. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const emptyConversation = {
  id: "conv-1",
  organization_id: "org-1",
  user_id: "user-1",
  title: "PTO questions",
  created_at: "2026-07-23T00:00:00Z",
  messages: [],
};

async function sendQuestion(question: string) {
  fireEvent.change(screen.getByLabelText(/message/i), { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: /^send$/i }));
}

/**
 * Matches a <p> whose full (recursive) text content equals `expected`,
 * trimmed. Message text renders as multiple child nodes when it contains
 * citation markers (plain text spans interleaved with marker buttons -- see
 * MessageContent/lib/citations.ts), so testing-library's default text
 * matcher (which only looks at an element's own direct text-node children,
 * not nested elements) never matches the concatenated whole; this walks
 * `element.textContent` instead, restricted to `<p>` so it doesn't also
 * match an ancestor wrapper div that happens to have identical text because
 * the `<p>` is its only child.
 */
function byTrimmedText(expected: string) {
  return (_content: string, element: Element | null) =>
    element?.tagName === "P" && element.textContent?.trim() === expected;
}

describe("ConversationThread", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows an empty state and loading conversation, then the composer once history loads", async () => {
    vi.mocked(getConversation).mockResolvedValueOnce(emptyConversation);

    renderWithQueryClient(<ConversationThread conversationId="conv-1" />);

    expect(await screen.findByText(/no messages yet/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/message/i)).toBeInTheDocument();
  });

  it("loads and renders full message history on mount (simulating a reload of the page)", async () => {
    vi.mocked(getConversation).mockResolvedValueOnce({
      ...emptyConversation,
      messages: [
        {
          id: "m1",
          conversation_id: "conv-1",
          role: "user",
          content: "What is our standard PTO policy?",
          created_at: "2026-07-23T00:00:00Z",
          citations: [],
        },
        {
          id: "m2",
          conversation_id: "conv-1",
          role: "assistant",
          content: "Standard PTO is 15 days per year [1].",
          created_at: "2026-07-23T00:00:01Z",
          citations: [{ id: "cit-1", chunk_id: "chunk-1", relevance_score: 0.92 }],
        },
      ],
    });

    renderWithQueryClient(<ConversationThread conversationId="conv-1" />);

    expect(await screen.findByText("What is our standard PTO policy?")).toBeInTheDocument();
    expect(
      screen.getByText(byTrimmedText("Standard PTO is 15 days per year [1].")),
    ).toBeInTheDocument();
    // The historical citation (no `marker` field) still resolves via the
    // text-occurrence fallback (see lib/citations.ts) into a clickable marker.
    expect(screen.getByRole("button", { name: /show source for citation 1/i })).toBeInTheDocument();
  });

  it("sends a message: shows the user's message immediately, streams deltas incrementally, then finalizes with citations", async () => {
    vi.mocked(getConversation).mockResolvedValueOnce(emptyConversation);

    const gate1 = deferred<void>();
    const gate2 = deferred<void>();
    vi.mocked(streamMessage).mockImplementation(async (_id, _content, handlers) => {
      handlers.onDelta("Standard ");
      await gate1.promise;
      handlers.onDelta("PTO is 15 days per year ");
      await gate2.promise;
      handlers.onDelta("[1].");
      handlers.onDone({
        message_id: "msg-assistant-1",
        citations: [{ chunk_id: "chunk-1", relevance_score: 0.92, marker: 1, text_position: 30 }],
      });
    });

    renderWithQueryClient(<ConversationThread conversationId="conv-1" />);
    await screen.findByText(/no messages yet/i);

    await sendQuestion("What is our standard PTO policy?");

    // The user's message renders immediately, before any assistant text exists.
    expect(await screen.findByText("What is our standard PTO policy?")).toBeInTheDocument();

    // First delta rendered on its own -- proves incremental rendering, not a
    // single buffered update after the stream ends.
    expect(await screen.findByText(byTrimmedText("Standard"))).toBeInTheDocument();

    gate1.resolve();
    await waitFor(() =>
      expect(
        screen.getByText(byTrimmedText("Standard PTO is 15 days per year")),
      ).toBeInTheDocument(),
    );
    // Not yet finalized: no citation marker button exists until `done` fires.
    expect(screen.queryByRole("button", { name: /show source for citation/i })).not.toBeInTheDocument();

    gate2.resolve();

    // Terminal event finalizes the message with its citation marker rendered
    // as a clickable element.
    const marker = await screen.findByRole("button", { name: /show source for citation 1/i });
    expect(marker).toBeInTheDocument();
    expect(
      screen.getByText(byTrimmedText("Standard PTO is 15 days per year [1].")),
    ).toBeInTheDocument();

    // The "generating" state is gone and the composer is usable again.
    expect(screen.queryByText(/generating/i)).not.toBeInTheDocument();
    // The composer's input re-enables once sending finishes (the send
    // button itself stays disabled here too, but only because the draft is
    // now empty, not because a send is in flight).
    expect(screen.getByLabelText(/message/i)).not.toBeDisabled();
  });

  it("clicking a citation marker shows the source panel with the resolved document and chunk text", async () => {
    vi.mocked(getConversation).mockResolvedValueOnce({
      ...emptyConversation,
      messages: [
        {
          id: "m2",
          conversation_id: "conv-1",
          role: "assistant",
          content: "Standard PTO is 15 days per year [1].",
          created_at: "2026-07-23T00:00:01Z",
          citations: [{ id: "cit-1", chunk_id: "chunk-1", relevance_score: 0.92 }],
        },
      ],
    });
    vi.mocked(getChunk).mockResolvedValueOnce({
      id: "chunk-1",
      document_id: "doc-1",
      filename: "handbook.pdf",
      content: "Standard PTO is 15 days per year for all full-time employees.",
      page_number: 3,
    });

    renderWithQueryClient(<ConversationThread conversationId="conv-1" />);

    const marker = await screen.findByRole("button", { name: /show source for citation 1/i });
    fireEvent.click(marker);

    expect(await screen.findByText("handbook.pdf (page 3)")).toBeInTheDocument();
    expect(
      screen.getByText("Standard PTO is 15 days per year for all full-time employees."),
    ).toBeInTheDocument();
    expect(getChunk).toHaveBeenCalledWith("chunk-1");
  });

  it("shows an error message when the stream fails, and lets the user try again", async () => {
    const { ApiError } = await import("@/lib/api-client");
    vi.mocked(getConversation).mockResolvedValueOnce(emptyConversation);
    vi.mocked(streamMessage).mockRejectedValueOnce(new ApiError("Network error mid-stream", 502));

    renderWithQueryClient(<ConversationThread conversationId="conv-1" />);
    await screen.findByText(/no messages yet/i);

    await sendQuestion("What is our standard PTO policy?");

    expect(await screen.findByRole("alert")).toHaveTextContent("Network error mid-stream");
    // No half-finished assistant message lingers after the failure.
    expect(screen.queryByText(/generating/i)).not.toBeInTheDocument();
    // The composer's input re-enables once sending finishes (the send
    // button itself stays disabled here too, but only because the draft is
    // now empty, not because a send is in flight).
    expect(screen.getByLabelText(/message/i)).not.toBeDisabled();
  });
});
