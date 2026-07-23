import type { CitationEvent, CitationResponse } from "./types";

/** Matches a bracketed, 1-indexed citation marker like `[1]` or `[12]`
 * anywhere in a message's text -- mirrors
 * apps/api/app/services/generation_service.py's `_CITATION_MARKER_PATTERN`. */
const MARKER_PATTERN = /\[(\d+)\]/g;

function hasMarker(
  citation: CitationEvent | CitationResponse,
): citation is CitationEvent {
  return "marker" in citation;
}

/**
 * Map each citation marker number found in `content` (the `n` in `[n]`) to
 * the chunk id it refers to.
 *
 * Two citation shapes reach this function (see lib/types.ts):
 *
 * - `CitationEvent`, from a message that just streamed (the SSE `done`
 *   event, see apps/api/app/api/conversations.py), carries its own
 *   `marker` field -- each citation maps to its `chunk_id` directly, no
 *   ambiguity, since the backend already resolved it.
 * - `CitationResponse`, from conversation history (`GET
 *   /v1/conversations/{id}`), does not carry `marker`: the persisted
 *   `Citation` row only stores `chunk_id` and `relevance_score` (see
 *   apps/api/app/models/citation.py), not the marker number a live
 *   generation call computes. As a fallback for that case, this pairs each
 *   `[n]` occurrence in the message text, in the order it appears, with
 *   the citations array, in the order the API returns it. That pairing is
 *   correct because the backend persists one `Citation` row per marker
 *   occurrence in exactly the order `generation_service.parse_citations`
 *   found them in the text (left to right) and returns them in that same
 *   order on a fresh, unmodified select -- see the PR description for the
 *   full write-up of this scoping decision and its one assumption.
 */
export function buildMarkerToChunkMap(
  content: string,
  citations: Array<CitationEvent | CitationResponse>,
): Map<number, string> {
  const map = new Map<number, string>();
  const withMarker = citations.filter(hasMarker);

  if (withMarker.length > 0) {
    for (const citation of withMarker) {
      map.set(citation.marker, citation.chunk_id);
    }
    return map;
  }

  const occurrences = Array.from(content.matchAll(MARKER_PATTERN));
  occurrences.forEach((match, index) => {
    const citation = citations[index];
    if (citation) {
      map.set(Number(match[1]), citation.chunk_id);
    }
  });
  return map;
}

export interface ContentSegment {
  type: "text" | "marker";
  key: string;
  value: string;
  marker?: number;
  chunkId?: string;
}

/**
 * Split `content` into alternating text/marker segments for rendering. A
 * `[n]` marker becomes a `"marker"` segment (with its resolved `chunkId`)
 * only if `markerMap` actually resolves it; an unresolved marker (e.g. a
 * model hallucination that never made it into the citations list, see
 * `generation_service.parse_citations`) renders as plain text instead of a
 * dead clickable element.
 */
export function splitContentIntoSegments(
  content: string,
  markerMap: Map<number, string>,
): ContentSegment[] {
  const segments: ContentSegment[] = [];
  let lastIndex = 0;
  let segmentIndex = 0;

  for (const match of content.matchAll(MARKER_PATTERN)) {
    const index = match.index ?? 0;
    if (index > lastIndex) {
      segments.push({
        type: "text",
        key: `text-${segmentIndex++}`,
        value: content.slice(lastIndex, index),
      });
    }

    const marker = Number(match[1]);
    const chunkId = markerMap.get(marker);
    if (chunkId) {
      segments.push({
        type: "marker",
        key: `marker-${segmentIndex++}`,
        value: match[0],
        marker,
        chunkId,
      });
    } else {
      segments.push({
        type: "text",
        key: `text-${segmentIndex++}`,
        value: match[0],
      });
    }
    lastIndex = index + match[0].length;
  }

  if (lastIndex < content.length) {
    segments.push({
      type: "text",
      key: `text-${segmentIndex++}`,
      value: content.slice(lastIndex),
    });
  }

  return segments;
}
