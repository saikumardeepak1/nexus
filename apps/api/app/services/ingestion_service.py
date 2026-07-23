"""Validates, stores, and enqueues processing for uploaded documents.

See docs/TDD.md section 3.2 (`ingestion_service`) and section 3.5 (async
processing): upload validates and writes the raw file plus a `Document` row
synchronously (fast, so the upload call returns quickly), then enqueues
`process_document(document_id)` onto Redis. That keeps the upload endpoint's
latency independent of document size and model inference time -- the actual
parse/chunk/embed/index work happens in the worker, in a job whose body is
filled in by later issues (see app/workers/jobs.py).
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import UploadFile
from redis import Redis
from rq import Queue
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Document
from app.services import vector_store_service

logger = logging.getLogger(__name__)

# PRD non-goals restrict v1 ingestion to PDF and plain text (see
# docs/PRD.md "Non-goals": "Multi-modal ingestion ... text and PDF only").
ALLOWED_EXTENSIONS = {".pdf", ".txt"}

# 25MB per upload. Chosen as a generous-but-bounded ceiling: the PRD's
# success criterion is a 50-page PDF fully ingested in under 2 minutes, and a
# 50-page text-heavy PDF is typically a few MB, so 25MB comfortably covers
# realistic documents while still bounding worst-case memory, disk, and
# downstream parsing cost per upload. Easy to raise later (single constant)
# if real corpora need larger files.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Read the upload in fixed-size chunks rather than one `.read()` call so an
# oversized upload can be rejected as soon as it crosses the limit, without
# ever buffering the whole (potentially huge) file in memory first.
_READ_CHUNK_BYTES = 1024 * 1024

_RQ_QUEUE_NAME = "default"
_PROCESS_DOCUMENT_JOB = "app.workers.jobs.process_document"


class UnsupportedFileTypeError(Exception):
    """Raised when the uploaded file's extension is not PDF or plain text."""


class FileTooLargeError(Exception):
    """Raised when the uploaded file exceeds MAX_UPLOAD_BYTES."""


class DocumentNotFoundError(Exception):
    """Raised when a document does not exist or belongs to another organization."""


def _validate_filename(filename: str | None) -> str:
    if not filename:
        raise UnsupportedFileTypeError("A filename is required")
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"Unsupported file type '{extension or '(none)'}'. "
            "Only PDF (.pdf) and plain text (.txt) files are accepted."
        )
    return filename


async def _read_within_limit(upload_file: UploadFile) -> bytes:
    """Read the whole upload into memory, aborting as soon as the running
    total crosses MAX_UPLOAD_BYTES so an oversized file is never fully
    buffered.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload_file.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise FileTooLargeError(
                f"File exceeds the maximum upload size of {MAX_UPLOAD_BYTES} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _storage_path(document_id: uuid.UUID) -> Path:
    return Path(settings.documents_storage_path) / str(document_id)


def _enqueue_process_document(document_id: uuid.UUID) -> None:
    """Enqueue the process_document job by import path rather than a direct
    function reference, so the API process never has to import
    app.workers.jobs (and, with it, whatever heavier dependencies later
    ingestion issues add there) -- RQ resolves the job function inside the
    worker process instead.
    """
    connection = Redis.from_url(settings.redis_url)
    try:
        queue = Queue(_RQ_QUEUE_NAME, connection=connection)
        queue.enqueue(_PROCESS_DOCUMENT_JOB, str(document_id))
    finally:
        connection.close()


async def ingest_document(
    *,
    organization_id: uuid.UUID,
    upload_file: UploadFile,
    session: AsyncSession,
) -> Document:
    """Validate, persist, store, and enqueue processing for an uploaded
    document.

    Raises `UnsupportedFileTypeError` / `FileTooLargeError` for invalid
    uploads; the route layer maps these to 415 / 413 responses. On success,
    the returned `Document` has already been committed with
    `status="queued"`, its raw bytes are on disk, and `process_document` has
    already been enqueued -- all before this function returns, per the
    acceptance criteria in issue #5.
    """
    filename = _validate_filename(upload_file.filename)
    content = await _read_within_limit(upload_file)

    document = Document(organization_id=organization_id, filename=filename, status="queued")
    session.add(document)
    await session.flush()  # assigns document.id without ending the transaction

    storage_path = _storage_path(document.id)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_bytes(content)

    await session.commit()
    await session.refresh(document)

    _enqueue_process_document(document.id)

    logger.info(
        "document ingested and queued for processing",
        extra={"document_id": str(document.id)},
    )

    return document


async def delete_document(
    *,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """Delete a document owned by `organization_id`.

    Raises `DocumentNotFoundError` for a document that doesn't exist or
    belongs to another organization -- the route layer maps this to a 404,
    the same "not found, not not-yours" distinction `get_document` makes.

    Deletes, in order:
    1. The raw file on disk (if it's still there; a document whose
       ingestion job never got as far as expecting a fully-written file, or
       one already cleaned up by a previous partial delete attempt, is not
       treated as an error).
    2. The `Document` row itself. Its `Chunk` rows (and their `Citation`
       rows in turn) cascade at the database level via `ondelete="CASCADE"`
       foreign keys (see app/models/chunk.py, app/models/citation.py), so
       this one delete+commit is enough to remove every Postgres row that
       belonged to the document.
    3. The document's chunk vectors in Qdrant, via
       `vector_store_service.delete_by_document`. This runs after the
       Postgres commit and is treated as best-effort: if Qdrant is
       unreachable or errors, the document is already fully gone from
       Postgres and disk (the parts of "delete a document" a caller can
       actually observe through this API), so the error is logged rather
       than raised -- leaving a few orphaned Qdrant points is preferable to
       reporting a failed delete for a document that in fact no longer
       exists.
    """
    document = await session.get(Document, document_id)
    if document is None or document.organization_id != organization_id:
        raise DocumentNotFoundError(f"Document {document_id} not found")

    storage_path = _storage_path(document.id)
    storage_path.unlink(missing_ok=True)

    await session.delete(document)
    await session.commit()

    try:
        vector_store_service.delete_by_document(document_id)
    except Exception:
        logger.warning(
            "Failed to delete Qdrant points for document %s; Postgres row and "
            "file were already removed, leaving orphaned vector(s) behind.",
            document_id,
            exc_info=True,
        )
