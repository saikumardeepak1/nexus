"""Tests for app.graph.rag_graph (see docs/TDD.md section 3.3 and issue #14).

Split into two groups:

- Unit tests: `hybrid_search_service.hybrid_search`, `reranking_service.rerank`,
  and `generation_service.generate_answer` are mocked at their own module
  boundaries (the same pattern `test_hybrid_search_service.py`'s concurrency
  test and `test_generation_service.py` use for their own SDK boundaries),
  each recording exactly what it was called with. This proves the graph's
  wiring, not any one node's own logic: `retrieve`'s mocked return value is
  what `rerank` receives as its candidate input, `rerank`'s mocked return
  value is what `generate` receives as its candidate input, and the
  conversation history passed into `run_rag_graph` reaches `generate`
  unchanged. No database is touched here -- the session `run_rag_graph`
  is given is an opaque sentinel, passed straight through `GraphContext` to
  the mocked `hybrid_search`, never actually used for a query.
- One integration-style test: real Postgres, a real Qdrant collection, and
  real embeddings for `retrieve`/`rerank`, with only
  `generation_service.generate_answer` mocked (no live GEMINI_API_KEY in
  CI). Proves the graph produces a real, ranked, non-mocked retrieval result
  that reaches the generate node, not just that mocked stand-ins chain
  together correctly.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from typing import Any, cast

import pytest
from qdrant_client.http import models as qmodels
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph.rag_graph import run_rag_graph
from app.models import Chunk, Document, Organization
from app.services import (
    generation_service,
    hybrid_search_service,
    reranking_service,
    vector_store_service,
)
from app.services.embedding_service import embed_documents
from app.services.generation_service import ConversationTurn, GenerationResult, ResolvedCitation
from app.services.hybrid_search_service import HybridSearchResult
from app.services.reranking_service import RerankCandidate
from app.services.vector_store_service import ensure_collection, get_client

# --- Unit tests: mocked services, wiring only -------------------------------

_ORG_ID = uuid.uuid4()
_CHUNK_ID = uuid.uuid4()
_DOC_ID = uuid.uuid4()

_RETRIEVED = [
    HybridSearchResult(
        chunk_id=_CHUNK_ID,
        document_id=_DOC_ID,
        content="retrieved content",
        page_number=1,
        fused_score=0.5,
    ),
]

_RERANKED = [
    RerankCandidate(chunk_id=str(_CHUNK_ID), content="retrieved content", relevance_score=0.9),
]

_GENERATED = GenerationResult(
    answer="the answer [1]",
    citations=[
        ResolvedCitation(chunk_id=str(_CHUNK_ID), relevance_score=0.9, marker=1, text_position=10)
    ],
)

# A stand-in for the AsyncSession run_rag_graph is given: never actually
# queried against in these unit tests (hybrid_search itself is mocked out),
# only carried through GraphContext and compared for identity, so a plain
# sentinel object is enough -- no real database needed.
_SENTINEL_SESSION = cast(AsyncSession, object())


def _install_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Monkeypatch all three service boundaries and return a dict that gets
    filled in with each mocked call's actual arguments as the graph runs.
    """
    captured: dict[str, Any] = {}

    async def fake_hybrid_search(
        session: AsyncSession, organization_id: uuid.UUID, query: str, limit: int = 10
    ) -> list[HybridSearchResult]:
        captured["retrieve_args"] = {
            "session": session,
            "organization_id": organization_id,
            "query": query,
        }
        return _RETRIEVED

    def fake_rerank(
        query: str, candidates: list[RerankCandidate], top_k: int = 5
    ) -> list[RerankCandidate]:
        captured["rerank_args"] = {"query": query, "candidates": candidates}
        return _RERANKED

    async def fake_generate_answer(
        query: str, history: list[ConversationTurn], candidates: list[RerankCandidate]
    ) -> GenerationResult:
        captured["generate_args"] = {"query": query, "history": history, "candidates": candidates}
        return _GENERATED

    monkeypatch.setattr(hybrid_search_service, "hybrid_search", fake_hybrid_search)
    monkeypatch.setattr(reranking_service, "rerank", fake_rerank)
    monkeypatch.setattr(generation_service, "generate_answer", fake_generate_answer)
    return captured


async def test_retrieve_receives_query_organization_id_and_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_mocks(monkeypatch)

    await run_rag_graph(_SENTINEL_SESSION, _ORG_ID, "what is x", history=[])

    assert captured["retrieve_args"] == {
        "session": _SENTINEL_SESSION,
        "organization_id": _ORG_ID,
        "query": "what is x",
    }


async def test_rerank_receives_retrieves_output_as_its_candidate_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_mocks(monkeypatch)

    await run_rag_graph(_SENTINEL_SESSION, _ORG_ID, "what is x", history=[])

    # retrieve returned _RETRIEVED (HybridSearchResult); rerank must receive
    # that exact chunk_id/content, converted into the RerankCandidate shape
    # reranking_service.rerank expects (chunk_id stringified, no score yet).
    assert captured["rerank_args"]["query"] == "what is x"
    assert captured["rerank_args"]["candidates"] == [
        RerankCandidate(chunk_id=str(_CHUNK_ID), content="retrieved content")
    ]


async def test_generate_receives_reranks_output_as_its_candidate_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_mocks(monkeypatch)

    await run_rag_graph(_SENTINEL_SESSION, _ORG_ID, "what is x", history=[])

    # rerank returned _RERANKED; generate must receive that exact list,
    # scores included, unchanged.
    assert captured["generate_args"]["candidates"] == _RERANKED


async def test_conversation_history_reaches_generate_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_mocks(monkeypatch)
    history = [
        ConversationTurn(role="user", content="What is a good espresso grind size?"),
        ConversationTurn(role="assistant", content="A fine, consistent grind [1]."),
    ]

    await run_rag_graph(_SENTINEL_SESSION, _ORG_ID, "And what temperature?", history=history)

    assert captured["generate_args"]["history"] == history
    assert captured["generate_args"]["query"] == "And what temperature?"


async def test_history_defaults_to_empty_list_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_mocks(monkeypatch)

    await run_rag_graph(_SENTINEL_SESSION, _ORG_ID, "what is x")

    assert captured["generate_args"]["history"] == []


async def test_final_result_contains_generates_answer_and_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mocks(monkeypatch)

    result = await run_rag_graph(_SENTINEL_SESSION, _ORG_ID, "what is x", history=[])

    assert result.answer == _GENERATED.answer
    assert result.citations == _GENERATED.citations


# --- Integration-style test: real Postgres + real Qdrant + real embeddings,
# only generation mocked (no live GEMINI_API_KEY in CI). ----------------------


@pytest.fixture
def qdrant_points() -> Generator[list[uuid.UUID], None, None]:
    """Same pattern as test_hybrid_search_service.py's fixture of the same
    name: tracks chunk_ids upserted into the real Qdrant collection during a
    test and deletes exactly those points afterward.
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


async def test_graph_feeds_real_ranked_retrieval_into_mocked_generate(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    qdrant_points: list[uuid.UUID],
) -> None:
    """Real hybrid search + real reranking against a small real corpus, with
    only generation_service.generate_answer mocked. Proves retrieve and
    rerank are wired together correctly end to end with real data, not just
    that hand-built mock return values chain through -- the top-ranked
    candidate generate is called with must be the one real chunk that
    actually answers the query.
    """
    organization = Organization(name="Graph Integration Corp")
    db_session.add(organization)
    await db_session.flush()

    document = Document(
        organization_id=organization.id, filename="handbook.pdf", status="ready"
    )
    db_session.add(document)
    await db_session.flush()

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
    await db_session.commit()

    captured: dict[str, Any] = {}

    async def fake_generate_answer(
        query: str, history: list[ConversationTurn], candidates: list[RerankCandidate]
    ) -> GenerationResult:
        captured["candidates"] = candidates
        return GenerationResult(answer="mocked answer [1]", citations=[])

    monkeypatch.setattr(generation_service, "generate_answer", fake_generate_answer)

    result = await run_rag_graph(
        db_session, organization.id, query="9284-XQ", history=[]
    )

    assert result.answer == "mocked answer [1]"
    reranked_candidates = captured["candidates"]
    assert len(reranked_candidates) > 0
    assert reranked_candidates[0].chunk_id == str(target.id)
    assert reranked_candidates[0].content == target.content
    assert reranked_candidates[0].relevance_score is not None
