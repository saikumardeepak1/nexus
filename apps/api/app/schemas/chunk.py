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

from pydantic import BaseModel


class ChunkResponse(BaseModel):
    """Enough of a chunk's data to render a citation's source panel: which
    document it came from (by filename) and the passage's own text.
    """

    id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    content: str
    page_number: int | None

    model_config = {"from_attributes": True}
