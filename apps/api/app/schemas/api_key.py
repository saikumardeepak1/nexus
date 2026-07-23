"""Request/response schemas for /v1/api-keys routes."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class ApiKeyCreateResponse(BaseModel):
    """Returned exactly once, at creation. ``raw_key`` is never retrievable
    again after this response, only ``prefix`` is kept for display.
    """

    id: uuid.UUID
    prefix: str
    raw_key: str
    created_at: datetime


class ApiKeyResponse(BaseModel):
    """The persisted, listable view of an API key: no raw secret, ever."""

    id: uuid.UUID
    prefix: str
    created_at: datetime
    revoked_at: datetime | None

    model_config = {"from_attributes": True}
