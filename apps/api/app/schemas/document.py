"""Request/response schemas for /v1/documents routes."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class DocumentResponse(BaseModel):
    """Status and metadata for a single uploaded document."""

    id: uuid.UUID
    organization_id: uuid.UUID
    filename: str
    status: str
    page_count: int | None
    error_detail: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentListResponse(BaseModel):
    """GET /v1/documents: every document owned by the caller's organization."""

    documents: list[DocumentResponse]
