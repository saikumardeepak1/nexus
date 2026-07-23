"use client";

import { useQuery } from "@tanstack/react-query";

import { ApiError, getChunk } from "@/lib/api-client";
import { chunkQueryKey } from "@/lib/chat";

interface CitationPanelProps {
  chunkId: string;
}

/**
 * Source panel shown when a citation marker is clicked: resolves the
 * marker's chunk id to its originating document filename and the chunk's
 * own text (see apps/api/app/api/chunks.py, added for this issue's citation
 * display requirement). Only fetches once actually rendered (the parent
 * only mounts this when a marker is open), so browsing a long answer with
 * many citations never fetches a chunk the user never clicked.
 */
export function CitationPanel({ chunkId }: CitationPanelProps) {
  const query = useQuery({
    queryKey: chunkQueryKey(chunkId),
    queryFn: () => getChunk(chunkId),
  });

  if (query.isLoading) {
    return <p className="text-xs text-muted-foreground">Loading source...</p>;
  }

  if (query.isError) {
    return (
      <p role="alert" className="text-xs font-medium text-destructive">
        {query.error instanceof ApiError ? query.error.message : "Failed to load source."}
      </p>
    );
  }

  const chunk = query.data;
  if (!chunk) return null;

  return (
    <div className="space-y-1">
      <p className="text-xs font-semibold">
        {chunk.filename}
        {chunk.page_number !== null ? ` (page ${chunk.page_number})` : ""}
      </p>
      <p className="text-xs text-muted-foreground">{chunk.content}</p>
    </div>
  );
}
