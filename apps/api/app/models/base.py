"""Shared model building blocks."""

import uuid

from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    """Adds a UUID primary key, generated application-side, to a model.

    Every core entity in docs/ARCHITECTURE.md uses a ``uuid id PK``, so this
    is shared across all of them rather than repeated per model.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
