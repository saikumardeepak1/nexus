"""Integration test: insert one row per core table, following the full FK
chain from Organization down to Citation, and confirm every row round-trips
with the values it was written with.

Runs against a real (migrated) Postgres database, see conftest.py.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApiKey,
    Chunk,
    Citation,
    Conversation,
    Document,
    Message,
    Organization,
    User,
)


async def test_full_fk_chain_round_trips(db_session: AsyncSession) -> None:
    organization = Organization(name="Acme Corp")
    db_session.add(organization)
    await db_session.flush()

    user = User(
        organization_id=organization.id,
        email="ada@acme.example",
        hashed_password="not-a-real-hash",
        role="admin",
    )
    api_key = ApiKey(
        organization_id=organization.id,
        prefix="nxs_live_ab12",
        hashed_key="hashed-secret-value",
    )
    document = Document(
        organization_id=organization.id,
        filename="handbook.pdf",
        status="ready",
        page_count=12,
    )
    db_session.add_all([user, api_key, document])
    await db_session.flush()

    chunk = Chunk(
        document_id=document.id,
        chunk_index=0,
        content="Employees accrue fifteen days of paid time off per year.",
        qdrant_point_id="point-0001",
        page_number=3,
    )
    db_session.add(chunk)
    await db_session.flush()

    conversation = Conversation(
        organization_id=organization.id,
        user_id=user.id,
        title="PTO policy question",
    )
    db_session.add(conversation)
    await db_session.flush()

    message = Message(
        conversation_id=conversation.id,
        role="assistant",
        content="Employees accrue fifteen PTO days per year.",
    )
    db_session.add(message)
    await db_session.flush()

    citation = Citation(
        message_id=message.id,
        chunk_id=chunk.id,
        relevance_score=0.87,
    )
    db_session.add(citation)
    await db_session.commit()

    # Re-fetch everything by primary key through fresh selects to confirm
    # the values actually persisted (not just held in the session identity map).
    fetched_org = (
        await db_session.execute(select(Organization).where(Organization.id == organization.id))
    ).scalar_one()
    assert fetched_org.name == "Acme Corp"
    assert fetched_org.created_at is not None

    fetched_user = (await db_session.execute(select(User).where(User.id == user.id))).scalar_one()
    assert fetched_user.organization_id == organization.id
    assert fetched_user.email == "ada@acme.example"

    fetched_key = (
        await db_session.execute(select(ApiKey).where(ApiKey.id == api_key.id))
    ).scalar_one()
    assert fetched_key.organization_id == organization.id
    assert fetched_key.revoked_at is None

    fetched_doc = (
        await db_session.execute(select(Document).where(Document.id == document.id))
    ).scalar_one()
    assert fetched_doc.organization_id == organization.id
    assert fetched_doc.status == "ready"

    fetched_chunk = (
        await db_session.execute(select(Chunk).where(Chunk.id == chunk.id))
    ).scalar_one()
    assert fetched_chunk.document_id == document.id
    assert fetched_chunk.page_number == 3

    fetched_conversation = (
        await db_session.execute(select(Conversation).where(Conversation.id == conversation.id))
    ).scalar_one()
    assert fetched_conversation.organization_id == organization.id
    assert fetched_conversation.user_id == user.id

    fetched_message = (
        await db_session.execute(select(Message).where(Message.id == message.id))
    ).scalar_one()
    assert fetched_message.conversation_id == conversation.id
    assert fetched_message.role == "assistant"

    fetched_citation = (
        await db_session.execute(select(Citation).where(Citation.id == citation.id))
    ).scalar_one()
    assert fetched_citation.message_id == message.id
    assert fetched_citation.chunk_id == chunk.id
    assert fetched_citation.relevance_score == 0.87
