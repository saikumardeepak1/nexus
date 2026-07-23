"""Lexical (keyword) search over ``chunks.content_tsv``, ``ts_rank``-scored.

This is the lexical half of hybrid retrieval (see docs/TDD.md section 3.2).
It is intentionally standalone: it has no dependency on Qdrant or the
embedding model, and does not attempt to combine its results with dense
search -- that fusion is `hybrid_search_service`'s job (issue #11), built
once this and the Qdrant integration issue both land.

``chunks.content_tsv`` is populated and kept in sync with ``chunks.content``
by a Postgres trigger defined in the core schema migration (see
app/models/chunk.py and alembic/versions/7a7c3a5d9fbb_create_core_schema.py)
-- this module only ever reads it, never writes it.

Chunks belong to documents, which belong to organizations; there is no
``organization_id`` column directly on ``chunks`` (see the ERD in
docs/ARCHITECTURE.md), so every query here joins through ``documents`` to
scope results to the caller's organization. That join is what makes
cross-organization search isolation possible.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.models import Chunk, Document

# Must match the configuration used by the chunks_content_tsv_trigger
# (`to_tsvector('english', NEW.content)`) so query-side and write-side
# tsvectors are built with the same stemming/stopword rules.
_TS_CONFIG = "english"


@dataclass(frozen=True)
class LexicalSearchResult:
    """One chunk matched by lexical search, plus its ts_rank score.

    Deliberately small and independent of SQLAlchemy Row/ORM objects, so
    downstream services (hybrid_search_service, later) have a clean,
    stable contract to build on rather than depending on this module's
    internal query shape.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    content: str
    page_number: int | None
    rank: float


async def lexical_search(
    session: AsyncSession,
    organization_id: uuid.UUID,
    query: str,
    limit: int = 10,
) -> list[LexicalSearchResult]:
    """Run a ``ts_rank``-scored full-text search over an organization's chunks.

    Uses ``websearch_to_tsquery`` rather than ``plainto_tsquery`` to parse
    `query`. ``plainto_tsquery`` treats every word as a mandatory AND term
    with no operators at all, which is a poor fit for how people actually
    type into a search box. ``websearch_to_tsquery`` understands the same
    informal syntax a web search engine does (quoted phrases for exact
    matches, ``OR``, and ``-`` to exclude a term) while still never raising a
    syntax error on malformed input the way ``to_tsquery`` would -- it just
    degrades gracefully, which matters for a query string handed to us
    straight from a user rather than a developer.

    Results are scoped to `organization_id` by joining `Chunk` through its
    parent `Document` (chunks have no `organization_id` column directly,
    see docs/ARCHITECTURE.md), so a query can never return another
    organization's chunks even if their content would otherwise match.

    An empty, whitespace-only, or stopword-only `query` naturally produces
    an empty tsquery, which matches nothing in Postgres and raises no
    error; this function short-circuits on the empty/whitespace case before
    ever touching the database, to skip the round trip.

    Args:
        session: an active async SQLAlchemy session.
        organization_id: the tenant to scope results to.
        query: the raw user search string.
        limit: maximum number of results to return, ordered by rank
            descending.

    Returns:
        A list of `LexicalSearchResult`, highest `ts_rank` first. Empty if
        nothing matched.
    """
    if not query or not query.strip() or limit <= 0:
        return []

    tsquery = func.websearch_to_tsquery(_TS_CONFIG, query)
    rank = func.ts_rank(Chunk.content_tsv, tsquery).label("rank")

    stmt = (
        select(
            Chunk.id,
            Chunk.document_id,
            Chunk.content,
            Chunk.page_number,
            rank,
        )
        .join(Document, Chunk.document_id == Document.id)
        .where(Document.organization_id == organization_id)
        .where(Chunk.content_tsv.op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(limit)
    )

    result = await session.execute(stmt)
    return [
        LexicalSearchResult(
            chunk_id=row.id,
            document_id=row.document_id,
            content=row.content,
            page_number=row.page_number,
            rank=row.rank,
        )
        for row in result.all()
    ]
