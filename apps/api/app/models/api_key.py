"""ApiKey: a programmatic credential (nxs_live_...) for an organization."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.organization import Organization


class ApiKey(UUIDPrimaryKeyMixin, Base):
    """A hashed API key. ``prefix`` is the short, non-secret display prefix
    (e.g. ``nxs_live_ab12``) shown in the dashboard; ``hashed_key`` is the
    salted hash of the full secret, which is only ever shown once at creation.
    """

    __tablename__ = "api_keys"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    hashed_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="api_keys")
