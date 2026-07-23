import { describe, expect, it } from "vitest";

import { DOCUMENT_POLL_INTERVAL_MS, documentsRefetchInterval } from "./documents";
import type { DocumentResponse } from "./types";

function makeDocument(status: DocumentResponse["status"]): DocumentResponse {
  return {
    id: `doc-${status}`,
    organization_id: "org-1",
    filename: "notes.txt",
    status,
    page_count: null,
    error_detail: null,
    created_at: "2026-07-23T00:00:00Z",
  };
}

describe("documentsRefetchInterval", () => {
  it("does not poll when there are no documents", () => {
    expect(documentsRefetchInterval(undefined)).toBe(false);
    expect(documentsRefetchInterval([])).toBe(false);
  });

  it("polls while any document is queued or processing", () => {
    expect(documentsRefetchInterval([makeDocument("queued")])).toBe(DOCUMENT_POLL_INTERVAL_MS);
    expect(documentsRefetchInterval([makeDocument("processing")])).toBe(DOCUMENT_POLL_INTERVAL_MS);
    expect(
      documentsRefetchInterval([makeDocument("ready"), makeDocument("processing")]),
    ).toBe(DOCUMENT_POLL_INTERVAL_MS);
  });

  it("stops polling once every document has reached a terminal state", () => {
    expect(documentsRefetchInterval([makeDocument("ready")])).toBe(false);
    expect(documentsRefetchInterval([makeDocument("failed")])).toBe(false);
    expect(
      documentsRefetchInterval([makeDocument("ready"), makeDocument("failed")]),
    ).toBe(false);
  });
});
