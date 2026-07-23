"""Citation: links an assistant message to the chunk it drew its answer from."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.chunk import Chunk
    from app.models.message import Message


class Citation(UUIDPrimaryKeyMixin, Base):
    """One (message, chunk) grounding link with the reranker's relevance score."""

    __tablename__ = "citations"

    message_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False
    )
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False)

    message: Mapped["Message"] = relationship(back_populates="citations")
    chunk: Mapped["Chunk"] = relationship(back_populates="citations")
