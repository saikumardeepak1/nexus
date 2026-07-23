"""Auth routes: register (bootstrap an organization + its first user), login,
and refresh (rotating refresh-token flow). See docs/TDD.md section 3.6.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.models import Organization, RefreshToken, User
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPairResponse,
    UserResponse,
)
from app.services import auth_service

router = APIRouter(prefix="/v1/auth", tags=["auth"])


async def _issue_token_pair(session: AsyncSession, user: User) -> TokenPairResponse:
    access_token = auth_service.create_access_token(
        user_id=user.id, organization_id=user.organization_id
    )
    generated_refresh = auth_service.generate_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            hashed_token=generated_refresh.hashed_token,
            expires_at=auth_service.refresh_token_expiry(),
        )
    )
    await session.commit()

    return TokenPairResponse(
        access_token=access_token,
        refresh_token=generated_refresh.raw_token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserResponse.model_validate(user),
    )


@router.post(
    "/register", response_model=TokenPairResponse, status_code=status.HTTP_201_CREATED
)
async def register(
    body: RegisterRequest, session: AsyncSession = Depends(get_session)
) -> TokenPairResponse:
    """Bootstrap a brand-new organization and its first (admin) user, and log
    that user in immediately. The only way a first Organization/User pair
    comes into existence in Nexus.
    """
    # Organization has no unique constraint to violate (unlike email below),
    # so no IntegrityError branch is needed here.
    organization = Organization(name=body.organization_name)
    session.add(organization)
    await session.flush()

    user = User(
        organization_id=organization.id,
        email=body.email,
        hashed_password=auth_service.hash_password(body.password),
        role="admin",
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        ) from exc

    return await _issue_token_pair(session, user)


@router.post("/login", response_model=TokenPairResponse)
async def login(
    body: LoginRequest, session: AsyncSession = Depends(get_session)
) -> TokenPairResponse:
    result = await session.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    invalid_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
    )
    if user is None:
        raise invalid_credentials
    if not auth_service.verify_password(body.password, user.hashed_password):
        raise invalid_credentials

    return await _issue_token_pair(session, user)


@router.post("/refresh", response_model=TokenPairResponse)
async def refresh(
    body: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> TokenPairResponse:
    """Exchange a refresh token for a new access + refresh token pair. The
    presented refresh token is revoked as part of this call (rotation): it
    cannot be used a second time, whether by the legitimate client or by an
    attacker who intercepted it.
    """
    invalid_token = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
    )

    hashed_token = auth_service.hash_refresh_token(body.refresh_token)
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.hashed_token == hashed_token)
    )
    token_row = result.scalar_one_or_none()

    if token_row is None or token_row.revoked_at is not None:
        raise invalid_token
    if token_row.expires_at <= datetime.now(UTC):
        raise invalid_token

    user = await session.get(User, token_row.user_id)
    if user is None:
        raise invalid_token

    token_row.revoked_at = datetime.now(UTC)
    session.add(token_row)
    await session.flush()

    return await _issue_token_pair(session, user)
