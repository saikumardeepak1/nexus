import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DocumentList } from "./DocumentList";
import { DocumentUploadForm } from "./DocumentUploadForm";

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
    uploadDocument: vi.fn(),
    deleteDocument: vi.fn(),
  };
});

import { listDocuments, uploadDocument } from "@/lib/api-client";

function renderWithQueryClient(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

function selectFile(input: HTMLElement, file: File) {
  fireEvent.change(input, { target: { files: [file] } });
}

describe("DocumentUploadForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("submits the selected file to the upload endpoint", async () => {
    vi.mocked(uploadDocument).mockResolvedValueOnce({
      id: "1",
      organization_id: "org-1",
      filename: "policy.pdf",
      status: "queued",
      page_count: null,
      error_detail: null,
      created_at: "2026-07-23T00:00:00Z",
    });

    renderWithQueryClient(<DocumentUploadForm />);

    const file = new File(["hello"], "policy.pdf", { type: "application/pdf" });
    selectFile(screen.getByLabelText(/upload a document/i), file);
    fireEvent.click(screen.getByRole("button", { name: /upload/i }));

    await waitFor(() => expect(uploadDocument).toHaveBeenCalledWith(file));
  });

  it("shows the newly uploaded document in the list immediately with status=queued, without a manual refresh", async () => {
    vi.mocked(listDocuments).mockResolvedValueOnce({ documents: [] });
    vi.mocked(uploadDocument).mockResolvedValueOnce({
      id: "1",
      organization_id: "org-1",
      filename: "policy.pdf",
      status: "queued",
      page_count: null,
      error_detail: null,
      created_at: "2026-07-23T00:00:00Z",
    });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <DocumentUploadForm />
        <DocumentList />
      </QueryClientProvider>,
    );

    // Initial (empty) list has loaded.
    expect(await screen.findByText(/no documents yet/i)).toBeInTheDocument();

    const file = new File(["hello"], "policy.pdf", { type: "application/pdf" });
    selectFile(screen.getByLabelText(/upload a document/i), file);
    fireEvent.click(screen.getByRole("button", { name: /upload/i }));

    expect(await screen.findByText("policy.pdf")).toBeInTheDocument();
    expect(screen.getByText("Queued")).toBeInTheDocument();
    // listDocuments was only ever called for the initial load: the new
    // document appeared via the shared query cache, not a refetch.
    expect(listDocuments).toHaveBeenCalledTimes(1);
  });

  it("shows a visible error message when the upload fails", async () => {
    const { ApiError } = await import("@/lib/api-client");
    vi.mocked(uploadDocument).mockRejectedValueOnce(
      new ApiError("Unsupported file type '.png'.", 415),
    );

    renderWithQueryClient(<DocumentUploadForm />);

    const file = new File(["hello"], "image.png", { type: "image/png" });
    selectFile(screen.getByLabelText(/upload a document/i), file);
    fireEvent.click(screen.getByRole("button", { name: /upload/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Unsupported file type");
  });
});
