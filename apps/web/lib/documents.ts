import type { DocumentResponse } from "./types";

/**
 * Shared TanStack Query key for the document list, used by every component
 * that reads or writes the list's cache entry (DocumentList, DocumentUploadForm)
 * so an upload or delete in one component is reflected in the others without
 * a manual refetch.
 */
export const DOCUMENTS_QUERY_KEY = ["documents"] as const;

/** Poll every 3 seconds while ingestion is in flight. */
export const DOCUMENT_POLL_INTERVAL_MS = 3000;

const TERMINAL_STATUSES: ReadonlySet<string> = new Set(["ready", "failed"]);

export function isTerminalStatus(status: string): boolean {
  return TERMINAL_STATUSES.has(status);
}

/**
 * Decide the document list's TanStack Query `refetchInterval`: keep polling
 * while any listed document is still queued/processing, and stop once every
 * document has reached a terminal state (ready or failed), so the page
 * doesn't keep polling forever once ingestion has finished for everything
 * currently listed. A fresh upload adds a new `queued` document to the
 * cache, so the next time this is evaluated it resumes polling on its own.
 */
export function documentsRefetchInterval(documents: DocumentResponse[] | undefined): number | false {
  if (!documents || documents.length === 0) return false;
  return documents.every((document) => isTerminalStatus(document.status))
    ? false
    : DOCUMENT_POLL_INTERVAL_MS;
}
