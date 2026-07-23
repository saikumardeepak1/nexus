"""Integration tests for chunk lookup (GET /v1/chunks/{id}, see issue #17),
run against a real (test) Postgres via the ``client``/``db_session``
fixtures (see conftest.py). Only a Postgres row is needed here (no Qdrant
upsert, no embeddings): this endpoint reads ``chunks``/``documents``
directly and never touches the vector store.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Document


async def _register_and_get_access_token(
    client: AsyncClient, org_name: str = "Acme Corp"
) -> tuple[str, str]:
    email = f"user-{uuid.uuid4().hex[:12]}@example.com"
    response = await client.post(
        "/v1/auth/register",
        json={
            "organization_name": org_name,
            "email": email,
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["access_token"], body["user"]["organization_id"]


async def _create_api_key(client: AsyncClient, access_token: str) -> str:
    response = await client.post(
        "/v1/api-keys", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 201, response.text
    raw_key: str = response.json()["raw_key"]
    return raw_key


async def _seed_chunk(
    db_session: AsyncSession,
    organization_id: uuid.UUID,
    filename: str = "handbook.pdf",
    content: str = "Standard PTO is 15 days per year.",
    page_number: int | None = 3,
) -> uuid.UUID:
    document = Document(organization_id=organization_id, filename=filename, status="ready")
    db_session.add(document)
    await db_session.flush()

    chunk = Chunk(
        document_id=document.id, chunk_index=0, content=content, page_number=page_number
    )
    db_session.add(chunk)
    await db_session.commit()
    return chunk.id


async def test_get_chunk_resolves_document_filename_and_content(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    access_token, organization_id = await _register_and_get_access_token(client)
    chunk_id = await _seed_chunk(
        db_session,
        uuid.UUID(organization_id),
        filename="handbook.pdf",
        content="Standard PTO is 15 days per year.",
        page_number=3,
    )

    response = await client.get(
        f"/v1/chunks/{chunk_id}", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(chunk_id)
    assert body["filename"] == "handbook.pdf"
    assert body["content"] == "Standard PTO is 15 days per year."
    assert body["page_number"] == 3


async def test_get_chunk_works_with_api_key_auth(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    access_token, organization_id = await _register_and_get_access_token(client)
    raw_key = await _create_api_key(client, access_token)
    chunk_id = await _seed_chunk(db_session, uuid.UUID(organization_id))

    response = await client.get(
        f"/v1/chunks/{chunk_id}", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert response.status_code == 200, response.text


async def test_get_chunk_scoped_to_organization(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token_a, org_a = await _register_and_get_access_token(client, org_name="Org A")
    token_b, _ = await _register_and_get_access_token(client, org_name="Org B")
    chunk_id = await _seed_chunk(db_session, uuid.UUID(org_a))

    own_response = await client.get(
        f"/v1/chunks/{chunk_id}", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert own_response.status_code == 200

    other_org_response = await client.get(
        f"/v1/chunks/{chunk_id}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert other_org_response.status_code == 404


async def test_get_chunk_not_found(client: AsyncClient) -> None:
    access_token, _ = await _register_and_get_access_token(client)
    response = await client.get(
        f"/v1/chunks/{uuid.uuid4()}", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 404


async def test_get_chunk_requires_auth(client: AsyncClient, db_session: AsyncSession) -> None:
    access_token, organization_id = await _register_and_get_access_token(client)
    chunk_id = await _seed_chunk(db_session, uuid.UUID(organization_id))

    response = await client.get(f"/v1/chunks/{chunk_id}")
    assert response.status_code == 401
