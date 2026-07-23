"""Request/response schemas for /v1/conversations routes (see issue #16)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ConversationCreateRequest(BaseModel):
    """POST /v1/conversations body. ``title`` is optional: a conversation can
    be created before its first message exists to title it from, and a client
    is free to set/derive a title later (not built here, out of this issue's
    scope).
    """

    title: str | None = None


class ConversationResponse(BaseModel):
    """A conversation's own fields, with no message history -- what
    ``POST /v1/conversations`` and each entry of ``GET /v1/conversations``
    return.
    """

    id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    title: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationListResponse(BaseModel):
    """GET /v1/conversations: every conversation owned by the caller."""

    conversations: list[ConversationResponse]


class CitationResponse(BaseModel):
    """One (message, chunk) grounding link, as returned to a client."""

    id: uuid.UUID
    chunk_id: uuid.UUID
    relevance_score: float

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    """A single turn, with its citations inlined (empty for a user message,
    populated for an assistant message that cited real chunks) so a client
    can render sources without a second round trip.
    """

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    created_at: datetime
    citations: list[CitationResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ConversationDetailResponse(ConversationResponse):
    """GET /v1/conversations/{id}: the conversation plus its full message
    history, oldest first.
    """

    messages: list[MessageResponse] = Field(default_factory=list)


class MessageCreateRequest(BaseModel):
    """POST /v1/conversations/{id}/messages body: the user's new message."""

    content: str
