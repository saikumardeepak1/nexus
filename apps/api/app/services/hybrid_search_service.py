"""Hybrid (dense + lexical) retrieval, fused via Reciprocal Rank Fusion (see
docs/TDD.md section 3.2 and issue #11).

This module is the first place dense (`vector_store_service`) and lexical
(`lexical_search_service`) search results meet. It runs both concurrently,
combines them into a single ranked candidate list, and hands that list back
to whatever caller needs it next (`reranking_service`, issue #12, already
built separately; the RAG generation flow, Milestone 3, not started). This
module does not call either of those -- it only produces the fused candidate
list they will eventually consume.

Reciprocal Rank Fusion (RRF)
----------------------------
Each of the two underlying searches produces its own ranked list with its
own, incomparable notion of "score" (`ts_rank`'s value has no fixed range
and no relationship to Qdrant's cosine similarity). Rather than trying to
normalize and average two differently-scaled scores, RRF only looks at
*rank position* within each list: a chunk at 1-indexed rank ``r`` in a list
contributes ``1 / (k + r)`` to that chunk's fused score, and a chunk's total
fused score is the sum of that contribution across every list it appears
in. A chunk missing from a list simply contributes nothing from that list,
rather than being penalized with a fabricated worst-case score. This is
also why a chunk appearing in both lists tends to outrank one appearing in
only a single list even if that single appearance was a rank-1 match:
two moderate rank contributions usually sum to more than one top-rank
contribution alone (see the fused-ranking unit tests below for a worked
example).

``k`` is a smoothing constant: it dampens the influence of rank 1 vs. rank
2 (without it, rank 1 would score twice as high as rank 2, which
overweights small, often noisy differences at the very top of a list) and
ensures no chunk ever gets a zero or undefined contribution. ``k = 60`` is
the standard default from the paper that introduced RRF (Cormack, Clarke,
and Buettcher, "Reciprocal Rank Fusion Outperforms Condorcet and
Individual Rank Learning Methods", SIGIR 2009); it is used here rather than
some other value because it's a well-established, empirically-validated
default and there is no project-specific evidence yet to justify deviating
from it.

Candidate pool size
--------------------
Each underlying search is asked for more than `limit` results
(`_CANDIDATE_POOL_SIZE`) before fusion, not just `limit`. RRF's whole value
is surfacing a chunk that ranks decently in *both* lists even if it isn't
the single best hit in either one -- if each search only returned the
final `limit`, fusion would have nothing more to work with than a plain
list-append would, and a chunk ranked, say, 7th in lexical search and 8th
in dense search (individually below a `limit=10` cutoff... on second
thought, easier illustration: ranked 12th in one list and 9th in the other,
both below a `limit=10` cutoff in isolation) could never be found even
though its combined signal makes it a strong candidate. Fetching
``max(limit * 4, 50)`` from each side gives fusion a meaningfully deeper
pool to draw overlap from while staying cheap (both queries are still
single, indexed lookups -- ``ts_rank`` over a GIN index and Qdrant's HNSW
search are both sublinear in corpus size, so a few dozen extra rows costs
essentially nothing extra per call), then the fused, sorted result is
truncated back down to `limit` before being returned.

Content resolution
-------------------
`LexicalSearchResult` already carries `content` (it's read straight out of
`chunks.content` in the same query). `VectorSearchResult` does not --
Qdrant's payload optionally carries `content` for debugging convenience
(see `vector_store_service.upsert_chunk`), but `search` never reads it back,
so a vector-only hit (a chunk dense search found but lexical search did not)
arrives here with no content at all. Since a reranker needs real text to
score (`RerankCandidate.content`), every result this module returns must
have it. The fusion step therefore prefers the lexical result's content
whenever a chunk_id appears in both lists (no extra work needed, it's
already in hand), and for chunk_ids that only ever appeared in the dense
list, `hybrid_search` does one small, organization-scoped Postgres lookup
by `chunk_id` to backfill `content`/`page_number` for just that handful of
rows -- never a per-candidate query, one batched `IN (...)` lookup for
however many vector-only hits made the fused top-`limit`.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Document
from app.services.embedding_service import embed_query
from app.services.lexical_search_service import LexicalSearchResult, lexical_search
from app.services.vector_store_service import VectorSearchResult
from app.services.vector_store_service import search as vector_search

# The standard RRF smoothing constant (see the "Reciprocal Rank Fusion" note
# in the module docstring for why 60, specifically, and not some other
# value).
RRF_K = 60

# How many results to request from *each* underlying search before fusing,
# not the final number returned (see the "Candidate pool size" note in the
# module docstring). Scales with the caller's requested `limit` so a caller
# asking for more final results also gets a deeper pool to fuse from, with a
# floor so a small `limit` (e.g. 3) doesn't starve fusion of candidates to
# work with.
_CANDIDATE_POOL_MULTIPLIER = 4
_MIN_CANDIDATE_POOL_SIZE = 50


@dataclass(frozen=True)
class HybridSearchResult:
    """One chunk surfaced by hybrid search, plus its fused RRF score.

    Deliberately small and independent of either underlying search's result
    shape, so a future caller (`reranking_service`, via a
    `RerankCandidate(chunk_id=str(r.chunk_id), content=r.content)` mapping)
    has a clean, stable contract to build on -- the same role
    `LexicalSearchResult` and `VectorSearchResult` fill for their own
    modules.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    content: str
    page_number: int | None
    fused_score: float


def _candidate_pool_size(limit: int) -> int:
    """How many results to ask each underlying search for, given a final
    `limit` (see the "Candidate pool size" note in the module docstring).
    """
    return max(limit * _CANDIDATE_POOL_MULTIPLIER, _MIN_CANDIDATE_POOL_SIZE)


def _rrf_scores(
    lexical_results: list[LexicalSearchResult],
    vector_results: list[VectorSearchResult],
    k: int = RRF_K,
) -> dict[uuid.UUID, float]:
    """The core RRF math: every chunk_id appearing in either list maps to
    the sum of ``1 / (k + rank)`` across whichever list(s) it appears in,
    `rank` being that chunk's 1-indexed position within that particular
    list. See the "Reciprocal Rank Fusion" note in the module docstring.
    """
    scores: dict[uuid.UUID, float] = {}
    for rank, lexical_result in enumerate(lexical_results, start=1):
        scores[lexical_result.chunk_id] = (
            scores.get(lexical_result.chunk_id, 0.0) + 1.0 / (k + rank)
        )
    for rank, vector_result in enumerate(vector_results, start=1):
        scores[vector_result.chunk_id] = (
            scores.get(vector_result.chunk_id, 0.0) + 1.0 / (k + rank)
        )
    return scores


def fuse_search_results(
    lexical_results: list[LexicalSearchResult],
    vector_results: list[VectorSearchResult],
    limit: int = 10,
    k: int = RRF_K,
) -> list[HybridSearchResult]:
    """Fuse a lexical and a dense result list into one ranked candidate list.

    Pure function: no database or Qdrant access, just the RRF math plus
    content resolution from whichever of the two input lists a chunk
    appeared in (see the "Content resolution" note in the module
    docstring). A chunk that appeared only in `vector_results` (and
    therefore has no content available here) is returned with
    `content = ""`; `hybrid_search` backfills that from Postgres before
    returning to its own caller, but this function's contract is fusion +
    ranking, testable entirely against hand-constructed input lists with no
    I/O at all.

    Args:
        lexical_results: `lexical_search_service.lexical_search`'s output
            (or an equivalent hand-built list, for tests), highest `rank`
            first.
        vector_results: `vector_store_service.search`'s output (or an
            equivalent hand-built list, for tests), highest `score` first.
        limit: maximum number of fused results to return.
        k: the RRF smoothing constant; defaults to `RRF_K`.

    Returns:
        Up to `limit` `HybridSearchResult`, highest fused score first.
    """
    if limit <= 0:
        return []

    scores = _rrf_scores(lexical_results, vector_results, k)
    ranked_chunk_ids = sorted(scores, key=lambda chunk_id: scores[chunk_id], reverse=True)

    lexical_by_id = {result.chunk_id: result for result in lexical_results}
    vector_by_id = {result.chunk_id: result for result in vector_results}

    fused: list[HybridSearchResult] = []
    for chunk_id in ranked_chunk_ids[:limit]:
        fused_score = scores[chunk_id]
        lexical_hit = lexical_by_id.get(chunk_id)
        if lexical_hit is not None:
            # Prefer the lexical result's content when a chunk appears in
            # both lists -- it's already in hand, no lookup needed.
            fused.append(
                HybridSearchResult(
                    chunk_id=chunk_id,
                    document_id=lexical_hit.document_id,
                    content=lexical_hit.content,
                    page_number=lexical_hit.page_number,
                    fused_score=fused_score,
                )
            )
            continue

        vector_hit = vector_by_id[chunk_id]
        fused.append(
            HybridSearchResult(
                chunk_id=chunk_id,
                document_id=vector_hit.document_id,
                content="",
                page_number=None,
                fused_score=fused_score,
            )
        )

    return fused


async def _fetch_content_by_chunk_id(
    session: AsyncSession,
    organization_id: uuid.UUID,
    chunk_ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[str, int | None]]:
    """Batched, organization-scoped Postgres lookup of `content`/`page_number`
    for `chunk_ids`, used to backfill vector-only hits (see the "Content
    resolution" note in the module docstring).

    Scoped to `organization_id` via the same `Chunk`-joined-to-`Document`
    pattern `lexical_search_service` uses, even though every `chunk_id`
    here already came from a `vector_store_service.search` call that was
    itself scoped to the same organization -- defense in depth against ever
    resolving another organization's chunk if that invariant were somehow
    violated upstream.
    """
    if not chunk_ids:
        return {}

    stmt = (
        select(Chunk.id, Chunk.content, Chunk.page_number)
        .join(Document, Chunk.document_id == Document.id)
        .where(Document.organization_id == organization_id)
        .where(Chunk.id.in_(chunk_ids))
    )
    result = await session.execute(stmt)
    return {row.id: (row.content, row.page_number) for row in result.all()}


async def hybrid_search(
    session: AsyncSession,
    organization_id: uuid.UUID,
    query: str,
    limit: int = 10,
) -> list[HybridSearchResult]:
    """Run dense and lexical search concurrently and fuse their results into
    one ranked candidate list via Reciprocal Rank Fusion.

    The query is embedded once (`embed_query`) and handed to
    `vector_store_service.search`; the raw query string is handed to
    `lexical_search_service.lexical_search` separately, since it does its
    own Postgres-side parsing (`websearch_to_tsquery`). Both searches run
    concurrently via `asyncio.gather`: `lexical_search` is natively async,
    but `vector_store_service.search` is a synchronous, blocking call
    (Qdrant's client is sync), so it is scheduled onto a worker thread via
    `asyncio.to_thread` -- awaiting it alongside the lexical coroutine
    inside the same `gather` rather than after it, so the two genuinely
    overlap in wall-clock time instead of one blocking the other.

    Args:
        session: an active async SQLAlchemy session.
        organization_id: the tenant to scope both searches to.
        query: the raw user search string.
        limit: maximum number of fused results to return.

    Returns:
        Up to `limit` `HybridSearchResult`, highest fused score first, each
        with `content` populated and ready to hand to
        `reranking_service.rerank`. Empty if neither search found anything.
    """
    if not query or not query.strip() or limit <= 0:
        return []

    pool_size = _candidate_pool_size(limit)
    query_embedding = embed_query(query)

    lexical_results, vector_results = await asyncio.gather(
        lexical_search(session, organization_id, query, limit=pool_size),
        asyncio.to_thread(vector_search, organization_id, query_embedding, limit=pool_size),
    )

    fused = fuse_search_results(lexical_results, vector_results, limit=limit)

    missing_ids = [result.chunk_id for result in fused if not result.content]
    if missing_ids:
        content_by_id = await _fetch_content_by_chunk_id(session, organization_id, missing_ids)
        fused = [
            replace(
                result,
                content=content_by_id[result.chunk_id][0],
                page_number=content_by_id[result.chunk_id][1],
            )
            if result.chunk_id in content_by_id
            else result
            for result in fused
        ]

    # A chunk_id that came back from Qdrant but no longer exists in Postgres
    # (e.g. deleted between indexing and this query) has no content to hand
    # a reranker -- drop it rather than return an unusable empty-content
    # candidate.
    return [result for result in fused if result.content]
