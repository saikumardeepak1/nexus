import { describe, expect, it } from "vitest";

import { buildMarkerToChunkMap, splitContentIntoSegments } from "./citations";
import type { CitationEvent, CitationResponse } from "./types";

describe("buildMarkerToChunkMap", () => {
  it("maps directly from CitationEvent's own marker field", () => {
    const citations: CitationEvent[] = [
      { chunk_id: "chunk-1", relevance_score: 0.9, marker: 1, text_position: 10 },
      { chunk_id: "chunk-2", relevance_score: 0.8, marker: 2, text_position: 20 },
    ];

    const map = buildMarkerToChunkMap("Some text [1] and more [2].", citations);

    expect(map.get(1)).toBe("chunk-1");
    expect(map.get(2)).toBe("chunk-2");
  });

  it("falls back to pairing text occurrences with citations in order when marker is absent", () => {
    const citations: CitationResponse[] = [
      { id: "c1", chunk_id: "chunk-a", relevance_score: 0.9 },
      { id: "c2", chunk_id: "chunk-b", relevance_score: 0.7 },
    ];

    const map = buildMarkerToChunkMap("First claim [1]. Second claim [2].", citations);

    expect(map.get(1)).toBe("chunk-a");
    expect(map.get(2)).toBe("chunk-b");
  });

  it("handles a repeated marker resolving to the same chunk", () => {
    const citations: CitationResponse[] = [
      { id: "c1", chunk_id: "chunk-a", relevance_score: 0.9 },
      { id: "c2", chunk_id: "chunk-a", relevance_score: 0.9 },
    ];

    const map = buildMarkerToChunkMap("Claim [1]. Same claim again [1].", citations);

    expect(map.get(1)).toBe("chunk-a");
  });

  it("returns an empty map when there are no citations", () => {
    const map = buildMarkerToChunkMap("No citations here.", []);
    expect(map.size).toBe(0);
  });
});

describe("splitContentIntoSegments", () => {
  it("splits text around a resolved marker into text/marker segments", () => {
    const map = new Map([[1, "chunk-1"]]);
    const segments = splitContentIntoSegments("PTO is 15 days [1] per year.", map);

    expect(segments.map((segment) => segment.type)).toEqual(["text", "marker", "text"]);
    expect(segments[1]).toMatchObject({ value: "[1]", marker: 1, chunkId: "chunk-1" });
  });

  it("renders an unresolved marker as plain text", () => {
    const map = new Map<number, string>();
    const segments = splitContentIntoSegments("A hallucinated claim [7].", map);

    expect(segments.every((segment) => segment.type === "text")).toBe(true);
    expect(segments.map((segment) => segment.value).join("")).toBe("A hallucinated claim [7].");
  });

  it("returns a single text segment for content with no markers", () => {
    const segments = splitContentIntoSegments("Just plain text.", new Map());
    expect(segments).toHaveLength(1);
    expect(segments[0]).toMatchObject({ type: "text", value: "Just plain text." });
  });
});
