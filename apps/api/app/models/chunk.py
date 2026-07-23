"""Chunk: a slice of a document's text, embedded and indexed for retrieval.

``content_tsv`` backs Postgres full-text search (the lexical half of hybrid
search, see docs/TDD.md 3.2 ``lexical_search_service``). It is kept in sync
with ``content`` by a Postgres trigger (see the migration in
alembic/versions/) rather than set from application code, so it can never
drift from the text it indexes regardless of how a row is written or
updated.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.citation import Citation
    from app.models.document import Document


class Chunk(UUIDPrimaryKeyMixin, Base):
    """A single chunk of a document's parsed text, plus its retrieval metadata."""

    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_content_tsv", "content_tsv", postgresql_using="gin"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_tsv: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    qdrant_point_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    document: Mapped["Document"] = relationship(back_populates="chunks")
    citations: Mapped[list["Citation"]] = relationship(
        back_populates="chunk", cascade="all, delete-orphan"
    )
