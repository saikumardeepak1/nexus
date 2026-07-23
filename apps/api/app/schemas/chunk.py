"""Request/response schemas for /v1/chunks routes (see issue #17).

Citation display needs to resolve a citation's ``chunk_id`` back to the
document it came from and the chunk's own text -- neither of which the
``/v1/conversations`` citation payload carries (see
``app.schemas.conversation.CitationResponse`` and the SSE ``done`` event in
``app.api.conversations``, both of which only carry ``chunk_id`` plus
scoring/marker metadata). This schema backs a small, focused lookup
endpoint for exactly that purpose, not a general chunk browsing API.
"""

import uuid

from pydantic import BaseModel, ConfigDict


class ChunkResponse(BaseModel):
    """Enough of a chunk's data to render a citation's source panel: which
    document it came from (by filename) and the passage's own text.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "document_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                    "filename": "employee-handbook.pdf",
                    "content": "Employees accrue fifteen days of paid time off per year.",
                    "page_number": 12,
                }
            ]
        },
    )

    id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    content: str
    page_number: int | None
