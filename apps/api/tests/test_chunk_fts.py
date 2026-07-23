"""Focused test for the chunks.content_tsv trigger and full-text search.

Confirms that inserting and updating a chunk's `content` correctly
(re)populates `content_tsv`, and that a full-text query against it returns
the expected row and excludes an unrelated one.
"""

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Document, Organization


async def _make_document(db_session: AsyncSession) -> Document:
    organization = Organization(name="Globex")
    db_session.add(organization)
    await db_session.flush()

    document = Document(
        organization_id=organization.id,
        filename="runbook.pdf",
        status="ready",
    )
    db_session.add(document)
    await db_session.flush()
    return document


async def test_insert_populates_content_tsv(db_session: AsyncSession) -> None:
    document = await _make_document(db_session)

    chunk = Chunk(
        document_id=document.id,
        chunk_index=0,
        content="Restart the ingestion worker if the queue backs up.",
    )
    db_session.add(chunk)
    await db_session.commit()

    await db_session.refresh(chunk)
    assert chunk.content_tsv is not None
    assert "worker" in chunk.content_tsv
    assert "queue" in chunk.content_tsv


async def test_update_content_repopulates_content_tsv(db_session: AsyncSession) -> None:
    document = await _make_document(db_session)

    chunk = Chunk(
        document_id=document.id,
        chunk_index=0,
        content="The original sentence about widgets.",
    )
    db_session.add(chunk)
    await db_session.commit()
    await db_session.refresh(chunk)

    original_tsv = chunk.content_tsv
    assert original_tsv is not None
    assert "widget" in original_tsv

    chunk.content = "A completely different sentence about spreadsheets."
    await db_session.commit()
    await db_session.refresh(chunk)

    assert chunk.content_tsv is not None
    assert chunk.content_tsv != original_tsv
    assert "spreadsheet" in chunk.content_tsv
    assert "widget" not in chunk.content_tsv


async def test_full_text_query_returns_expected_chunk(db_session: AsyncSession) -> None:
    document = await _make_document(db_session)

    matching = Chunk(
        document_id=document.id,
        chunk_index=0,
        content="Employees accrue fifteen days of paid time off per year.",
    )
    unrelated = Chunk(
        document_id=document.id,
        chunk_index=1,
        content="The quarterly earnings call is scheduled for next Tuesday.",
    )
    db_session.add_all([matching, unrelated])
    await db_session.commit()

    query = func.plainto_tsquery("english", "paid time off")
    result = await db_session.execute(select(Chunk).where(Chunk.content_tsv.op("@@")(query)))
    matches = result.scalars().all()

    assert [c.id for c in matches] == [matching.id]

    ranked = await db_session.execute(
        select(
            Chunk.id,
            text("ts_rank(content_tsv, plainto_tsquery('english', 'paid time off')) AS rank"),
        )
        .where(Chunk.content_tsv.op("@@")(query))
        .order_by(text("rank DESC"))
    )
    top = ranked.first()
    assert top is not None
    assert top.id == matching.id
