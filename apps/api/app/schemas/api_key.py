"""Request/response schemas for /v1/api-keys routes."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ApiKeyCreateResponse(BaseModel):
    """Returned exactly once, at creation. ``raw_key`` is never retrievable
    again after this response, only ``prefix`` is kept for display.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "prefix": "nxs_live_ab12",
                    "raw_key": "nxs_live_ab12cd34ef56gh78ij90kl12mn34op56",
                    "created_at": "2026-07-23T09:15:00Z",
                }
            ]
        }
    )

    id: uuid.UUID
    prefix: str
    raw_key: str
    created_at: datetime


class ApiKeyResponse(BaseModel):
    """The persisted, listable view of an API key: no raw secret, ever."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "prefix": "nxs_live_ab12",
                    "created_at": "2026-07-23T09:15:00Z",
                    "revoked_at": None,
                }
            ]
        },
    )

    id: uuid.UUID
    prefix: str
    created_at: datetime
    revoked_at: datetime | None
