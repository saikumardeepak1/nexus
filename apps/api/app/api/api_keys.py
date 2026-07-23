"""API key issuance and revocation. Session-authenticated (a dashboard user
creates programmatic credentials for their own organization).
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import require_session
from app.models import ApiKey, User
from app.schemas.api_key import ApiKeyCreateResponse
from app.services import auth_service

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])


@router.post("", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    current_user: User = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyCreateResponse:
    """Generate a new API key for the caller's organization. The raw key is
    returned exactly once, in this response; only its hash is persisted, so
    it is never retrievable again after this call.

    Requires a JWT session (``Authorization: Bearer <jwt>``); API keys
    cannot be used to create other API keys.
    """
    generated = auth_service.generate_api_key()
    api_key = ApiKey(
        organization_id=current_user.organization_id,
        prefix=generated.prefix,
        hashed_key=generated.hashed_key,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)

    return ApiKeyCreateResponse(
        id=api_key.id,
        prefix=api_key.prefix,
        raw_key=generated.raw_key,
        created_at=api_key.created_at,
    )


@router.delete("/{api_key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    api_key_id: uuid.UUID,
    current_user: User = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Revoke an API key belonging to the caller's organization. Scoped by
    organization_id so one org can never revoke (or even discover the
    existence of, via a distinguishable error) another org's key.

    Requires a JWT session (``Authorization: Bearer <jwt>``). 404s (rather
    than 403s) for a key that exists but belongs to another organization, so
    a caller cannot distinguish "not found" from "not yours" and probe for
    other orgs' key ids. Revoking an already-revoked key is a no-op that
    still returns 204.
    """
    api_key = await session.get(ApiKey, api_key_id)
    if api_key is None or api_key.organization_id != current_user.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(UTC)
        session.add(api_key)
        await session.commit()
