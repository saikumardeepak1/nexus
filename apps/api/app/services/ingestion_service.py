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

import uuid
from pathlib import Path

from fastapi import UploadFile
from redis import Redis
from rq import Queue
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Document

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

    return document
