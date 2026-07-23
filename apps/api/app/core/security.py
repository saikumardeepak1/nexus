"""FastAPI dependencies that authenticate a request under one of Nexus's two
auth schemes (see docs/TDD.md section 3.6):

- ``require_api_key``: ``Authorization: Bearer nxs_live_...`` -> ``Organization``.
  For programmatic clients (e.g. document upload).
- ``require_session``: ``Authorization: Bearer <jwt>`` -> ``User``.
  For the dashboard.

Both are ordinary FastAPI dependencies (``Depends(require_api_key)`` /
``Depends(require_session)``) so any future route can require one, the
other, or (via a small wrapper) either.
"""

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.models import ApiKey, Organization, User
from app.services import auth_service

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def _extract_bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise _UNAUTHORIZED
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _UNAUTHORIZED
    return token


async def require_api_key(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Organization:
    """Resolve ``Authorization: Bearer nxs_live_...`` to the owning, non-revoked
    Organization. Raises 401 for a missing, malformed, unknown, or revoked key.
    """
    raw_key = _extract_bearer_token(authorization)
    if not raw_key.startswith(auth_service.API_KEY_PREFIX):
        raise _UNAUTHORIZED

    hashed_key = auth_service.hash_api_key(raw_key)
    result = await session.execute(
        select(ApiKey)
        .where(ApiKey.hashed_key == hashed_key)
        .join(Organization, Organization.id == ApiKey.organization_id)
    )
    api_key = result.scalar_one_or_none()
    if api_key is None or api_key.revoked_at is not None:
        raise _UNAUTHORIZED

    organization = await session.get(Organization, api_key.organization_id)
    if organization is None:
        raise _UNAUTHORIZED
    return organization


async def require_session(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Resolve ``Authorization: Bearer <jwt>`` to the User it was issued for.
    Raises 401 for a missing, malformed, expired, tampered, or stale (user no
    longer exists) token.
    """
    token = _extract_bearer_token(authorization)
    try:
        payload = auth_service.decode_access_token(token)
    except auth_service.InvalidTokenError as exc:
        raise _UNAUTHORIZED from exc

    user = await session.get(User, payload.user_id)
    if user is None or user.organization_id != payload.organization_id:
        raise _UNAUTHORIZED
    return user
