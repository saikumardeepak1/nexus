"""Integration tests for app.services.vector_store_service, against a real
(test) Qdrant instance and real embedding_service inference (see
docs/TDD.md section 3.2 and issue #9).

Each test gets its own scratch collection (a randomly-suffixed name, never
COLLECTION_NAME itself) via the `collection_name` fixture below, created
before the test and dropped after, so tests never interfere with each other
or with any real "nexus_chunks" data -- the same "scratch collection per
test" pattern docs/TDD.md section 6 describes for integration tests.

Real embeddings (BAAI/bge-small-en-v1.5, via embedding_service) are used
throughout rather than random vectors, so the similarity-ranking and
cross-org isolation tests are meaningful: a query vector genuinely has to
be closer to the "right" chunk's real embedding than to an unrelated
chunk's, not just closer to whatever random floats happened to be passed
in.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest

from app.services import vector_store_service
from app.services.embedding_service import embed_documents, embed_query
from app.services.vector_store_service import (
    ensure_collection,
    get_client,
    search,
    upsert_chunk,
)


@pytest.fixture
def collection_name() -> Generator[str, None, None]:
    """A scratch collection, unique per test, dropped afterward regardless
    of test outcome.
    """
    name = f"test_nexus_chunks_{uuid.uuid4().hex}"
    yield name
    get_client().delete_collection(name)


# --- Collection management --------------------------------------------------


def test_ensure_collection_creates_with_correct_vector_size_and_distance(
    collection_name: str,
) -> None:
    client = get_client()
    assert not client.collection_exists(collection_name)

    ensure_collection(collection_name=collection_name)

    assert client.collection_exists(collection_name)
    info = client.get_collection(collection_name)
    vectors_config = info.config.params.vectors
    assert vectors_config is not None
    expected_size = vector_store_service._vector_size()
    assert vectors_config.size == expected_size
    assert vectors_config.distance.value.lower() == "cosine"


def test_ensure_collection_is_idempotent(collection_name: str) -> None:
    ensure_collection(collection_name=collection_name)
    # Calling it again must not raise, and must not error about the
    # collection already existing.
    ensure_collection(collection_name=collection_name)

    client = get_client()
    assert client.collection_exists(collection_name)


# --- Upsert + search ---------------------------------------------------------


def test_upsert_then_search_within_same_org_returns_the_upserted_point(
    collection_name: str,
) -> None:
    ensure_collection(collection_name=collection_name)

    organization_id = uuid.uuid4()
    document_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    content = "Employees can request parental leave through the HR portal."
    [embedding] = embed_documents([content])

    upsert_chunk(
        chunk_id=chunk_id,
        document_id=document_id,
        organization_id=organization_id,
        embedding=embedding,
        content=content,
        collection_name=collection_name,
    )

    query_vector = embed_query("How do I request parental leave?")
    results = search(
        organization_id=organization_id,
        query_vector=query_vector,
        limit=10,
        collection_name=collection_name,
    )

    assert len(results) == 1
    assert results[0].chunk_id == chunk_id
    assert results[0].document_id == document_id


def test_query_close_to_one_chunk_ranks_it_above_a_far_chunk(collection_name: str) -> None:
    ensure_collection(collection_name=collection_name)
    organization_id = uuid.uuid4()

    relevant_content = "Employees can request parental leave through the HR portal."
    unrelated_content = "The recipe calls for two cups of flour and a teaspoon of salt."
    relevant_vec, unrelated_vec = embed_documents([relevant_content, unrelated_content])

    relevant_chunk_id = uuid.uuid4()
    unrelated_chunk_id = uuid.uuid4()

    upsert_chunk(
        chunk_id=relevant_chunk_id,
        document_id=uuid.uuid4(),
        organization_id=organization_id,
        embedding=relevant_vec,
        content=relevant_content,
        collection_name=collection_name,
    )
    upsert_chunk(
        chunk_id=unrelated_chunk_id,
        document_id=uuid.uuid4(),
        organization_id=organization_id,
        embedding=unrelated_vec,
        content=unrelated_content,
        collection_name=collection_name,
    )

    query_vector = embed_query("How do I request parental leave?")
    results = search(
        organization_id=organization_id,
        query_vector=query_vector,
        limit=10,
        collection_name=collection_name,
    )

    assert len(results) == 2
    assert results[0].chunk_id == relevant_chunk_id
    assert results[1].chunk_id == unrelated_chunk_id
    assert results[0].score > results[1].score


def test_search_respects_limit(collection_name: str) -> None:
    ensure_collection(collection_name=collection_name)
    organization_id = uuid.uuid4()

    contents = [f"Fixture chunk number {i} about internal company policy." for i in range(5)]
    embeddings = embed_documents(contents)
    for content, embedding in zip(contents, embeddings, strict=True):
        upsert_chunk(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            organization_id=organization_id,
            embedding=embedding,
            content=content,
            collection_name=collection_name,
        )

    query_vector = embed_query("What is the company policy?")
    results = search(
        organization_id=organization_id,
        query_vector=query_vector,
        limit=2,
        collection_name=collection_name,
    )

    assert len(results) == 2


# --- Cross-org isolation -----------------------------------------------------


def test_search_never_returns_another_organizations_chunks(collection_name: str) -> None:
    """The real cross-org isolation test: org A and org B each get a chunk
    that would both be top matches for the same query (near-duplicate
    content, so their real embeddings are genuinely close to each other and
    to the query). Searching as org A must never return org B's chunk, even
    though it is vector-similar, because the payload filter -- not
    similarity -- is what decides membership.
    """
    ensure_collection(collection_name=collection_name)

    org_a = uuid.uuid4()
    org_b = uuid.uuid4()

    content_a = "Employees can request parental leave through the HR portal."
    content_b = "Staff may request parental leave via the HR self-service portal."
    vec_a, vec_b = embed_documents([content_a, content_b])

    chunk_id_a = uuid.uuid4()
    chunk_id_b = uuid.uuid4()

    upsert_chunk(
        chunk_id=chunk_id_a,
        document_id=uuid.uuid4(),
        organization_id=org_a,
        embedding=vec_a,
        content=content_a,
        collection_name=collection_name,
    )
    upsert_chunk(
        chunk_id=chunk_id_b,
        document_id=uuid.uuid4(),
        organization_id=org_b,
        embedding=vec_b,
        content=content_b,
        collection_name=collection_name,
    )

    query_vector = embed_query("How do I request parental leave?")

    results_for_org_a = search(
        organization_id=org_a,
        query_vector=query_vector,
        limit=10,
        collection_name=collection_name,
    )
    results_for_org_b = search(
        organization_id=org_b,
        query_vector=query_vector,
        limit=10,
        collection_name=collection_name,
    )

    assert [r.chunk_id for r in results_for_org_a] == [chunk_id_a]
    assert [r.chunk_id for r in results_for_org_b] == [chunk_id_b]


def test_search_with_no_matching_org_returns_empty(collection_name: str) -> None:
    ensure_collection(collection_name=collection_name)

    upsert_chunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        embedding=embed_query("some content"),
        collection_name=collection_name,
    )

    results = search(
        organization_id=uuid.uuid4(),
        query_vector=embed_query("some content"),
        limit=10,
        collection_name=collection_name,
    )

    assert results == []
