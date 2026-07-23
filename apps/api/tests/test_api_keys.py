"""Integration tests for API key issuance/revocation (POST/DELETE
/v1/api-keys) and the require_api_key dependency, run against a real (test)
Postgres via the ``client`` and ``db_session`` fixtures (see conftest.py).

require_api_key is not yet wired to any data route (this issue predates any
data routes existing beyond auth itself), so it is exercised directly here
as a dependency function against real, committed database state -- the same
database the HTTP-level tests write through, just without an HTTP layer in
front of it.
"""

import uuid

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_api_key
from app.models import ApiKey, Organization


async def _register_and_get_access_token(client: AsyncClient) -> tuple[str, str]:
    email = f"user-{uuid.uuid4().hex[:12]}@example.com"
    response = await client.post(
        "/v1/auth/register",
        json={
            "organization_name": "Acme Corp",
            "email": email,
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["access_token"], body["user"]["organization_id"]


async def test_create_api_key_returns_raw_key_once(client: AsyncClient) -> None:
    access_token, _ = await _register_and_get_access_token(client)

    response = await client.post(
        "/v1/api-keys", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 201
    body = response.json()

    assert body["raw_key"].startswith("nxs_live_")
    assert body["prefix"].startswith("nxs_live_")
    assert body["raw_key"].startswith(body["prefix"])
    assert "hashed_key" not in body


async def test_created_api_key_is_stored_hashed_not_raw(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    access_token, _ = await _register_and_get_access_token(client)

    response = await client.post(
        "/v1/api-keys", headers={"Authorization": f"Bearer {access_token}"}
    )
    body = response.json()

    result = await db_session.execute(select(ApiKey).where(ApiKey.id == uuid.UUID(body["id"])))
    stored = result.scalar_one()

    assert stored.hashed_key != body["raw_key"]
    # The response body has no field that leaks the persisted hash either.
    assert body.get("hashed_key") is None


async def test_create_api_key_requires_session_auth(client: AsyncClient) -> None:
    response = await client.post("/v1/api-keys")
    assert response.status_code == 401


async def test_require_api_key_resolves_the_owning_organization(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    access_token, org_id = await _register_and_get_access_token(client)
    create_response = await client.post(
        "/v1/api-keys", headers={"Authorization": f"Bearer {access_token}"}
    )
    raw_key = create_response.json()["raw_key"]

    resolved_org = await require_api_key(
        authorization=f"Bearer {raw_key}", session=db_session
    )
    assert isinstance(resolved_org, Organization)
    assert str(resolved_org.id) == org_id


async def test_require_api_key_rejects_unknown_key(db_session: AsyncSession) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await require_api_key(authorization="Bearer nxs_live_totally-made-up", session=db_session)
    assert exc_info.value.status_code == 401


async def test_require_api_key_rejects_key_without_prefix(db_session: AsyncSession) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await require_api_key(authorization="Bearer some-other-token", session=db_session)
    assert exc_info.value.status_code == 401


async def test_revoked_api_key_is_rejected_by_require_api_key(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    access_token, _ = await _register_and_get_access_token(client)
    create_response = await client.post(
        "/v1/api-keys", headers={"Authorization": f"Bearer {access_token}"}
    )
    created = create_response.json()

    # Sanity check: the key works before revocation.
    org = await require_api_key(
        authorization=f"Bearer {created['raw_key']}", session=db_session
    )
    assert org is not None

    revoke_response = await client.delete(
        f"/v1/api-keys/{created['id']}", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert revoke_response.status_code == 204

    with pytest.raises(HTTPException) as exc_info:
        await require_api_key(
            authorization=f"Bearer {created['raw_key']}", session=db_session
        )
    assert exc_info.value.status_code == 401


async def test_cannot_revoke_another_organizations_api_key(client: AsyncClient) -> None:
    access_token_a, _ = await _register_and_get_access_token(client)
    access_token_b, _ = await _register_and_get_access_token(client)

    create_response = await client.post(
        "/v1/api-keys", headers={"Authorization": f"Bearer {access_token_a}"}
    )
    key_id = create_response.json()["id"]

    # Org B attempting to revoke org A's key must not succeed.
    revoke_response = await client.delete(
        f"/v1/api-keys/{key_id}", headers={"Authorization": f"Bearer {access_token_b}"}
    )
    assert revoke_response.status_code == 404

    # And org A can still issue its own keys afterward (nothing about org A
    # was disturbed by org B's rejected attempt).
    result_check = await client.post(
        "/v1/api-keys", headers={"Authorization": f"Bearer {access_token_a}"}
    )
    assert result_check.status_code == 201
