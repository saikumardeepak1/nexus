"""Integration tests for lexical_search_service, against a real Postgres.

Covers the issue #10 acceptance criteria:
- content_tsv is populated correctly on chunk write (exercised implicitly by
  every test here since search only works if the trigger ran; also asserted
  directly once below)
- search results are ordered by ts_rank, not insertion order
- an exact/close match ranks above a loose match
- a query never returns another organization's chunks, even when their
  content would otherwise match (cross-org isolation)
- an empty/no-match query returns an empty list without erroring
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Document, Organization
from app.services.lexical_search_service import LexicalSearchResult, lexical_search


async def _make_org_with_document(db_session: AsyncSession, *, name: str) -> Document:
    organization = Organization(name=name)
    db_session.add(organization)
    await db_session.flush()

    document = Document(
        organization_id=organization.id,
        filename=f"{name.lower()}.pdf",
        status="ready",
    )
    db_session.add(document)
    await db_session.flush()
    return document


async def test_content_tsv_populated_on_write(db_session: AsyncSession) -> None:
    document = await _make_org_with_document(db_session, name="Initrode")

    chunk = Chunk(
        document_id=document.id,
        chunk_index=0,
        content="The onboarding checklist covers laptop setup and badge access.",
    )
    db_session.add(chunk)
    await db_session.commit()

    await db_session.refresh(chunk)
    assert chunk.content_tsv is not None
    assert "onboard" in chunk.content_tsv
    assert "badg" in chunk.content_tsv


async def test_ranks_by_relevance_not_insertion_order(db_session: AsyncSession) -> None:
    document = await _make_org_with_document(db_session, name="Globex")

    # chunk_index=0 (inserted first) only mentions the query terms once, in
    # passing, diluted among unrelated text -- a loose match.
    loose_match = Chunk(
        document_id=document.id,
        chunk_index=0,
        content=(
            "Our travel policy covers many topics unrelated to time off "
            "requests, but note in passing time off exists."
        ),
    )
    # chunk_index=1 (inserted second) is dense with the query terms -- a
    # close/exact match. If results were ordered by chunk_index or
    # insertion order, loose_match would come first; ts_rank should put
    # dense_match first instead.
    dense_match = Chunk(
        document_id=document.id,
        chunk_index=1,
        content=(
            "Time off. Time off. Time off. Employees may request time off "
            "for any reason. This document is entirely about time off "
            "policy."
        ),
    )
    db_session.add_all([loose_match, dense_match])
    await db_session.commit()

    results = await lexical_search(
        db_session, organization_id=document.organization_id, query="time off"
    )

    assert [r.chunk_id for r in results] == [dense_match.id, loose_match.id]
    assert results[0].rank > results[1].rank


async def test_cross_org_isolation(db_session: AsyncSession) -> None:
    document_a = await _make_org_with_document(db_session, name="Acme")
    document_b = await _make_org_with_document(db_session, name="Umbrella")

    chunk_a = Chunk(
        document_id=document_a.id,
        chunk_index=0,
        content="Confidential merger negotiations are underway between Acme and Initech.",
    )
    # Same query terms, same relevance, but belongs to a different org.
    chunk_b = Chunk(
        document_id=document_b.id,
        chunk_index=0,
        content="Confidential merger negotiations for a completely different deal.",
    )
    db_session.add_all([chunk_a, chunk_b])
    await db_session.commit()

    results_a = await lexical_search(
        db_session, organization_id=document_a.organization_id, query="merger negotiations"
    )
    results_b = await lexical_search(
        db_session, organization_id=document_b.organization_id, query="merger negotiations"
    )

    assert [r.chunk_id for r in results_a] == [chunk_a.id]
    assert [r.chunk_id for r in results_b] == [chunk_b.id]
    # Neither org's result set leaks the other org's chunk, even though the
    # content would otherwise match equally well.
    assert chunk_b.id not in [r.chunk_id for r in results_a]
    assert chunk_a.id not in [r.chunk_id for r in results_b]


async def test_empty_query_returns_empty_list(db_session: AsyncSession) -> None:
    document = await _make_org_with_document(db_session, name="Soylent")
    chunk = Chunk(document_id=document.id, chunk_index=0, content="Anything at all.")
    db_session.add(chunk)
    await db_session.commit()

    assert await lexical_search(db_session, document.organization_id, "") == []
    assert await lexical_search(db_session, document.organization_id, "   ") == []


async def test_no_match_query_returns_empty_list(db_session: AsyncSession) -> None:
    document = await _make_org_with_document(db_session, name="Stark")
    chunk = Chunk(
        document_id=document.id,
        chunk_index=0,
        content="The quarterly earnings call is scheduled for next Tuesday.",
    )
    db_session.add(chunk)
    await db_session.commit()

    results = await lexical_search(
        db_session, document.organization_id, "nonexistent zzzznotarealword"
    )
    assert results == []


async def test_unknown_organization_returns_empty_list(db_session: AsyncSession) -> None:
    document = await _make_org_with_document(db_session, name="Wayne")
    chunk = Chunk(
        document_id=document.id,
        chunk_index=0,
        content="Batman patrols Gotham at night.",
    )
    db_session.add(chunk)
    await db_session.commit()

    results = await lexical_search(db_session, uuid.uuid4(), "Batman Gotham")
    assert results == []


async def test_result_shape(db_session: AsyncSession) -> None:
    document = await _make_org_with_document(db_session, name="Hooli")
    chunk = Chunk(
        document_id=document.id,
        chunk_index=0,
        content="Middle-out compression is our core algorithm.",
        page_number=3,
    )
    db_session.add(chunk)
    await db_session.commit()

    results = await lexical_search(db_session, document.organization_id, "compression algorithm")

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, LexicalSearchResult)
    assert result.chunk_id == chunk.id
    assert result.document_id == document.id
    assert result.content == chunk.content
    assert result.page_number == 3
    assert isinstance(result.rank, float)
    assert result.rank > 0
