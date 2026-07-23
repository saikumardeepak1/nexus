import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DocumentList } from "./DocumentList";

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
    listDocuments: vi.fn(),
    deleteDocument: vi.fn(),
  };
});

import { deleteDocument, listDocuments } from "@/lib/api-client";

function renderWithQueryClient(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

const baseDocument = {
  organization_id: "org-1",
  page_count: null,
  error_detail: null,
  created_at: "2026-07-23T00:00:00Z",
};

describe("DocumentList", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders every document with its filename and status badge", async () => {
    vi.mocked(listDocuments).mockResolvedValueOnce({
      documents: [
        { ...baseDocument, id: "1", filename: "policy.pdf", status: "queued" },
        { ...baseDocument, id: "2", filename: "runbook.txt", status: "processing" },
        { ...baseDocument, id: "3", filename: "handbook.pdf", status: "ready" },
      ],
    });

    renderWithQueryClient(<DocumentList />);

    expect(await screen.findByText("policy.pdf")).toBeInTheDocument();
    expect(screen.getByText("runbook.txt")).toBeInTheDocument();
    expect(screen.getByText("handbook.pdf")).toBeInTheDocument();
    expect(screen.getByText("Queued")).toBeInTheDocument();
    expect(screen.getByText("Processing")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
  });

  it("shows the error detail for a failed document", async () => {
    vi.mocked(listDocuments).mockResolvedValueOnce({
      documents: [
        {
          ...baseDocument,
          id: "1",
          filename: "corrupt.pdf",
          status: "failed",
          error_detail: "Could not parse PDF: unexpected end of file",
        },
      ],
    });

    renderWithQueryClient(<DocumentList />);

    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(
      screen.getByText("Could not parse PDF: unexpected end of file"),
    ).toBeInTheDocument();
  });

  it("shows an empty state when there are no documents", async () => {
    vi.mocked(listDocuments).mockResolvedValueOnce({ documents: [] });

    renderWithQueryClient(<DocumentList />);

    expect(await screen.findByText(/no documents yet/i)).toBeInTheDocument();
  });

  it("calls the delete endpoint and removes the row on success", async () => {
    vi.mocked(listDocuments).mockResolvedValueOnce({
      documents: [{ ...baseDocument, id: "1", filename: "policy.pdf", status: "ready" }],
    });
    vi.mocked(deleteDocument).mockResolvedValueOnce(undefined);

    renderWithQueryClient(<DocumentList />);

    await screen.findByText("policy.pdf");
    fireEvent.click(screen.getByRole("button", { name: /delete policy\.pdf/i }));

    await waitFor(() => expect(deleteDocument).toHaveBeenCalledWith("1"));
    await waitFor(() => expect(screen.queryByText("policy.pdf")).not.toBeInTheDocument());
  });
});
