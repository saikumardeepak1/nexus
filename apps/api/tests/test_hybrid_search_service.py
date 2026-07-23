"""Tests for app.services.hybrid_search_service (see docs/TDD.md section
3.2 and issue #11).

Split into three groups:

- Pure RRF fusion tests (`fuse_search_results`, `_rrf_scores`): hand
  constructed `LexicalSearchResult`/`VectorSearchResult` lists with known
  rankings, no database or Qdrant involved. These are the tests that pin
  down the actual fusion math.
- A concurrency test: proves `hybrid_search` actually overlaps the lexical
  and dense searches in wall-clock time rather than running them one after
  the other, by instrumenting both underlying calls with a sleep and
  asserting total elapsed time is close to a single sleep, not their sum.
- Fixture-corpus eval tests, against a real (test) Postgres and a real
  (test) Qdrant instance with real embeddings: a query with an obvious
  keyword match (lexical carries it) and a query that's a semantic
  paraphrase sharing no keywords with its target chunk (dense carries it),
  both confirming hybrid search surfaces the right chunk.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Generator

import pytest
from qdrant_client.http import models as qmodels
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Document, Organization
from app.services import hybrid_search_service, vector_store_service
from app.services.embedding_service import embed_documents
from app.services.hybrid_search_service import (
    RRF_K,
    fuse_search_results,
    hybrid_search,
)
from app.services.lexical_search_service import LexicalSearchResult, lexical_search
from app.services.vector_store_service import VectorSearchResult, ensure_collection, get_client

# --- Pure RRF fusion tests ---------------------------------------------------
# No database or Qdrant access anywhere in this section: every input is a
# hand-built LexicalSearchResult/VectorSearchResult, and every assertion is
# about the resulting rank order or fused_score value.

_DOC = uuid.uuid4()
_CHUNK_A = uuid.uuid4()  # lexical rank 1 only
_CHUNK_B = uuid.uuid4()  # lexical rank 2, vector rank 1 -- appears in both
_CHUNK_C = uuid.uuid4()  # vector rank 2 only


def _lexical(chunk_id: uuid.UUID, content: str, rank: float) -> LexicalSearchResult:
    return LexicalSearchResult(
        chunk_id=chunk_id, document_id=_DOC, content=content, page_number=None, rank=rank
    )


def _vector(chunk_id: uuid.UUID, score: float) -> VectorSearchResult:
    return VectorSearchResult(chunk_id=chunk_id, document_id=_DOC, score=score)


def test_chunk_in_both_lists_ranks_above_chunk_in_only_one() -> None:
    """The whole point of RRF: a chunk appearing in both lists (even at a
    modest position in each) should generally outrank a chunk that only
    appears in a single list, even at rank 1 there.
    """
    lexical_results = [
        _lexical(_CHUNK_A, "content a", rank=10.0),  # rank 1
        _lexical(_CHUNK_B, "content b", rank=8.0),  # rank 2
    ]
    vector_results = [
        _vector(_CHUNK_B, score=0.9),  # rank 1
        _vector(_CHUNK_C, score=0.8),  # rank 2
    ]

    fused = fuse_search_results(lexical_results, vector_results, limit=10)

    assert [r.chunk_id for r in fused] == [_CHUNK_B, _CHUNK_A, _CHUNK_C]


def test_fused_scores_match_rrf_formula() -> None:
    lexical_results = [
        _lexical(_CHUNK_A, "content a", rank=10.0),
        _lexical(_CHUNK_B, "content b", rank=8.0),
    ]
    vector_results = [
        _vector(_CHUNK_B, score=0.9),
        _vector(_CHUNK_C, score=0.8),
    ]

    fused = fuse_search_results(lexical_results, vector_results, limit=10, k=RRF_K)
    scores_by_id = {r.chunk_id: r.fused_score for r in fused}

    assert scores_by_id[_CHUNK_A] == pytest.approx(1.0 / (RRF_K + 1))
    assert scores_by_id[_CHUNK_B] == pytest.approx(1.0 / (RRF_K + 2) + 1.0 / (RRF_K + 1))
    assert scores_by_id[_CHUNK_C] == pytest.approx(1.0 / (RRF_K + 2))


def test_content_prefers_lexical_when_chunk_appears_in_both() -> None:
    lexical_results = [_lexical(_CHUNK_B, "the lexical content", rank=5.0)]
    vector_results = [_vector(_CHUNK_B, score=0.5)]

    [fused] = fuse_search_results(lexical_results, vector_results, limit=10)

    assert fused.content == "the lexical content"


def test_vector_only_hit_has_empty_content_placeholder() -> None:
    """`fuse_search_results` itself never touches Postgres -- a vector-only
    hit's content is left empty here, and it's `hybrid_search`'s job (not
    this function's) to backfill it from Postgres afterward.
    """
    vector_results = [_vector(_CHUNK_C, score=0.5)]

    [fused] = fuse_search_results([], vector_results, limit=10)

    assert fused.chunk_id == _CHUNK_C
    assert fused.document_id == _DOC
    assert fused.content == ""
    assert fused.page_number is None


def test_limit_truncates_fused_results() -> None:
    lexical_results = [_lexical(uuid.uuid4(), f"content {i}", rank=float(10 - i)) for i in range(5)]

    fused = fuse_search_results(lexical_results, [], limit=2)

    assert len(fused) == 2


def test_empty_inputs_return_empty_list() -> None:
    assert fuse_search_results([], [], limit=10) == []


def test_non_positive_limit_returns_empty_list() -> None:
    lexical_results = [_lexical(_CHUNK_A, "content a", rank=1.0)]
    assert fuse_search_results(lexical_results, [], limit=0) == []
    assert fuse_search_results(lexical_results, [], limit=-1) == []


def test_lexical_only_and_vector_only_results_both_surface() -> None:
    """A chunk found by only one of the two searches must still appear in
    the fused output (RRF never drops a single-list hit), just with a
    lower score than a chunk found by both.
    """
    lexical_results = [_lexical(_CHUNK_A, "content a", rank=5.0)]
    vector_results = [_vector(_CHUNK_C, score=0.5)]

    fused = fuse_search_results(lexical_results, vector_results, limit=10)

    assert {r.chunk_id for r in fused} == {_CHUNK_A, _CHUNK_C}


# --- Concurrency proof -------------------------------------------------------


async def test_lexical_and_dense_search_run_concurrently(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Instruments both underlying calls with an artificial delay and
    asserts hybrid_search's total wall time is close to a single delay
    (concurrent) rather than the sum of both (sequential).
    """
    delay_seconds = 0.25

    async def _slow_lexical_search(*args: object, **kwargs: object) -> list[LexicalSearchResult]:
        await asyncio.sleep(delay_seconds)
        return []

    def _slow_vector_search(*args: object, **kwargs: object) -> list[VectorSearchResult]:
        time.sleep(delay_seconds)
        return []

    monkeypatch.setattr(hybrid_search_service, "lexical_search", _slow_lexical_search)
    monkeypatch.setattr(hybrid_search_service, "vector_search", _slow_vector_search)
    monkeypatch.setattr(hybrid_search_service, "embed_query", lambda text: [0.0])

    start = time.perf_counter()
    await hybrid_search(db_session, uuid.uuid4(), "anything", limit=10)
    elapsed = time.perf_counter() - start

    # Sequential execution would take ~2 * delay_seconds; concurrent
    # execution should take ~1 * delay_seconds. The threshold sits well
    # below the sequential total to give scheduling jitter headroom without
    # being able to pass by accident if the two calls were run one after
    # the other.
    assert elapsed < delay_seconds * 1.5, (
        f"expected concurrent execution (~{delay_seconds}s), took {elapsed:.3f}s "
        f"(sequential would be ~{delay_seconds * 2}s)"
    )


# --- Fixture-corpus eval tests -----------------------------------------------
# Real Postgres, real Qdrant, real embeddings. Each test writes its own
# small corpus into the real `nexus_chunks` Qdrant collection (there is no
# collection_name override on hybrid_search's signature, per issue #11) and
# cleans up its own points afterward via the qdrant_points fixture; the
# Postgres side cleans up on its own via the db_session fixture's
# transaction rollback.


@pytest.fixture
def qdrant_points() -> Generator[list[uuid.UUID], None, None]:
    """Tracks chunk_ids upserted into the real Qdrant collection during a
    test, and deletes exactly those points afterward -- so eval tests can
    exercise `hybrid_search` end to end (which always searches
    `vector_store_service.COLLECTION_NAME`, not a scratch collection)
    without leaving permanent junk behind in it.
    """
    ensure_collection()
    ids: list[uuid.UUID] = []
    yield ids
    if ids:
        get_client().delete(
            collection_name=vector_store_service.COLLECTION_NAME,
            points_selector=qmodels.PointIdsList(points=[str(chunk_id) for chunk_id in ids]),
        )


async def _make_chunk(
    db_session: AsyncSession,
    document: Document,
    content: str,
    qdrant_points: list[uuid.UUID],
    *,
    chunk_index: int,
) -> Chunk:
    """Writes a chunk to Postgres and upserts its embedding into the real
    Qdrant collection, tracking it in `qdrant_points` for teardown.
    """
    chunk = Chunk(document_id=document.id, chunk_index=chunk_index, content=content)
    db_session.add(chunk)
    await db_session.flush()

    [embedding] = embed_documents([content])
    vector_store_service.upsert_chunk(
        chunk_id=chunk.id,
        document_id=document.id,
        organization_id=document.organization_id,
        embedding=embedding,
        content=content,
    )
    qdrant_points.append(chunk.id)
    return chunk


async def _make_org_with_document(db_session: AsyncSession, *, name: str) -> Document:
    organization = Organization(name=name)
    db_session.add(organization)
    await db_session.flush()

    document = Document(
        organization_id=organization.id, filename=f"{name.lower()}.pdf", status="ready"
    )
    db_session.add(document)
    await db_session.flush()
    return document


async def test_obvious_keyword_match_surfaces_via_lexical_signal(
    db_session: AsyncSession, qdrant_points: list[uuid.UUID]
) -> None:
    """A literal product code has essentially no semantic content for a
    dense embedding model to key off of, but is an exact, unambiguous
    keyword match for full-text search -- hybrid search should surface it
    via the lexical half of the fusion.
    """
    document = await _make_org_with_document(db_session, name="Keyword Corp")

    target = await _make_chunk(
        db_session,
        document,
        "Product SKU 9284-XQ ships within two business days from the Ohio warehouse.",
        qdrant_points,
        chunk_index=0,
    )
    await _make_chunk(
        db_session,
        document,
        "The warehouse team is hiring additional staff for the holiday season.",
        qdrant_points,
        chunk_index=1,
    )
    await _make_chunk(
        db_session,
        document,
        "Ohio experienced heavy snowfall this week, affecting shipping schedules.",
        qdrant_points,
        chunk_index=2,
    )
    await _make_chunk(
        db_session,
        document,
        "Shipping delays are common during peak holiday demand nationwide.",
        qdrant_points,
        chunk_index=3,
    )
    await db_session.commit()

    results = await hybrid_search(
        db_session, document.organization_id, query="9284-XQ", limit=10
    )

    assert len(results) > 0
    assert results[0].chunk_id == target.id
    assert results[0].content == target.content


async def test_semantic_paraphrase_with_no_shared_keywords_surfaces_via_dense_signal(
    db_session: AsyncSession, qdrant_points: list[uuid.UUID]
) -> None:
    """The query paraphrases the target chunk with entirely different
    vocabulary (verified below: plain lexical search over the same corpus
    finds nothing for this query), so only the dense half of the fusion can
    surface the right chunk.
    """
    document = await _make_org_with_document(db_session, name="Semantic Corp")

    target = await _make_chunk(
        db_session,
        document,
        "Staff welcoming an additional family member receive twelve weeks at full salary.",
        qdrant_points,
        chunk_index=0,
    )
    await _make_chunk(
        db_session,
        document,
        "Conference room bookings must be made at least one day in advance.",
        qdrant_points,
        chunk_index=1,
    )
    await _make_chunk(
        db_session,
        document,
        "The quarterly earnings call is scheduled for next Tuesday afternoon.",
        qdrant_points,
        chunk_index=2,
    )
    await _make_chunk(
        db_session,
        document,
        "Expense reports submitted after the fifth of the month roll over to next cycle.",
        qdrant_points,
        chunk_index=3,
    )
    await db_session.commit()

    query = "How much paid leave do new parents get?"

    # Confirms the premise: no lexical term overlap at all between the
    # query and the target chunk's content over this corpus.
    lexical_only = await lexical_search(db_session, document.organization_id, query)
    assert target.id not in [r.chunk_id for r in lexical_only]

    results = await hybrid_search(db_session, document.organization_id, query, limit=10)

    assert len(results) > 0
    assert results[0].chunk_id == target.id
    assert results[0].content == target.content
