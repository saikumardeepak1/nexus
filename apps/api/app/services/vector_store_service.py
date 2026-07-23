"""Qdrant vector store integration: collection management, upsert, and dense
k-NN search scoped by organization (see docs/TDD.md section 3.2 and issue #9).

This module is deliberately standalone plumbing: it manages the Qdrant
collection, upserts chunk vectors, and searches them. It does not combine
results with `lexical_search_service` (that fusion is `hybrid_search_service`'s
job, issue #11, built once this lands) and it is not wired into the RQ
ingestion job (that wiring is issue #7, built after this) -- callers are
responsible for calling `ensure_collection`/`upsert_chunk`/`search` from
wherever that logic ends up living.

Organization scoping
--------------------
Chunks have no `organization_id` column directly in Postgres (a chunk
belongs to a `Document`, which belongs to an `Organization` -- see the ERD
in docs/ARCHITECTURE.md). Qdrant is a separate system with its own
filtering model: it has no notion of a join across tables the way Postgres
does, so scoping a search to one organization requires `organization_id` to
be present directly on the point's payload. Storing `organization_id`
(alongside `document_id` and `chunk_id`, so a result can be resolved back to
Postgres rows) in the payload is the standard Qdrant payload-filtering
pattern for multi-tenant data, not a denormalization bug or a deviation from
the Postgres schema -- it is how a system that only speaks
vector-plus-payload achieves the tenant isolation Postgres gets for free
from a foreign key join.

The organization filter is applied as part of the search request itself
(via `query_filter` on `query_points`), not as a post-filter over an
unfiltered top-K. Qdrant evaluates the filter during the vector index
traversal, so a caller always gets up to `limit` results that belong to
their organization, rather than up to `limit` global results that then get
filtered down to fewer (or zero) matches.

Distance metric
----------------
`embedding_service` normalizes every embedding it produces (both
`embed_documents` and `embed_query` call `model.encode(...,
normalize_embeddings=True)`). For unit vectors, cosine similarity is
equivalent to dot product (the magnitude term in the cosine formula is
always 1), and Qdrant's COSINE distance is implemented as exactly that.
COSINE is therefore the metric that matches what `embedding_service`
actually hands this module -- not an arbitrary choice, and not something
like Euclidean distance, which remains well-defined for unit vectors but
measures a notion of "distance" (straight-line separation on the unit
sphere) that isn't what the embedding model was trained to make
meaningful; cosine/dot-product similarity is.

Vector size
-----------
The collection's vector size is derived from the embedding model's actual
output dimension (`embedding_service.get_model().get_sentence_embedding_dimension()`)
rather than a hardcoded literal, so the collection config always matches
whatever `settings.embedding_model_name` actually produces -- if that model
ever changes to one with a different output dimension, this module's
collection config follows automatically instead of silently drifting out of
sync with a stale constant.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from app.core.config import settings
from app.services.embedding_service import get_model

# Name of the single Qdrant collection all chunk vectors live in. Not
# per-organization: organization isolation is enforced by the
# `organization_id` payload filter on every upsert/search, not by
# separating organizations into different collections.
COLLECTION_NAME = "nexus_chunks"

_ORGANIZATION_ID_KEY = "organization_id"
_DOCUMENT_ID_KEY = "document_id"
_CHUNK_ID_KEY = "chunk_id"
_CONTENT_KEY = "content"

_client: QdrantClient | None = None
_client_lock = threading.Lock()


def get_client() -> QdrantClient:
    """Return the process-wide `QdrantClient`, constructing it on first use.

    Mirrors the lazy-singleton pattern `embedding_service.get_model` and
    `reranking_service._get_model` use: constructing a client opens a
    connection, which is unnecessary work to repeat per call, so every
    call after the first reuses the same instance.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = QdrantClient(url=settings.qdrant_url)
    return _client


def _vector_size() -> int:
    """The embedding model's actual output dimension (see the "Vector size"
    note in the module docstring for why this is derived rather than
    hardcoded).
    """
    dimension = get_model().get_sentence_embedding_dimension()
    assert dimension is not None, "embedding model did not report a vector dimension"
    return dimension


@dataclass(frozen=True)
class VectorSearchResult:
    """One point matched by dense k-NN search, plus its similarity score.

    Deliberately small and independent of Qdrant's own point/response
    shapes, so callers (`hybrid_search_service`, later) have a clean, stable
    contract to build on -- the same shape `LexicalSearchResult` fills for
    `lexical_search_service` and `RerankCandidate` fills for
    `reranking_service`.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    score: float


def ensure_collection(
    client: QdrantClient | None = None,
    collection_name: str = COLLECTION_NAME,
) -> None:
    """Create `collection_name` with the correct vector size and distance
    metric if it does not already exist.

    Safe to call every time, e.g. once at API/worker process startup: if the
    collection already exists (its config is never re-validated or
    recreated), this is a single cheap existence check and nothing else, so
    calling it twice (or on every process start) never errors and never
    duplicates or resets a collection that already has data in it.

    Args:
        client: an existing `QdrantClient`, or `None` to use the
            process-wide singleton from `get_client`.
        collection_name: defaults to `COLLECTION_NAME`; overridable so
            tests can point this at a scratch collection instead of the
            real one.
    """
    client = client or get_client()
    if client.collection_exists(collection_name):
        return
    client.create_collection(
        collection_name=collection_name,
        vectors_config=qmodels.VectorParams(
            size=_vector_size(),
            distance=qmodels.Distance.COSINE,
        ),
    )


def upsert_chunk(
    chunk_id: uuid.UUID,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    embedding: list[float],
    content: str | None = None,
    client: QdrantClient | None = None,
    collection_name: str = COLLECTION_NAME,
) -> None:
    """Upsert one chunk's vector, keyed by `chunk_id`.

    `organization_id` and `document_id` are stored on the point's payload
    alongside `chunk_id` itself (see the "Organization scoping" note in the
    module docstring) so `search` can filter by organization and so a
    search result can be resolved back to its Postgres `Chunk`/`Document`
    rows without a second round trip. `content` is optional and stored only
    for convenience (e.g. inspecting a point without a Postgres join); it is
    never read back by `search`.

    Upserting with the same `chunk_id` again replaces that point in place
    (Qdrant's upsert semantics), so re-processing a document is safe to
    re-run without leaving stale duplicate points behind.

    Args:
        chunk_id: the chunk's Postgres primary key; becomes the point ID.
        document_id: the chunk's parent document, stored on the payload.
        organization_id: the owning organization, stored on the payload and
            used by `search` to scope results.
        embedding: the chunk's embedding vector (from
            `embedding_service.embed_documents`).
        content: the chunk's text, stored on the payload for convenience.
        client: an existing `QdrantClient`, or `None` to use the
            process-wide singleton from `get_client`.
        collection_name: defaults to `COLLECTION_NAME`; overridable so
            tests can point this at a scratch collection instead of the
            real one.
    """
    client = client or get_client()
    payload: dict[str, str] = {
        _ORGANIZATION_ID_KEY: str(organization_id),
        _DOCUMENT_ID_KEY: str(document_id),
        _CHUNK_ID_KEY: str(chunk_id),
    }
    if content is not None:
        payload[_CONTENT_KEY] = content

    client.upsert(
        collection_name=collection_name,
        points=[
            qmodels.PointStruct(
                id=str(chunk_id),
                vector=embedding,
                payload=payload,
            )
        ],
    )


def search(
    organization_id: uuid.UUID,
    query_vector: list[float],
    limit: int = 10,
    client: QdrantClient | None = None,
    collection_name: str = COLLECTION_NAME,
) -> list[VectorSearchResult]:
    """Run a dense k-NN search over `query_vector`, scoped to `organization_id`.

    The `organization_id` filter is applied as part of the Qdrant search
    request itself (`query_filter`), not as a post-filter over an
    unfiltered top-K -- see the "Organization scoping" note in the module
    docstring for why that distinction matters for correctness. A caller
    can never receive another organization's chunks, even if their vectors
    are the closest match to `query_vector` in the whole collection.

    Args:
        organization_id: the tenant to scope results to.
        query_vector: the query's embedding vector (from
            `embedding_service.embed_query`), same vector space as the
            embeddings passed to `upsert_chunk`.
        limit: maximum number of results to return, ordered by descending
            similarity score.
        client: an existing `QdrantClient`, or `None` to use the
            process-wide singleton from `get_client`.
        collection_name: defaults to `COLLECTION_NAME`; overridable so
            tests can point this at a scratch collection instead of the
            real one.

    Returns:
        Up to `limit` `VectorSearchResult`, highest score first. Empty if
        nothing in `organization_id`'s corpus has been indexed yet.
    """
    client = client or get_client()
    query_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key=_ORGANIZATION_ID_KEY,
                match=qmodels.MatchValue(value=str(organization_id)),
            )
        ]
    )

    response = client.query_points(
        collection_name=collection_name,
        query=query_vector,
        query_filter=query_filter,
        limit=limit,
        with_payload=True,
    )

    results = []
    for point in response.points:
        assert point.payload is not None
        results.append(
            VectorSearchResult(
                chunk_id=uuid.UUID(point.payload[_CHUNK_ID_KEY]),
                document_id=uuid.UUID(point.payload[_DOCUMENT_ID_KEY]),
                score=point.score,
            )
        )
    return results
