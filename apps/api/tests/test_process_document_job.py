"""Integration tests for app.workers.jobs.process_document: the real worker
pipeline (parse, chunk, embed, index, flip Document.status), run against a
real (test) Postgres, real (test) Redis, and real (test) Qdrant, all three,
per docs/TDD.md sections 3.2 and 3.5 and issue #7's acceptance criteria.

Why this file doesn't use the shared ``client``/``db_session`` fixtures
------------------------------------------------------------------------
conftest.py's fixtures wrap every test in an outer transaction that is
rolled back at the end (so tests never leak rows into each other), using a
savepoint for any ``commit()`` a route issues. That is the right default
for route-level tests, but it is wrong here: ``process_document`` is
invoked exactly as the RQ worker process invokes it, using the app's own
``async_session_factory``/``engine`` from ``app.core.db``, a different
database connection than the test's transactional ``db_session``. A row
written inside an uncommitted outer transaction on one connection is
invisible to a query on another connection (that is what transaction
isolation means), so ``process_document`` would never see the uploaded
document at all if the upload went through the rollback-wrapped fixtures.
This file uses its own ``real_client`` fixture instead: a plain
``AsyncClient`` over the real app with no ``get_session`` override, so every
write actually commits to the real (test) database and is visible to any
other connection, exactly like the API process and the worker process are
two separate connections in production.

Bridging event loops in-process
--------------------------------
``process_document`` is a synchronous function that calls ``asyncio.run()``
internally (see its module docstring in app/workers/jobs.py), the right
thing for RQ's synchronous worker loop to call, since a real worker process
never has another event loop already running. This test, however, runs
inside pytest-asyncio's own event loop, so calling ``process_document``
directly would raise ("asyncio.run() cannot be called from a running event
loop"). Running it via ``asyncio.to_thread`` gives it a plain thread with no
event loop of its own, where its internal ``asyncio.run()`` behaves exactly
as it does inside the real worker process, the same production code path,
just invoked from a worker thread instead of a worker process.

That still leaves one wrinkle: the app's module-level ``engine`` pools
connections, and an asyncpg connection is permanently bound to the event
loop it was opened on. This test's own coroutine (loop A) and the thread
``process_document`` runs in (loop B, its own fresh loop from
``asyncio.run``) are different loops sharing the same pooled engine, so a
connection opened on one loop can never be safely reused on the other.
``engine.dispose()`` closes every pooled connection and resets the pool;
calling it immediately before and after crossing between loop A and loop B
guarantees each loop only ever opens brand-new connections of its own, so
nothing pooled on one loop is ever handed to the other.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from qdrant_client.http import models as qmodels
from redis import Redis
from rq import Queue
from rq.job import Job
from sqlalchemy import select

from app.core.config import settings
from app.core.db import async_session_factory, engine
from app.models import Chunk, Document
from app.services import vector_store_service
from app.services.chunking_service import chunk_document
from app.services.embedding_service import embed_query
from app.workers.jobs import process_document

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest_asyncio.fixture(autouse=True)
async def _dispose_shared_engine_after_test() -> AsyncGenerator[None, None]:
    """Every test in this file uses the app's real, module-level ``engine``
    (see the module docstring), and pytest-asyncio tears down a fresh event
    loop per test function. Without this, a connection pooled at the end of
    one test (bound to that test's soon-to-be-closed loop) would still sit
    in the pool when the next test's differently-loop coroutine tries to
    check it out, failing with "attached to a different loop". Disposing
    here, still inside this test's own loop, guarantees the pool is always
    empty by the time this loop closes.
    """
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def real_client() -> AsyncGenerator[AsyncClient, None]:
    """An httpx client over the real app with no dependency overrides, so
    every write actually commits (see module docstring for why this test
    file needs that instead of the shared, rollback-wrapped ``client``
    fixture).
    """
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest_asyncio.fixture
async def rq_queue() -> AsyncGenerator[Queue, None]:
    """A Queue bound to the same Redis the app enqueues onto, emptied before
    and after the test (same pattern as tests/test_documents.py).
    """
    connection = Redis.from_url(settings.redis_url)
    queue = Queue("default", connection=connection)
    queue.empty()
    try:
        yield queue
    finally:
        queue.empty()
        connection.close()


async def _register_and_get_access_token(
    client: AsyncClient, org_name: str = "Acme Corp"
) -> tuple[str, str]:
    email = f"user-{uuid.uuid4().hex[:12]}@example.com"
    response = await client.post(
        "/v1/auth/register",
        json={
            "organization_name": org_name,
            "email": email,
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["access_token"], body["user"]["organization_id"]


async def _cross_loop_call(func: object, *args: object) -> None:
    """Call a sync function (``process_document``) from this test's own
    event loop safely, per the "Bridging event loops in-process" note in the
    module docstring: dispose the shared engine's pooled connections
    immediately before and after so neither loop is ever handed a
    connection opened on the other.
    """
    await engine.dispose()
    try:
        await asyncio.to_thread(func, *args)  # type: ignore[arg-type]
    finally:
        await engine.dispose()


async def _delete_qdrant_points_for_document(document_id: uuid.UUID) -> None:
    client = vector_store_service.get_client()
    if not client.collection_exists(vector_store_service.COLLECTION_NAME):
        return
    client.delete(
        collection_name=vector_store_service.COLLECTION_NAME,
        points_selector=qmodels.FilterSelector(
            filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="document_id",
                        match=qmodels.MatchValue(value=str(document_id)),
                    )
                ]
            )
        ),
    )


async def test_process_document_success_end_to_end(
    real_client: AsyncClient, rq_queue: Queue
) -> None:
    access_token, organization_id_str = await _register_and_get_access_token(real_client)
    organization_id = uuid.UUID(organization_id_str)

    pdf_bytes = (FIXTURES_DIR / "multi_page.pdf").read_bytes()
    upload_response = await real_client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {access_token}"},
        files={"file": ("multi_page.pdf", pdf_bytes, "application/pdf")},
    )
    assert upload_response.status_code == 201, upload_response.text
    document_id = uuid.UUID(upload_response.json()["id"])
    assert upload_response.json()["status"] == "queued"

    # The job actually landed on real Redis, not just that ingest_document
    # ran without error.
    job_ids = rq_queue.job_ids
    assert len(job_ids) == 1
    job = Job.fetch(job_ids[0], connection=rq_queue.connection)
    assert job.func_name == "app.workers.jobs.process_document"
    assert job.args == (str(document_id),)

    try:
        # Run the real job body, exactly as RQ would invoke it (see module
        # docstring for the event-loop bridging this requires).
        await _cross_loop_call(process_document, str(document_id))

        async with async_session_factory() as session:
            document = await session.get(Document, document_id)
            assert document is not None
            assert document.status == "ready"
            assert document.error_detail is None
            assert document.page_count == 3

            result = await session.execute(
                select(Chunk)
                .where(Chunk.document_id == document_id)
                .order_by(Chunk.chunk_index)
            )
            persisted_chunks = list(result.scalars().all())

        expected_chunks = chunk_document(pdf_bytes, "application/pdf")
        assert len(persisted_chunks) == len(expected_chunks)
        for persisted, expected in zip(persisted_chunks, expected_chunks, strict=True):
            assert persisted.chunk_index == expected.chunk_index
            assert persisted.content == expected.content
            assert persisted.page_number == expected.page_number
            # Every chunk was actually indexed into Qdrant, and the point id
            # is recorded back onto the row so it can be looked up later.
            assert persisted.qdrant_point_id == str(persisted.id)

        # Searching Qdrant directly, scoped to this organization, returns
        # every chunk that was just indexed.
        query_vector = embed_query("What is the remote work policy?")
        search_results = vector_store_service.search(
            organization_id=organization_id, query_vector=query_vector, limit=50
        )
        found_chunk_ids = {result.chunk_id for result in search_results}
        expected_chunk_ids = {chunk.id for chunk in persisted_chunks}
        assert expected_chunk_ids <= found_chunk_ids
        assert all(result.document_id == document_id for result in search_results)

        # The topmost result should be page 1's chunk (the fixture's actual
        # remote-work-policy text), a real relevance check, not just "some
        # vector was returned".
        top_result_chunk = next(c for c in persisted_chunks if c.id == search_results[0].chunk_id)
        assert top_result_chunk.page_number == 1

        # GET /v1/documents/{id} reflects the same status via the API.
        get_response = await real_client.get(
            f"/v1/documents/{document_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert get_response.status_code == 200
        assert get_response.json()["status"] == "ready"
        assert get_response.json()["error_detail"] is None
    finally:
        await _delete_qdrant_points_for_document(document_id)
        async with async_session_factory() as session:
            document = await session.get(Document, document_id)
            if document is not None:
                await session.delete(document)
                await session.commit()


async def test_process_document_failure_path_marks_document_failed(
    real_client: AsyncClient,
) -> None:
    access_token, _ = await _register_and_get_access_token(real_client, org_name="Broken Corp")

    corrupt_pdf_bytes = b"this is not a real pdf, just garbage bytes with a .pdf extension"
    upload_response = await real_client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {access_token}"},
        files={"file": ("broken.pdf", corrupt_pdf_bytes, "application/pdf")},
    )
    assert upload_response.status_code == 201, upload_response.text
    document_id = uuid.UUID(upload_response.json()["id"])

    try:
        # The job function must return cleanly (no exception) even though
        # the underlying document can never be parsed.
        await _cross_loop_call(process_document, str(document_id))

        async with async_session_factory() as session:
            document = await session.get(Document, document_id)
            assert document is not None
            assert document.status == "failed"
            assert document.error_detail
            # No chunks or vectors should exist for a document that never
            # made it past parsing.
            result = await session.execute(select(Chunk).where(Chunk.document_id == document_id))
            assert result.scalars().all() == []

        get_response = await real_client.get(
            f"/v1/documents/{document_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert get_response.status_code == 200
        assert get_response.json()["status"] == "failed"
        assert get_response.json()["error_detail"]
    finally:
        await _delete_qdrant_points_for_document(document_id)
        async with async_session_factory() as session:
            document = await session.get(Document, document_id)
            if document is not None:
                await session.delete(document)
                await session.commit()


async def test_process_document_missing_document_returns_cleanly() -> None:
    """A document id that doesn't exist (e.g. deleted between enqueue and
    dequeue) must not raise either.
    """
    missing_id = str(uuid.uuid4())
    await _cross_loop_call(process_document, missing_id)
