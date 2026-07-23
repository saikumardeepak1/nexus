"""Document upload and listing routes (see docs/TDD.md section 3.4).

``POST /v1/documents`` accepts either auth scheme (API key or JWT session,
see ``app.core.security.require_organization``) since TDD.md notes it's
"available for programmatic document upload" as well as dashboard use.
Every route is scoped to the caller's organization_id so one org can never
see or fetch another org's documents.
"""

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import require_organization
from app.models import Document
from app.schemas.document import DocumentListResponse, DocumentResponse
from app.services import ingestion_service

router = APIRouter(prefix="/v1/documents", tags=["documents"])


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    organization_id: uuid.UUID = Depends(require_organization),
    session: AsyncSession = Depends(get_session),
) -> Document:
    """Upload a PDF or plain-text document. Validates type and size, stores
    the raw file, persists a ``Document`` row with ``status="queued"``, and
    enqueues ``process_document`` -- all before this call returns (see
    issue #5's acceptance criteria).
    """
    try:
        return await ingestion_service.ingest_document(
            organization_id=organization_id, upload_file=file, session=session
        )
    except ingestion_service.UnsupportedFileTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except ingestion_service.FileTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    organization_id: uuid.UUID = Depends(require_organization),
    session: AsyncSession = Depends(get_session),
) -> DocumentListResponse:
    """List every document owned by the caller's organization, newest first."""
    result = await session.execute(
        select(Document)
        .where(Document.organization_id == organization_id)
        .order_by(Document.created_at.desc())
    )
    documents = list(result.scalars().all())
    return DocumentListResponse(
        documents=[DocumentResponse.model_validate(document) for document in documents]
    )


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: uuid.UUID,
    organization_id: uuid.UUID = Depends(require_organization),
    session: AsyncSession = Depends(get_session),
) -> Document:
    """Fetch a single document's status and metadata. 404s (rather than
    403s) for a document that exists but belongs to another organization,
    so a caller cannot distinguish "not found" from "not yours" and probe
    for other orgs' document ids.
    """
    document = await session.get(Document, document_id)
    if document is None or document.organization_id != organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    organization_id: uuid.UUID = Depends(require_organization),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a document: its raw file, its Postgres row (chunks and
    citations cascade), and its Qdrant vectors (best-effort, see
    ``ingestion_service.delete_document``). 404s (rather than 403s) for a
    document that exists but belongs to another organization, matching
    ``get_document``'s not-found-vs-not-yours behavior.
    """
    try:
        await ingestion_service.delete_document(
            organization_id=organization_id, document_id=document_id, session=session
        )
    except ingestion_service.DocumentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        ) from exc
