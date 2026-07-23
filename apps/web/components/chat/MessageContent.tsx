"use client";

import { useState } from "react";

import { CitationPanel } from "./CitationPanel";
import { buildMarkerToChunkMap, splitContentIntoSegments } from "@/lib/citations";
import type { CitationEvent, CitationResponse } from "@/lib/types";

interface MessageContentProps {
  content: string;
  citations: Array<CitationEvent | CitationResponse>;
}

/**
 * Renders a message's text with its `[n]` citation markers as clickable
 * elements. Clicking a marker toggles a source panel showing the resolved
 * document filename and chunk text (see CitationPanel), rendered inline
 * below the text rather than as a floating popover, since that needs no
 * positioning/portal logic to work correctly in both the app and its tests.
 */
export function MessageContent({ content, citations }: MessageContentProps) {
  const [openChunkId, setOpenChunkId] = useState<string | null>(null);

  const markerMap = buildMarkerToChunkMap(content, citations);
  const segments = splitContentIntoSegments(content, markerMap);

  return (
    <div>
      <p className="whitespace-pre-wrap text-sm">
        {segments.map((segment) =>
          segment.type === "marker" && segment.chunkId ? (
            <button
              key={segment.key}
              type="button"
              onClick={() =>
                setOpenChunkId((current) => (current === segment.chunkId ? null : (segment.chunkId as string)))
              }
              aria-expanded={openChunkId === segment.chunkId}
              aria-label={`Show source for citation ${segment.marker}`}
              className="mx-0.5 inline-flex items-center rounded bg-secondary px-1 text-xs font-semibold text-secondary-foreground hover:bg-secondary/80"
            >
              {segment.value}
            </button>
          ) : (
            <span key={segment.key}>{segment.value}</span>
          ),
        )}
      </p>
      {openChunkId ? (
        <div className="mt-2 rounded-md border bg-muted/40 p-2">
          <CitationPanel chunkId={openChunkId} />
        </div>
      ) : null}
    </div>
  );
}
