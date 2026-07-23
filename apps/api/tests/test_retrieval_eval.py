"""Retrieval quality eval: a small, hand-written fixture corpus with known
question/expected-chunk pairs, run through the real pipeline (hybrid search,
then reranking) end to end against real Postgres and real Qdrant with real
embeddings (see docs/TDD.md section 6, "Retrieval eval", and issue #19).

This is the standing test that validates retrieval *quality*, not just that
the code runs: every assertion below checks that a specific, pre-selected
chunk actually lands in the top-K after the full retrieve -> rerank pipeline,
for a query written ahead of time to target it.

Fixture corpus
---------------
Twelve short passages across three unrelated topics (travel/expense policy,
vector-search internals, sourdough baking), hand-written and committed here
rather than generated at test time. This deliberately does not reuse
`test_hybrid_search_service.py`'s warehouse/parental-leave corpus or
`test_reranking_service.py`'s espresso/Kubernetes corpus -- it is a separate
fixture that happens to exercise the same two retrieval scenarios (an
obvious keyword match, and a semantic paraphrase sharing no keywords with
its target) through one more pipeline stage than either of those files
covers alone: this file runs `hybrid_search` *and then* `reranking_service.
rerank` on the fused candidates, the same two-stage path `app/graph/
rag_graph.py`'s `retrieve` -> `rerank` nodes call in production, rather than
stopping at hybrid search's own output.

Eval questions
---------------
- Two obvious keyword matches: a unique policy code and a unique technical
  term, each appearing verbatim in exactly one passage, which lexical search
  should carry to the top even though a dense embedding has little to key
  off of in a short unique token.
- Two semantic paraphrases: each query shares no meaningful keyword with its
  target passage (verified explicitly below via a plain lexical-search
  check, the same premise-check pattern `test_hybrid_search_service.py`
  uses), so only the dense half of hybrid search -- and then reranking's
  cross-encoder -- can be what surfaces the right chunk.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
import pytest_asyncio
from qdrant_client.http import models as qmodels
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Document, Organization
from app.services import vector_store_service
from app.services.embedding_service import embed_documents
from app.services.hybrid_search_service import hybrid_search
from app.services.lexical_search_service import lexical_search
from app.services.reranking_service import RerankCandidate, rerank

# --- Fixture corpus: 12 passages across 3 unrelated topics ------------------

_CORPUS: dict[str, str] = {
    # --- Travel & expense policy ---
    "travel-per-diem": (
        "Domestic travel per diem is capped at $75 per day under Finance "
        "Policy FP-114."
    ),
    "travel-international": (
        "International travel requires a pre-approved itinerary submitted "
        "at least two weeks in advance."
    ),
    "travel-cards": (
        "Corporate credit card statements must be reconciled in the expense "
        "system within five business days of the statement close."
    ),
    "travel-rental-cars": (
        "Rental cars booked through the preferred vendor are covered in "
        "full; other vendors require manager approval."
    ),
    # --- Vector search / Qdrant internals ---
    "vector-ef-construct": (
        "Raising the ef_construct parameter builds a more thorough graph at "
        "index time, which later searches read from for noticeably better "
        "recall."
    ),
    "vector-cosine": (
        "The nexus_chunks collection uses COSINE distance with vectors "
        "sized to the embedding model's output dimension."
    ),
    "vector-payload": (
        "Every point's payload stores organization_id, document_id, and "
        "chunk_id so a search hit can be resolved back to Postgres."
    ),
    "vector-rrf": (
        "Reciprocal rank fusion combines a lexical ranked list and a dense "
        "ranked list without ever normalizing their differently-scaled raw "
        "scores."
    ),
    # --- Sourdough baking ---
    "bread-starter": (
        "A starter that doubles in volume within four to six hours of "
        "feeding is active enough to leaven a loaf."
    ),
    "bread-shaping": (
        "Shaping the dough tightly before its final rise builds the surface "
        "tension that gives the crust its rounded shape."
    ),
    "bread-scoring": (
        "Scoring the loaf just before baking lets steam escape from a "
        "predictable spot instead of tearing the crust randomly."
    ),
    "bread-dutch-oven": (
        "A Dutch oven traps the steam a home oven can't hold onto, which is "
        "what gives the crust its shatter when you tap it."
    ),
}

# --- Eval questions: (query, expected passage key, human-readable kind) -----

_EVAL_CASES: list[tuple[str, str, str]] = [
    # Obvious keyword matches: a unique alphanumeric policy code and a
    # unique technical term, neither of which a dense embedding has much
    # semantic content to key off of on its own.
    ("FP-114", "travel-per-diem", "keyword"),
    ("COSINE distance", "vector-cosine", "keyword"),
    # Semantic paraphrases sharing no meaningful keyword with their target
    # passage (see the premise-check tests below).
    (
        "Why would increasing a build-time setting make future lookups sharper?",
        "vector-ef-construct",
        "semantic",
    ),
    (
        "How can I tell if my fermented dough culture is ready to bake with?",
        "bread-starter",
        "semantic",
    ),
]

_TOP_K = 3


@pytest.fixture
def qdrant_points() -> Generator[list[uuid.UUID], None, None]:
    """Tracks chunk_ids upserted into the real Qdrant collection during a
    test and deletes exactly those points afterward, the same pattern
    `test_hybrid_search_service.py` uses (hybrid_search always searches
    `vector_store_service.COLLECTION_NAME`, so this eval writes into and
    cleans up that real collection rather than a scratch one).
    """
    vector_store_service.ensure_collection()
    ids: list[uuid.UUID] = []
    yield ids
    if ids:
        vector_store_service.get_client().delete(
            collection_name=vector_store_service.COLLECTION_NAME,
            points_selector=qmodels.PointIdsList(points=[str(chunk_id) for chunk_id in ids]),
        )


@pytest_asyncio.fixture
async def corpus_chunks(
    db_session: AsyncSession, qdrant_points: list[uuid.UUID]
) -> dict[str, uuid.UUID]:
    """Writes every `_CORPUS` passage as a `Chunk` row in Postgres and its
    real embedding as a point in the real Qdrant collection, scoped to one
    fresh `Organization`. Returns a mapping of the corpus's human-readable
    keys to the chunk_id actually assigned, so eval assertions can refer to
    "bread-starter" rather than a raw UUID.
    """
    organization = Organization(name="Retrieval Eval Corp")
    db_session.add(organization)
    await db_session.flush()

    document = Document(
        organization_id=organization.id, filename="retrieval-eval-corpus.txt", status="ready"
    )
    db_session.add(document)
    await db_session.flush()

    keys = list(_CORPUS.keys())
    contents = [_CORPUS[key] for key in keys]
    embeddings = embed_documents(contents)

    chunk_ids: dict[str, uuid.UUID] = {}
    for index, (key, content, embedding) in enumerate(
        zip(keys, contents, embeddings, strict=True)
    ):
        chunk = Chunk(document_id=document.id, chunk_index=index, content=content)
        db_session.add(chunk)
        await db_session.flush()

        vector_store_service.upsert_chunk(
            chunk_id=chunk.id,
            document_id=document.id,
            organization_id=organization.id,
            embedding=embedding,
            content=content,
        )
        qdrant_points.append(chunk.id)
        chunk_ids[key] = chunk.id

    await db_session.commit()

    # Stash the organization_id on the returned dict via a module-level side
    # channel would be awkward; tests that need it re-derive it from the
    # document instead (see `_organization_id_for` below).
    return chunk_ids


async def _organization_id_for_document(
    db_session: AsyncSession, chunk_id: uuid.UUID
) -> uuid.UUID:
    chunk = await db_session.get(Chunk, chunk_id)
    assert chunk is not None
    document = await db_session.get(Document, chunk.document_id)
    assert document is not None
    return document.organization_id


# --- Premise checks: the "semantic" queries really share no keyword --------


async def test_semantic_queries_share_no_lexical_signal_with_their_target(
    db_session: AsyncSession, corpus_chunks: dict[str, uuid.UUID]
) -> None:
    """Confirms the premise of the semantic eval cases below: plain lexical
    search over this corpus finds nothing for either semantic-paraphrase
    query for its target chunk, the same premise-check pattern
    `test_hybrid_search_service.py` uses for its own paraphrase case. If
    this ever stops holding (e.g. the corpus or queries are edited later),
    the semantic cases would silently start passing via the lexical half of
    fusion instead of actually exercising the dense signal -- this test
    exists so that regression is loud instead of silent.
    """
    organization_id = await _organization_id_for_document(
        db_session, corpus_chunks["vector-ef-construct"]
    )
    for query, expected_key, kind in _EVAL_CASES:
        if kind != "semantic":
            continue
        expected_chunk_id = corpus_chunks[expected_key]
        lexical_only = await lexical_search(db_session, organization_id, query)
        assert expected_chunk_id not in [r.chunk_id for r in lexical_only], (
            f"expected no lexical overlap between {query!r} and its target chunk "
            f"({expected_key}), but lexical search found it anyway"
        )


# --- The eval itself: hybrid search -> rerank -> expected chunk in top-K ---


async def test_expected_chunk_lands_in_top_k_after_hybrid_search_and_rerank(
    db_session: AsyncSession, corpus_chunks: dict[str, uuid.UUID]
) -> None:
    """The real eval: for every (query, expected chunk) pair, run the actual
    two-stage retrieval pipeline production uses (`hybrid_search_service.
    hybrid_search` then `reranking_service.rerank`, the same
    `HybridSearchResult` -> `RerankCandidate` mapping `app/graph/
    rag_graph.py`'s `rerank` node uses) and assert the expected chunk is
    among the top `_TOP_K` results -- not merely that some chunk came back.
    """
    organization_id = await _organization_id_for_document(
        db_session, corpus_chunks["travel-per-diem"]
    )

    failures: list[str] = []
    for query, expected_key, kind in _EVAL_CASES:
        expected_chunk_id = corpus_chunks[expected_key]

        hybrid_results = await hybrid_search(db_session, organization_id, query, limit=10)
        assert len(hybrid_results) > 0, f"hybrid_search returned nothing for {query!r}"

        candidates = [
            RerankCandidate(chunk_id=str(result.chunk_id), content=result.content)
            for result in hybrid_results
        ]
        reranked = rerank(query, candidates, top_k=_TOP_K)

        top_k_chunk_ids = {uuid.UUID(candidate.chunk_id) for candidate in reranked}
        landed = expected_chunk_id in top_k_chunk_ids
        if not landed:
            failures.append(
                f"[{kind}] query={query!r} expected={expected_key!r} "
                f"not in top-{_TOP_K}: {[c.chunk_id for c in reranked]}"
            )

        # Printed for real, manual eyeballing per the task's verification
        # step (same convention as test_chunking_service.py's PDF sanity
        # check): shows exactly which chunk landed where for every case.
        print(f"\n[{kind}] query={query!r} expected={expected_key!r} landed={landed}")
        for rank, candidate in enumerate(reranked, start=1):
            marker = " <-- expected" if candidate.chunk_id == str(expected_chunk_id) else ""
            print(
                f"    #{rank} score={candidate.relevance_score:.4f} "
                f"content={candidate.content!r}{marker}"
            )

    assert not failures, "retrieval eval failures:\n" + "\n".join(failures)


async def test_each_eval_case_individually_reports_its_own_result() -> None:
    """A thin, parametrize-style wrapper is deliberately not used for the
    main eval above so a single failing case doesn't stop the others from
    running and reporting; this second test instead asserts the count of
    eval cases actually exercised matches what's defined above, so a future
    edit that silently drops a case from `_EVAL_CASES` without updating this
    number is caught.
    """
    assert len(_EVAL_CASES) == 4
    assert sum(1 for _, _, kind in _EVAL_CASES if kind == "keyword") == 2
    assert sum(1 for _, _, kind in _EVAL_CASES if kind == "semantic") == 2


async def test_corpus_spans_at_least_three_distinct_topics() -> None:
    """Acceptance-criteria sanity check: the fixture corpus itself spans at
    least three distinct topics and 8-15 passages (see docs/TDD.md section 6
    and issue #19), not just a restatement of the eval question count above.
    """
    assert 8 <= len(_CORPUS) <= 15
    topics = {key.split("-")[0] for key in _CORPUS}
    assert len(topics) >= 3
