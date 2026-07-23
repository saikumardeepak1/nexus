"""Chunk lookup route (see docs/TDD.md and issue #17).

Scoping decision
------------------
The chat UI's acceptance criteria (issue #17) require a citation marker to
resolve to "the source document name + chunk text." Neither
``app.schemas.conversation.CitationResponse`` (persisted citations, returned
by ``GET /v1/conversations/{id}``) nor the SSE ``done`` event's citation
payload (``app.api.conversations.send_message``) carry that: both only
carry ``chunk_id`` plus scoring/marker metadata, by design (see
``app.services.generation_service``'s "Citation marker format" note) -- a
citation links a message to *which* chunk supported a claim, it was never
meant to duplicate the chunk's own content inline.

Rather than fatten every citation payload with the full chunk body (most
citations are never clicked, so that would ship unused bytes on every
message), this adds one small, focused lookup endpoint the client calls
only when a citation marker is actually clicked: ``GET /v1/chunks/{id}``
resolves a chunk id back to its owning document's filename plus the
chunk's own text and page number. Not a general chunk-browsing API (no
list endpoint, no chunk search) -- just enough to render a citation's
source panel.

Scoped by ``organization_id`` via ``require_organization`` (either auth
scheme, same as ``documents.py``): a chunk belongs to a document, and every
document in an organization is visible to every user in that organization
(see docs/PRD.md's non-goals: "Role-based per-document access control
beyond organization-level isolation"), so this does not need the stricter
per-user scoping ``conversations.py`` uses for conversations themselves.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import require_organization
from app.models import Chunk, Document
from app.schemas.chunk import ChunkResponse

router = APIRouter(prefix="/v1/chunks", tags=["chunks"])


@router.get("/{chunk_id}", response_model=ChunkResponse)
async def get_chunk(
    chunk_id: uuid.UUID,
    organization_id: uuid.UUID = Depends(require_organization),
    session: AsyncSession = Depends(get_session),
) -> ChunkResponse:
    """Resolve a chunk id to its document filename, own text, and page
    number. 404s (rather than 403s) for a chunk that exists but belongs to
    another organization's document, matching ``documents.py``'s
    not-found-vs-not-yours pattern.
    """
    result = await session.execute(
        select(Chunk, Document.filename)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.id == chunk_id)
        .where(Document.organization_id == organization_id)
    )
    row = result.first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chunk not found")
    chunk, filename = row
    return ChunkResponse(
        id=chunk.id,
        document_id=chunk.document_id,
        filename=filename,
        content=chunk.content,
        page_number=chunk.page_number,
    )
