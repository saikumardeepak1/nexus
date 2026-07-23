import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConversationSidebar } from "./ConversationSidebar";

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
    listConversations: vi.fn(),
    createConversation: vi.fn(),
  };
});

import { createConversation, listConversations } from "@/lib/api-client";

function renderWithQueryClient(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

describe("ConversationSidebar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows an empty state when there are no conversations", async () => {
    vi.mocked(listConversations).mockResolvedValueOnce({ conversations: [] });

    renderWithQueryClient(
      <ConversationSidebar selectedConversationId={null} onSelectConversation={vi.fn()} />,
    );

    expect(await screen.findByText(/no conversations yet/i)).toBeInTheDocument();
  });

  it("lists every conversation and calls onSelectConversation when clicked", async () => {
    vi.mocked(listConversations).mockResolvedValueOnce({
      conversations: [
        {
          id: "conv-1",
          organization_id: "org-1",
          user_id: "user-1",
          title: "Vacation questions",
          created_at: "2026-07-23T00:00:00Z",
        },
      ],
    });
    const onSelect = vi.fn();

    renderWithQueryClient(
      <ConversationSidebar selectedConversationId={null} onSelectConversation={onSelect} />,
    );

    const item = await screen.findByText("Vacation questions");
    fireEvent.click(item);

    expect(onSelect).toHaveBeenCalledWith("conv-1");
  });

  it("creates a new conversation and selects it immediately", async () => {
    vi.mocked(listConversations).mockResolvedValueOnce({ conversations: [] });
    vi.mocked(createConversation).mockResolvedValueOnce({
      id: "conv-new",
      organization_id: "org-1",
      user_id: "user-1",
      title: null,
      created_at: "2026-07-23T00:00:00Z",
    });
    const onSelect = vi.fn();

    renderWithQueryClient(
      <ConversationSidebar selectedConversationId={null} onSelectConversation={onSelect} />,
    );

    await screen.findByText(/no conversations yet/i);
    fireEvent.click(screen.getByRole("button", { name: /new conversation/i }));

    await waitFor(() => expect(onSelect).toHaveBeenCalledWith("conv-new"));
    expect(await screen.findByText("Untitled conversation")).toBeInTheDocument();
  });

  it("shows an error message when loading conversations fails", async () => {
    const { ApiError } = await import("@/lib/api-client");
    vi.mocked(listConversations).mockRejectedValueOnce(new ApiError("Network error", 500));

    renderWithQueryClient(
      <ConversationSidebar selectedConversationId={null} onSelectConversation={vi.fn()} />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("Network error");
  });
});
