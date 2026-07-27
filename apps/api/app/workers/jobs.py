"""RQ job definitions run by the worker process.

``process_document`` is enqueued by ``app.services.ingestion_service`` right
after a document upload is persisted (see docs/TDD.md sections 3.2 and 3.5).

The pipeline below does the real work: it looks up the ``Document`` row,
flips its status to ``processing``, reads the raw bytes already written to
disk by ``ingestion_service``, parses/chunks them (``chunking_service``),
embeds every chunk in one batched call (``embedding_service``), writes the
resulting ``Chunk`` rows to Postgres, upserts each chunk's vector into
Qdrant (``vector_store_service``), and finally flips ``Document.status`` to
``ready``. Any failure along the way (unsupported file type, a corrupt/
unparsable document, an embedding or Qdrant error, anything else) is caught,
recorded on ``Document.error_detail``, and turned into ``status="failed"``
rather than allowed to propagate -- RQ retries a job that raises, and a
permanently-broken document (e.g. a corrupt PDF) would just fail the same
way forever, so this job always returns normally.

Sync/async bridging
--------------------
RQ workers call job functions synchronously (see ``app.workers.worker``),
but the app's database access is async-only (``app.core.db``'s
``async_session_factory``, the same one route handlers use via
``get_session``). ``process_document`` itself stays a plain sync function
(so RQ's job-resolution/`Job.func_name` contract is unchanged) and its
entire body runs inside a single ``asyncio.run(...)`` call around an inner
async function -- the standard way to bridge a sync entrypoint into async
code without ever leaving an unawaited coroutine behind. Each database
"phase" (mark processing, mark ready, mark failed) opens and closes its own
``async_session_factory()`` session rather than holding one open across the
slow, CPU-bound chunking/embedding work in between, so a database connection
is never held idle for the duration of model inference.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from app.core.config import settings
from app.core.db import async_session_factory
from app.core.logging import correlation_id_var
from app.models import Chunk, Document
from app.services import chunking_service, vector_store_service
from app.services.embedding_service import embed_documents

logger = logging.getLogger(__name__)


def _storage_path(document_id: uuid.UUID) -> Path:
    """Mirror of ``ingestion_service._storage_path``: the worker reads the
    same file the API process wrote, from the same shared
    ``documents_storage_path`` directory (a Docker named volume in
    production, see infra/docker-compose.yml).
    """
    return Path(settings.documents_storage_path) / str(document_id)


async def _mark_processing(document_id: uuid.UUID) -> tuple[uuid.UUID, str] | None:
    """Flip ``Document.status`` to ``processing`` and clear any stale
    ``error_detail`` from a previous failed attempt. Returns the document's
    ``(organization_id, filename)`` for the caller to use in later phases
    (which run in their own sessions, see the module docstring), or ``None``
    if no such document exists.
    """
    async with async_session_factory() as session:
        document = await session.get(Document, document_id)
        if document is None:
            return None
        document.status = "processing"
        document.error_detail = None
        await session.commit()
        return document.organization_id, document.filename


async def _mark_ready(
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    chunks: list[chunking_service.Chunk],
    embeddings: list[list[float]],
) -> None:
    """Persist one ``Chunk`` row per parsed chunk, upsert each into Qdrant,
    and flip ``Document.status`` to ``ready``, all in one transaction.
    """
    async with async_session_factory() as session:
        chunk_rows = [
            Chunk(
                document_id=document_id,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                page_number=chunk.page_number,
            )
            for chunk in chunks
        ]
        session.add_all(chunk_rows)
        await session.flush()  # assigns each chunk_row.id without ending the transaction

        vector_store_service.ensure_collection()
        for chunk_row, embedding in zip(chunk_rows, embeddings, strict=True):
            vector_store_service.upsert_chunk(
                chunk_id=chunk_row.id,
                document_id=document_id,
                organization_id=organization_id,
                embedding=embedding,
                content=chunk_row.content,
            )
            # The Qdrant point id is the chunk's own id (see upsert_chunk's
            # contract), stored back so a chunk row can be resolved to its
            # vector-store point without recomputing anything.
            chunk_row.qdrant_point_id = str(chunk_row.id)

        page_numbers = [chunk.page_number for chunk in chunks if chunk.page_number is not None]

        document = await session.get(Document, document_id)
        assert document is not None, "document disappeared mid-job"
        document.status = "ready"
        if page_numbers:
            document.page_count = max(page_numbers)

        await session.commit()


async def _mark_failed(document_id: uuid.UUID, error: Exception) -> None:
    """Record a failed ingestion: ``status="failed"`` plus a human-readable
    ``error_detail``, in its own fresh session/transaction so a failure that
    happened mid-transaction elsewhere never leaves this write blocked on a
    session that is itself in a bad state.
    """
    logger.error(
        "process_document failed",
        extra={"document_id": str(document_id), "error": str(error)},
    )
    async with async_session_factory() as session:
        document = await session.get(Document, document_id)
        if document is None:
            return
        document.status = "failed"
        document.error_detail = str(error)
        await session.commit()


async def _process_document_async(document_id: str) -> None:
    doc_uuid = uuid.UUID(document_id)

    identity = await _mark_processing(doc_uuid)
    if identity is None:
        logger.error(
            "process_document: no such document, skipping", extra={"document_id": document_id}
        )
        return
    organization_id, filename = identity

    try:
        content = _storage_path(doc_uuid).read_bytes()
        chunks = chunking_service.chunk_document(content, "", filename=filename)
        embeddings = embed_documents([chunk.content for chunk in chunks])
    except Exception as exc:  # noqa: BLE001 - any parse/embedding failure marks the doc failed
        await _mark_failed(doc_uuid, exc)
        return

    try:
        await _mark_ready(doc_uuid, organization_id, chunks, embeddings)
    except Exception as exc:  # noqa: BLE001 - any indexing/persistence failure marks the doc failed
        await _mark_failed(doc_uuid, exc)
        return

    logger.info("process_document completed", extra={"document_id": document_id})


def process_document(document_id: str) -> None:
    """Process an uploaded document: parse, chunk, embed, and index it.

    Runs the full pipeline (see module docstring) via ``asyncio.run``, so
    this stays a plain sync function callable by RQ's worker loop. Never
    raises: any failure is caught inside the pipeline and recorded on the
    ``Document`` row as ``status="failed"`` plus ``error_detail`` instead,
    so a permanently-broken document (a corrupt PDF, an unsupported file
    type) fails once and stays failed rather than being retried forever by
    RQ. The outer ``except`` below is a last-resort safety net for a truly
    unexpected crash (e.g. the database itself being unreachable) that
    happens outside the pipeline's own error handling.

    Sets a ``job-<hex>`` id on ``correlation_id_var`` for the duration of the
    job, the worker-side equivalent of ``CorrelationIdMiddleware`` on the API
    side (see app/core/logging.py and app/core/middleware.py): every log line
    the pipeline emits, including from the services it calls into, carries
    the same id. The ``reset`` in ``finally`` runs whether the job succeeds,
    is caught by ``_process_document_async``'s own error handling, or hits
    the outer safety-net ``except`` below, so the id never leaks into
    whatever job this same worker process picks up next.
    """
    token = correlation_id_var.set(f"job-{uuid.uuid4().hex}")
    try:
        asyncio.run(_process_document_async(document_id))
    except Exception:  # noqa: BLE001 - job bodies must never raise, see docstring
        logger.exception(
            "process_document crashed outside its own error handling",
            extra={"document_id": document_id},
        )
    finally:
        correlation_id_var.reset(token)
