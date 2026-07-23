"""Integration tests for document upload/listing (POST/GET /v1/documents,
GET /v1/documents/{id}), run against a real (test) Postgres via the
``client``/``db_session`` fixtures (see conftest.py) and a real (test)
Redis via the ``rq_queue`` fixture below, so the enqueue assertions prove an
actual job landed on the queue rather than that a function was called.
"""

import io
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest_asyncio
from httpx import AsyncClient
from redis import Redis
from rq import Queue
from rq.job import Job
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Chunk, Document
from app.services.ingestion_service import MAX_UPLOAD_BYTES


def _minimal_pdf_bytes() -> bytes:
    """A tiny, structurally valid single-page PDF. Ingestion at this issue's
    scope only validates the file extension/size and stores raw bytes (real
    PDF parsing lands in a later issue), so the content just needs to be
    plausible PDF bytes, not a rendering-correct document.
    """
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
        b"%%EOF"
    )


@pytest_asyncio.fixture
async def rq_queue() -> AsyncGenerator[Queue, None]:
    """A Queue bound to the same Redis the app enqueues onto (settings.redis_url),
    emptied before and after the test so job-count assertions aren't polluted
    by other tests sharing the instance.
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


async def _create_api_key(client: AsyncClient, access_token: str) -> str:
    response = await client.post(
        "/v1/api-keys", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 201, response.text
    raw_key: str = response.json()["raw_key"]
    return raw_key


async def test_upload_pdf_creates_queued_document_and_enqueues_job(
    client: AsyncClient, rq_queue: Queue
) -> None:
    access_token, organization_id = await _register_and_get_access_token(client)

    response = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {access_token}"},
        files={"file": ("policy.pdf", _minimal_pdf_bytes(), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["filename"] == "policy.pdf"
    assert body["status"] == "queued"
    assert body["organization_id"] == organization_id
    assert uuid.UUID(body["id"])

    # The job was actually enqueued onto Redis, not just that some code ran.
    job_ids = rq_queue.job_ids
    assert len(job_ids) == 1
    job = Job.fetch(job_ids[0], connection=rq_queue.connection)
    assert job.func_name == "app.workers.jobs.process_document"
    assert job.args == (body["id"],)


async def test_upload_text_creates_queued_document_and_enqueues_job(
    client: AsyncClient, rq_queue: Queue
) -> None:
    access_token, organization_id = await _register_and_get_access_token(client)

    response = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {access_token}"},
        files={"file": ("runbook.txt", b"Restart the worker if the queue backs up.", "text/plain")},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["filename"] == "runbook.txt"
    assert body["status"] == "queued"
    assert body["organization_id"] == organization_id

    job_ids = rq_queue.job_ids
    assert len(job_ids) == 1
    job = Job.fetch(job_ids[0], connection=rq_queue.connection)
    assert job.func_name == "app.workers.jobs.process_document"
    assert job.args == (body["id"],)


async def test_upload_document_row_committed_before_response_returns(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The Document row must exist (status=queued) as soon as the upload
    call returns, independent of re-reading the response body -- fetch it
    straight from the database with a fresh query.
    """
    access_token, _ = await _register_and_get_access_token(client)
    response = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {access_token}"},
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    document_id = response.json()["id"]

    result = await db_session.execute(select(Document).where(Document.id == uuid.UUID(document_id)))
    stored = result.scalar_one()
    assert stored.status == "queued"
    assert stored.filename == "notes.txt"


async def test_upload_rejects_unsupported_file_type(client: AsyncClient, rq_queue: Queue) -> None:
    access_token, _ = await _register_and_get_access_token(client)

    response = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {access_token}"},
        files={"file": ("image.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )
    assert response.status_code == 415
    assert response.json()["detail"]
    assert rq_queue.job_ids == []


async def test_upload_rejects_oversized_file(client: AsyncClient, rq_queue: Queue) -> None:
    access_token, _ = await _register_and_get_access_token(client)

    oversized = io.BytesIO(b"a" * (MAX_UPLOAD_BYTES + 1))
    response = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {access_token}"},
        files={"file": ("huge.txt", oversized, "text/plain")},
    )
    assert response.status_code == 413
    assert response.json()["detail"]
    assert rq_queue.job_ids == []


async def test_upload_requires_auth(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/documents",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 401


async def test_upload_works_with_api_key_auth(client: AsyncClient, rq_queue: Queue) -> None:
    access_token, organization_id = await _register_and_get_access_token(client)
    raw_key = await _create_api_key(client, access_token)

    response = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {raw_key}"},
        files={"file": ("notes.txt", b"hello from an api key", "text/plain")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["organization_id"] == organization_id
    assert len(rq_queue.job_ids) == 1


async def test_list_documents_scoped_to_organization(client: AsyncClient) -> None:
    token_a, _ = await _register_and_get_access_token(client, org_name="Org A")
    token_b, _ = await _register_and_get_access_token(client, org_name="Org B")

    await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {token_a}"},
        files={"file": ("a-doc.txt", b"org a content", "text/plain")},
    )
    await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {token_b}"},
        files={"file": ("b-doc.txt", b"org b content", "text/plain")},
    )

    response_a = await client.get(
        "/v1/documents", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert response_a.status_code == 200
    filenames_a = {doc["filename"] for doc in response_a.json()["documents"]}
    assert filenames_a == {"a-doc.txt"}

    response_b = await client.get(
        "/v1/documents", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert response_b.status_code == 200
    filenames_b = {doc["filename"] for doc in response_b.json()["documents"]}
    assert filenames_b == {"b-doc.txt"}


async def test_get_document_scoped_to_organization(client: AsyncClient) -> None:
    token_a, _ = await _register_and_get_access_token(client, org_name="Org A")
    token_b, _ = await _register_and_get_access_token(client, org_name="Org B")

    upload_response = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {token_a}"},
        files={"file": ("secret.txt", b"org a secret", "text/plain")},
    )
    document_id = upload_response.json()["id"]

    own_response = await client.get(
        f"/v1/documents/{document_id}", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert own_response.status_code == 200
    assert own_response.json()["filename"] == "secret.txt"

    other_org_response = await client.get(
        f"/v1/documents/{document_id}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert other_org_response.status_code == 404


async def test_get_document_not_found(client: AsyncClient) -> None:
    access_token, _ = await _register_and_get_access_token(client)
    response = await client.get(
        f"/v1/documents/{uuid.uuid4()}", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 404


async def test_delete_document_removes_row_file_and_chunks(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    access_token, _ = await _register_and_get_access_token(client)

    upload_response = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {access_token}"},
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert upload_response.status_code == 201, upload_response.text
    document_id = uuid.UUID(upload_response.json()["id"])

    storage_path = Path(settings.documents_storage_path) / str(document_id)
    assert storage_path.exists()

    # A chunk row belonging to this document, so the cascade assertion below
    # proves something real is actually being removed, not just an empty
    # Document row with no children.
    db_session.add(
        Chunk(document_id=document_id, chunk_index=0, content="hello world chunk")
    )
    await db_session.commit()

    delete_response = await client.delete(
        f"/v1/documents/{document_id}", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert delete_response.status_code == 204
    assert delete_response.content == b""

    document_result = await db_session.execute(select(Document).where(Document.id == document_id))
    assert document_result.scalar_one_or_none() is None

    chunk_result = await db_session.execute(
        select(Chunk).where(Chunk.document_id == document_id)
    )
    assert chunk_result.scalar_one_or_none() is None

    assert not storage_path.exists()

    list_response = await client.get(
        "/v1/documents", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert list_response.status_code == 200
    assert document_id not in {uuid.UUID(doc["id"]) for doc in list_response.json()["documents"]}


async def test_delete_document_scoped_to_organization(client: AsyncClient) -> None:
    token_a, _ = await _register_and_get_access_token(client, org_name="Org A")
    token_b, _ = await _register_and_get_access_token(client, org_name="Org B")

    upload_response = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {token_a}"},
        files={"file": ("secret.txt", b"org a secret", "text/plain")},
    )
    document_id = upload_response.json()["id"]

    other_org_response = await client.delete(
        f"/v1/documents/{document_id}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert other_org_response.status_code == 404

    # Untouched: still fetchable by its own organization.
    own_response = await client.get(
        f"/v1/documents/{document_id}", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert own_response.status_code == 200


async def test_delete_document_not_found(client: AsyncClient) -> None:
    access_token, _ = await _register_and_get_access_token(client)
    response = await client.delete(
        f"/v1/documents/{uuid.uuid4()}", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 404


async def test_delete_document_second_delete_is_404_not_500(client: AsyncClient) -> None:
    access_token, _ = await _register_and_get_access_token(client)

    upload_response = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {access_token}"},
        files={"file": ("notes.txt", b"hello again", "text/plain")},
    )
    document_id = upload_response.json()["id"]

    first_delete = await client.delete(
        f"/v1/documents/{document_id}", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert first_delete.status_code == 204

    second_delete = await client.delete(
        f"/v1/documents/{document_id}", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert second_delete.status_code == 404


async def test_delete_document_requires_auth(client: AsyncClient) -> None:
    response = await client.delete(f"/v1/documents/{uuid.uuid4()}")
    assert response.status_code == 401
