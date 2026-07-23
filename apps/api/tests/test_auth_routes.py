"""Integration tests for /v1/auth/register, /v1/auth/login, and
/v1/auth/refresh, run against a real (test) Postgres via the ``client``
fixture (see conftest.py).
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import RefreshToken, User


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.com"


async def _register(client: AsyncClient, *, email: str | None = None) -> dict:
    response = await client.post(
        "/v1/auth/register",
        json={
            "organization_name": "Acme Corp",
            "email": email or _unique_email(),
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_register_creates_org_and_user_and_returns_token_pair(
    client: AsyncClient,
) -> None:
    body = await _register(client)

    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["user"]["email"]
    assert body["user"]["role"] == "admin"
    assert body["user"]["organization_id"]


async def test_register_duplicate_email_is_rejected(client: AsyncClient) -> None:
    email = _unique_email()
    await _register(client, email=email)

    response = await client.post(
        "/v1/auth/register",
        json={
            "organization_name": "Someone Else's Company",
            "email": email,
            "password": "another-password-123",
        },
    )
    assert response.status_code == 409


async def test_login_with_correct_credentials_returns_token_pair(client: AsyncClient) -> None:
    email = _unique_email()
    await _register(client, email=email)

    response = await client.post(
        "/v1/auth/login",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["user"]["email"] == email


async def test_login_with_wrong_password_is_rejected(client: AsyncClient) -> None:
    email = _unique_email()
    await _register(client, email=email)

    response = await client.post(
        "/v1/auth/login", json={"email": email, "password": "totally-wrong-password"}
    )
    assert response.status_code == 401


async def test_login_with_unknown_email_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/login",
        json={"email": _unique_email(), "password": "whatever-password-123"},
    )
    assert response.status_code == 401


async def test_refresh_returns_new_pair_and_invalidates_old_refresh_token(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    registered = await _register(client)
    original_refresh_token = registered["refresh_token"]

    refresh_response = await client.post(
        "/v1/auth/refresh", json={"refresh_token": original_refresh_token}
    )
    assert refresh_response.status_code == 200
    refreshed = refresh_response.json()

    assert refreshed["access_token"] != registered["access_token"]
    assert refreshed["refresh_token"] != original_refresh_token

    # The old refresh token must no longer work.
    reuse_response = await client.post(
        "/v1/auth/refresh", json={"refresh_token": original_refresh_token}
    )
    assert reuse_response.status_code == 401

    # The new refresh token issued by the rotation must work.
    second_refresh_response = await client.post(
        "/v1/auth/refresh", json={"refresh_token": refreshed["refresh_token"]}
    )
    assert second_refresh_response.status_code == 200


async def test_refresh_with_garbage_token_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/v1/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert response.status_code == 401


async def test_refresh_with_expired_token_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    registered = await _register(client)
    result = await db_session.execute(
        select(User).where(User.email == registered["user"]["email"])
    )
    user = result.scalar_one()

    from app.services import auth_service

    generated = auth_service.generate_refresh_token()
    db_session.add(
        RefreshToken(
            user_id=user.id,
            hashed_token=generated.hashed_token,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await db_session.commit()

    response = await client.post("/v1/auth/refresh", json={"refresh_token": generated.raw_token})
    assert response.status_code == 401


async def test_protected_route_rejects_missing_authorization(client: AsyncClient) -> None:
    response = await client.post("/v1/api-keys")
    assert response.status_code == 401


async def test_protected_route_rejects_tampered_access_token(client: AsyncClient) -> None:
    registered = await _register(client)
    token = registered["access_token"]
    header_b64, payload_b64, signature_b64 = token.split(".")

    # Flip a character in the middle of the payload rather than the tail of
    # the signature: the trailing base64 character of the signature carries
    # unused padding bits, so flipping it occasionally decodes to the exact
    # same signature bytes and leaves the token still valid, making this
    # test flaky. A mid-payload character sits in a full 4-char base64
    # group with no unused bits, so changing it always changes the signed
    # content and therefore always invalidates the signature.
    middle = len(payload_b64) // 2
    original_char = payload_b64[middle]
    flipped_char = "x" if original_char != "x" else "y"
    tampered_payload = payload_b64[:middle] + flipped_char + payload_b64[middle + 1 :]
    tampered = f"{header_b64}.{tampered_payload}.{signature_b64}"

    response = await client.post(
        "/v1/api-keys", headers={"Authorization": f"Bearer {tampered}"}
    )
    assert response.status_code == 401


async def test_protected_route_rejects_expired_access_token(client: AsyncClient) -> None:
    registered = await _register(client)
    user_id = registered["user"]["id"]
    org_id = registered["user"]["organization_id"]

    expired = jwt.encode(
        {
            "sub": user_id,
            "org_id": org_id,
            "type": "access",
            "iat": datetime.now(UTC) - timedelta(minutes=30),
            "exp": datetime.now(UTC) - timedelta(minutes=1),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    response = await client.post(
        "/v1/api-keys", headers={"Authorization": f"Bearer {expired}"}
    )
    assert response.status_code == 401


async def test_access_token_resolves_to_the_correct_organization(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The session-authenticated /v1/api-keys route resolves the principal
    from the access token; the created key must belong to the token's own
    organization, never a different one, even when another org exists.
    """
    org_a = await _register(client)
    await _register(client)  # org_b, exists purely to prove isolation

    response = await client.post(
        "/v1/api-keys", headers={"Authorization": f"Bearer {org_a['access_token']}"}
    )
    assert response.status_code == 201

    from app.models import ApiKey

    result = await db_session.execute(
        select(ApiKey).where(ApiKey.id == uuid.UUID(response.json()["id"]))
    )
    created_key = result.scalar_one()
    assert str(created_key.organization_id) == org_a["user"]["organization_id"]


@pytest.mark.parametrize("bad_header", ["", "Token abc123", "Bearer", "Bearer "])
async def test_protected_route_rejects_malformed_authorization_header(
    client: AsyncClient, bad_header: str
) -> None:
    response = await client.post("/v1/api-keys", headers={"Authorization": bad_header})
    assert response.status_code == 401


async def test_protected_route_rejects_token_for_a_deleted_user(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A previously valid access token must stop working once the user it
    was issued for no longer exists, even though the (stateless) JWT itself
    hasn't expired yet.
    """
    registered = await _register(client)
    result = await db_session.execute(
        select(User).where(User.id == uuid.UUID(registered["user"]["id"]))
    )
    user = result.scalar_one()
    await db_session.delete(user)
    await db_session.commit()

    response = await client.post(
        "/v1/api-keys", headers={"Authorization": f"Bearer {registered['access_token']}"}
    )
    assert response.status_code == 401
