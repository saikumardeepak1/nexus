"""Request/response schemas for /v1/conversations routes (see issue #16)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreateRequest(BaseModel):
    """POST /v1/conversations body. ``title`` is optional: a conversation can
    be created before its first message exists to title it from, and a client
    is free to set/derive a title later (not built here, out of this issue's
    scope).
    """

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"title": "PTO policy question"}]}
    )

    title: str | None = None


class ConversationResponse(BaseModel):
    """A conversation's own fields, with no message history -- what
    ``POST /v1/conversations`` and each entry of ``GET /v1/conversations``
    return.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "organization_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                    "user_id": "9b2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
                    "title": "PTO policy question",
                    "created_at": "2026-07-23T09:15:00Z",
                }
            ]
        },
    )

    id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    title: str | None
    created_at: datetime


class ConversationListResponse(BaseModel):
    """GET /v1/conversations: every conversation owned by the caller."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "conversations": [
                        {
                            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                            "organization_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                            "user_id": "9b2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
                            "title": "PTO policy question",
                            "created_at": "2026-07-23T09:15:00Z",
                        }
                    ]
                }
            ]
        }
    )

    conversations: list[ConversationResponse]


class CitationResponse(BaseModel):
    """One (message, chunk) grounding link, as returned to a client."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
                    "chunk_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                    "relevance_score": 0.87,
                }
            ]
        },
    )

    id: uuid.UUID
    chunk_id: uuid.UUID
    relevance_score: float


class MessageResponse(BaseModel):
    """A single turn, with its citations inlined (empty for a user message,
    populated for an assistant message that cited real chunks) so a client
    can render sources without a second round trip.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "conversation_id": "9b2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
                    "role": "assistant",
                    "content": "Employees accrue fifteen PTO days per year.",
                    "created_at": "2026-07-23T09:15:05Z",
                    "citations": [
                        {
                            "id": "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
                            "chunk_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                            "relevance_score": 0.87,
                        }
                    ],
                }
            ]
        },
    )

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    created_at: datetime
    citations: list[CitationResponse] = Field(default_factory=list)


class ConversationDetailResponse(ConversationResponse):
    """GET /v1/conversations/{id}: the conversation plus its full message
    history, oldest first.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "organization_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                    "user_id": "9b2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
                    "title": "PTO policy question",
                    "created_at": "2026-07-23T09:15:00Z",
                    "messages": [
                        {
                            "id": "2a3b4c5d-6e7f-4a5b-8c9d-0e1f2a3b4c5d",
                            "conversation_id": "9b2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
                            "role": "user",
                            "content": "How many PTO days do employees get?",
                            "created_at": "2026-07-23T09:15:00Z",
                            "citations": [],
                        },
                        {
                            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                            "conversation_id": "9b2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
                            "role": "assistant",
                            "content": "Employees accrue fifteen PTO days per year.",
                            "created_at": "2026-07-23T09:15:05Z",
                            "citations": [
                                {
                                    "id": "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
                                    "chunk_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                                    "relevance_score": 0.87,
                                }
                            ],
                        },
                    ],
                }
            ]
        },
    )

    messages: list[MessageResponse] = Field(default_factory=list)


class MessageCreateRequest(BaseModel):
    """POST /v1/conversations/{id}/messages body: the user's new message."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"content": "How many PTO days do employees get?"}]
        }
    )

    content: str
