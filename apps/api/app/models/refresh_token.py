"""RefreshToken: a rotating, revocable credential backing JWT session refresh.

Access tokens are short-lived, stateless JWTs (never persisted). Refresh
tokens are opaque random secrets, shown once at issuance and stored here as
a deterministic hash (see app.services.auth_service.hash_refresh_token) so a
presented token can be looked up directly instead of compared one-by-one.
Rotation marks the consumed row's ``revoked_at`` and inserts a new row, so a
stolen-and-reused refresh token can never mint more than one extra access
token before showing up as already-revoked.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class RefreshToken(UUIDPrimaryKeyMixin, Base):
    """A hashed, revocable refresh token belonging to a single user."""

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    hashed_token: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")
