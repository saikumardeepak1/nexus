"""Request/response schemas for /v1/documents routes."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DocumentResponse(BaseModel):
    """Status and metadata for a single uploaded document."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "organization_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                    "filename": "employee-handbook.pdf",
                    "status": "ready",
                    "page_count": 24,
                    "error_detail": None,
                    "created_at": "2026-07-23T09:15:00Z",
                }
            ]
        },
    )

    id: uuid.UUID
    organization_id: uuid.UUID
    filename: str
    status: str
    page_count: int | None
    error_detail: str | None
    created_at: datetime


class DocumentListResponse(BaseModel):
    """GET /v1/documents: every document owned by the caller's organization."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "documents": [
                        {
                            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                            "organization_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                            "filename": "employee-handbook.pdf",
                            "status": "ready",
                            "page_count": 24,
                            "error_detail": None,
                            "created_at": "2026-07-23T09:15:00Z",
                        }
                    ]
                }
            ]
        }
    )

    documents: list[DocumentResponse]
