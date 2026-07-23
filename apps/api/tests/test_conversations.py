"""Integration tests for conversation/message routes (see docs/TDD.md
section 3.4 and issue #16), run against a real (test) Postgres, a real (test)
Qdrant collection, and real embeddings for retrieval, with only
``generation_service.stream_answer`` mocked (no live GEMINI_API_KEY in CI) --
the same pattern ``tests/test_rag_graph.py``'s integration-style test uses.

Covers: conversation creation, sending a message end to end (persists both
messages, persists citations, streams a response), the follow-up-context
test proving prior-turn content actually reaches the second call's history,
windowing (only the most recent N prior messages are loaded), org/ownership
scoping, and list/detail endpoint shapes.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from qdrant_client.http import models as qmodels
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Chunk, Citation, Conversation, Document, Message, User
from app.services import auth_service, generation_service, vector_store_service
from app.services.embedding_service import embed_documents
from app.services.generation_service import ConversationTurn, GenerationPrompt
from app.services.reranking_service import RerankCandidate
from app.services.vector_store_service import ensure_collection, get_client


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


async def _create_conversation(client: AsyncClient, access_token: str) -> str:
    response = await client.post(
        "/v1/conversations",
        headers={"Authorization": f"Bearer {access_token}"},
        json={},
    )
    assert response.status_code == 201, response.text
    conversation_id: str = response.json()["id"]
    return conversation_id


async def _ingest_pto_chunk(
    db_session: AsyncSession, organization_id: uuid.UUID, content: str
) -> uuid.UUID:
    """Ingest one real, indexed chunk (real embedding, real Qdrant upsert,
    real Postgres row) so hybrid_search has something real to retrieve for
    the PTO-themed questions these tests ask. Returns the chunk's id, which
    the caller is responsible for deleting from Qdrant afterward (see each
    test's ``finally`` block).
    """
    document = Document(organization_id=organization_id, filename="handbook.pdf", status="ready")
    db_session.add(document)
    await db_session.flush()

    chunk = Chunk(document_id=document.id, chunk_index=0, content=content)
    db_session.add(chunk)
    await db_session.flush()

    [embedding] = embed_documents([content])
    ensure_collection()
    vector_store_service.upsert_chunk(
        chunk_id=chunk.id,
        document_id=document.id,
        organization_id=organization_id,
        embedding=embedding,
        content=content,
    )
    await db_session.commit()
    return chunk.id


def _delete_qdrant_point(chunk_id: uuid.UUID) -> None:
    get_client().delete(
        collection_name=vector_store_service.COLLECTION_NAME,
        points_selector=qmodels.PointIdsList(points=[str(chunk_id)]),
    )


def _parse_sse_events(text: str) -> list[tuple[str, dict[str, object]]]:
    """Parse a raw SSE response body into a list of (event, data) tuples, in
    the order they were sent.
    """
    events: list[tuple[str, dict[str, object]]] = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        event_name = ""
        data: dict[str, object] = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        events.append((event_name, data))
    return events


def _expected_streamed_text(text: str) -> str:
    """What the accumulated answer looks like after `_fake_stream_answer`
    streams `text` word by word with a trailing space rejoined after each
    word -- matches _fake_stream_answer_factory's chunking exactly, so tests
    can assert on the persisted/streamed text without duplicating that logic
    by hand.
    """
    return "".join(piece + " " for piece in text.split(" "))


def _fake_stream_answer_factory(
    text: str,
) -> Callable[[GenerationPrompt], AsyncIterator[str]]:
    """Build a stand-in for generation_service.stream_answer that yields
    `text` split into word-sized deltas, proving the endpoint really forwards
    incremental chunks (not just one final blob) without needing a live
    Gemini call.
    """

    async def _fake_stream_answer(prompt: GenerationPrompt) -> AsyncIterator[str]:
        for piece in text.split(" "):
            yield piece + " "

    return _fake_stream_answer


# --- Conversation creation ----------------------------------------------------


async def test_create_conversation_persists_row_scoped_to_user_and_org(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    access_token, organization_id = await _register_and_get_access_token(client)

    response = await client.post(
        "/v1/conversations",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"title": "Vacation questions"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["organization_id"] == organization_id
    assert body["title"] == "Vacation questions"
    assert uuid.UUID(body["id"])

    result = await db_session.execute(
        select(Conversation).where(Conversation.id == uuid.UUID(body["id"]))
    )
    stored = result.scalar_one()
    assert stored.title == "Vacation questions"
    assert str(stored.organization_id) == organization_id


async def test_create_conversation_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/v1/conversations", json={})
    assert response.status_code == 401


async def test_create_conversation_title_optional(client: AsyncClient) -> None:
    access_token, _ = await _register_and_get_access_token(client)
    response = await client.post(
        "/v1/conversations",
        headers={"Authorization": f"Bearer {access_token}"},
        json={},
    )
    assert response.status_code == 201, response.text
    assert response.json()["title"] is None


# --- Sending a message end to end --------------------------------------------


async def test_send_message_persists_messages_citations_and_streams_answer(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access_token, organization_id = await _register_and_get_access_token(client)
    conversation_id = await _create_conversation(client, access_token)

    content = "Our standard PTO policy is 15 days per year for all full-time employees."
    chunk_id = await _ingest_pto_chunk(db_session, uuid.UUID(organization_id), content)
    try:
        answer_text = "Standard PTO is 15 days per year [1]."
        monkeypatch.setattr(
            generation_service, "stream_answer", _fake_stream_answer_factory(answer_text)
        )

        response = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"content": "What is our standard PTO policy?"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")

        events = _parse_sse_events(response.text)
        assert events[-1][0] == "done"
        delta_events = [event for event in events if event[0] == "delta"]
        # More than one delta event proves the endpoint forwards incremental
        # chunks, not a single end-of-generation payload (the actual point
        # of choosing real streaming, see app/api/conversations.py's module
        # docstring).
        assert len(delta_events) > 1
        streamed_text = "".join(str(event[1]["text"]) for event in delta_events)
        assert streamed_text == _expected_streamed_text(answer_text)

        done_data = events[-1][1]
        assert uuid.UUID(str(done_data["message_id"]))
        citations = done_data["citations"]
        assert isinstance(citations, list)
        assert len(citations) == 1
        assert citations[0]["chunk_id"] == str(chunk_id)

        # Both messages persisted, in the correct roles.
        result = await db_session.execute(
            select(Message)
            .where(Message.conversation_id == uuid.UUID(conversation_id))
            .order_by(Message.created_at.asc())
        )
        messages = list(result.scalars().all())
        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[0].content == "What is our standard PTO policy?"
        assert messages[1].role == "assistant"
        assert messages[1].content == _expected_streamed_text(answer_text)

        # Citation persisted, linked to the real chunk.
        citation_result = await db_session.execute(
            select(Citation).where(Citation.message_id == messages[1].id)
        )
        stored_citations = list(citation_result.scalars().all())
        assert len(stored_citations) == 1
        assert stored_citations[0].chunk_id == chunk_id
        assert stored_citations[0].relevance_score is not None
    finally:
        _delete_qdrant_point(chunk_id)


# --- Follow-up context retention ---------------------------------------------


async def test_followup_question_reuses_prior_turn_context(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core acceptance-criteria test: a first message establishes
    context, a second, ellipsis-dependent follow-up only makes sense with
    that context, and the assertion proves the second call's constructed
    history actually contains the first turn's content -- by spying on
    generation_service.build_prompt (the exact boundary this issue's
    acceptance criteria names) and asserting on the ``history`` it was
    invoked with, not just that rows exist in the database.
    """
    access_token, organization_id = await _register_and_get_access_token(client)
    conversation_id = await _create_conversation(client, access_token)

    content = "Our standard PTO policy is 15 days per year for all full-time employees."
    chunk_id = await _ingest_pto_chunk(db_session, uuid.UUID(organization_id), content)

    captured_builds: list[dict[str, object]] = []
    real_build_prompt = generation_service.build_prompt

    def _spy_build_prompt(
        query: str, history: list[ConversationTurn], candidates: list[RerankCandidate]
    ) -> GenerationPrompt:
        captured_builds.append({"query": query, "history": list(history)})
        return real_build_prompt(query, history, candidates)

    monkeypatch.setattr(generation_service, "build_prompt", _spy_build_prompt)

    try:
        first_answer = "Standard PTO is 15 days per year [1]."
        monkeypatch.setattr(
            generation_service, "stream_answer", _fake_stream_answer_factory(first_answer)
        )
        first_response = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"content": "What is our standard PTO policy?"},
        )
        assert first_response.status_code == 200, first_response.text

        second_answer = "New hires get a prorated amount based on their start date [1]."
        monkeypatch.setattr(
            generation_service, "stream_answer", _fake_stream_answer_factory(second_answer)
        )
        second_response = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"content": "What about for new hires?"},
        )
        assert second_response.status_code == 200, second_response.text

        # Proof #1: the second call's constructed history actually contains
        # the first turn's question and answer, unchanged.
        assert len(captured_builds) == 2
        assert captured_builds[0]["history"] == []
        assert captured_builds[0]["query"] == "What is our standard PTO policy?"

        second_history = captured_builds[1]["history"]
        assert second_history == [
            ConversationTurn(role="user", content="What is our standard PTO policy?"),
            ConversationTurn(role="assistant", content=_expected_streamed_text(first_answer)),
        ]
        assert captured_builds[1]["query"] == "What about for new hires?"

        # Proof #2: the persisted Message rows form the correct chronological
        # chain a real history-load would reconstruct correctly.
        result = await db_session.execute(
            select(Message)
            .where(Message.conversation_id == uuid.UUID(conversation_id))
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
        messages = list(result.scalars().all())
        assert [(m.role, m.content) for m in messages] == [
            ("user", "What is our standard PTO policy?"),
            ("assistant", _expected_streamed_text(first_answer)),
            ("user", "What about for new hires?"),
            ("assistant", _expected_streamed_text(second_answer)),
        ]
    finally:
        _delete_qdrant_point(chunk_id)


# --- Windowing -----------------------------------------------------------------


async def test_message_history_is_windowed_to_configured_limit(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conversation with more prior messages than the configured window
    must only load the most recent N -- proven by asserting on the exact
    history list generation_service.build_prompt was invoked with, the same
    spy technique as the follow-up-context test.
    """
    monkeypatch.setattr(settings, "conversation_history_window", 4)

    access_token, _ = await _register_and_get_access_token(client)
    conversation_id = await _create_conversation(client, access_token)

    # Seed 6 prior messages (more than window=4), with explicit, strictly
    # increasing created_at timestamps -- see app/api/conversations.py's
    # module docstring for why relying on the database's now()-based
    # server_default would not distinguish these within this fixture's
    # single shared transaction (verified empirically: now() is frozen at
    # transaction start, unaffected by savepoints).
    base_time = datetime.now(UTC)
    seeded: list[tuple[str, str]] = []
    for i in range(6):
        role = "user" if i % 2 == 0 else "assistant"
        message_content = f"seed message {i}"
        seeded.append((role, message_content))
        db_session.add(
            Message(
                conversation_id=uuid.UUID(conversation_id),
                role=role,
                content=message_content,
                created_at=base_time + timedelta(seconds=i),
            )
        )
    await db_session.commit()

    captured_builds: list[list[ConversationTurn]] = []
    real_build_prompt = generation_service.build_prompt

    def _spy_build_prompt(
        query: str, history: list[ConversationTurn], candidates: list[RerankCandidate]
    ) -> GenerationPrompt:
        captured_builds.append(list(history))
        return real_build_prompt(query, history, candidates)

    monkeypatch.setattr(generation_service, "build_prompt", _spy_build_prompt)
    monkeypatch.setattr(generation_service, "stream_answer", _fake_stream_answer_factory("ok."))

    response = await client.post(
        f"/v1/conversations/{conversation_id}/messages",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"content": "new question"},
    )
    assert response.status_code == 200, response.text

    assert len(captured_builds) == 1
    loaded_history = captured_builds[0]
    assert len(loaded_history) == 4
    expected = [ConversationTurn(role=role, content=text) for role, text in seeded[-4:]]
    assert loaded_history == expected


async def test_message_history_window_of_zero_loads_no_history(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured window of 0 (or negative, defensively) loads no history
    at all rather than erroring -- the edge case _load_recent_history's
    early return exists for.
    """
    monkeypatch.setattr(settings, "conversation_history_window", 0)

    access_token, _ = await _register_and_get_access_token(client)
    conversation_id = await _create_conversation(client, access_token)
    db_session.add(
        Message(conversation_id=uuid.UUID(conversation_id), role="user", content="earlier turn")
    )
    await db_session.commit()

    captured_builds: list[list[ConversationTurn]] = []
    real_build_prompt = generation_service.build_prompt

    def _spy_build_prompt(
        query: str, history: list[ConversationTurn], candidates: list[RerankCandidate]
    ) -> GenerationPrompt:
        captured_builds.append(list(history))
        return real_build_prompt(query, history, candidates)

    monkeypatch.setattr(generation_service, "build_prompt", _spy_build_prompt)
    monkeypatch.setattr(generation_service, "stream_answer", _fake_stream_answer_factory("ok."))

    response = await client.post(
        f"/v1/conversations/{conversation_id}/messages",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"content": "new question"},
    )
    assert response.status_code == 200, response.text
    assert captured_builds == [[]]


# --- Org/ownership scoping -----------------------------------------------------


async def test_send_message_scoped_to_organization(client: AsyncClient) -> None:
    token_a, _ = await _register_and_get_access_token(client, org_name="Org A")
    token_b, _ = await _register_and_get_access_token(client, org_name="Org B")
    conversation_id = await _create_conversation(client, token_a)

    response = await client.post(
        f"/v1/conversations/{conversation_id}/messages",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"content": "trying to read someone else's conversation"},
    )
    assert response.status_code == 404


async def test_get_conversation_scoped_to_organization(client: AsyncClient) -> None:
    token_a, _ = await _register_and_get_access_token(client, org_name="Org A")
    token_b, _ = await _register_and_get_access_token(client, org_name="Org B")
    conversation_id = await _create_conversation(client, token_a)

    own_response = await client.get(
        f"/v1/conversations/{conversation_id}", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert own_response.status_code == 200

    other_org_response = await client.get(
        f"/v1/conversations/{conversation_id}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert other_org_response.status_code == 404


async def test_get_conversation_scoped_to_owning_user_within_same_organization(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Stricter than plain org-scoping: a second user registered into the
    *same* organization as the conversation's owner still cannot read it.
    Requires creating a second user directly (register always creates a
    brand-new organization, so a same-org second user is added directly via
    the ORM here rather than through the API).
    """
    token_a, organization_id = await _register_and_get_access_token(client, org_name="Shared Org")
    conversation_id = await _create_conversation(client, token_a)

    second_user = User(
        organization_id=uuid.UUID(organization_id),
        email=f"second-{uuid.uuid4().hex[:12]}@example.com",
        hashed_password=auth_service.hash_password("another password"),
        role="member",
    )
    db_session.add(second_user)
    await db_session.commit()
    second_token = auth_service.create_access_token(
        user_id=second_user.id, organization_id=second_user.organization_id
    )

    response = await client.get(
        f"/v1/conversations/{conversation_id}",
        headers={"Authorization": f"Bearer {second_token}"},
    )
    assert response.status_code == 404


async def test_get_conversation_not_found(client: AsyncClient) -> None:
    access_token, _ = await _register_and_get_access_token(client)
    response = await client.get(
        f"/v1/conversations/{uuid.uuid4()}", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 404


# --- List/detail shapes --------------------------------------------------------


async def test_list_conversations_scoped_to_user(client: AsyncClient) -> None:
    token_a, _ = await _register_and_get_access_token(client, org_name="Org A")
    token_b, _ = await _register_and_get_access_token(client, org_name="Org B")

    await _create_conversation(client, token_a)
    await _create_conversation(client, token_b)

    response_a = await client.get(
        "/v1/conversations", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert response_a.status_code == 200
    assert len(response_a.json()["conversations"]) == 1

    response_b = await client.get(
        "/v1/conversations", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert response_b.status_code == 200
    assert len(response_b.json()["conversations"]) == 1


async def test_get_conversation_includes_message_history_and_citations(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    access_token, _ = await _register_and_get_access_token(client)
    conversation_id = await _create_conversation(client, access_token)

    monkeypatch.setattr(
        generation_service, "stream_answer", _fake_stream_answer_factory("a plain answer.")
    )
    send_response = await client.post(
        f"/v1/conversations/{conversation_id}/messages",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"content": "hello"},
    )
    assert send_response.status_code == 200, send_response.text

    detail_response = await client.get(
        f"/v1/conversations/{conversation_id}", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert detail_response.status_code == 200
    body = detail_response.json()
    assert body["id"] == conversation_id
    assert len(body["messages"]) == 2
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "hello"
    assert body["messages"][0]["citations"] == []
    assert body["messages"][1]["role"] == "assistant"
    assert body["messages"][1]["citations"] == []


async def test_send_message_requires_auth(client: AsyncClient) -> None:
    access_token, _ = await _register_and_get_access_token(client)
    conversation_id = await _create_conversation(client, access_token)

    response = await client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"content": "hello"},
    )
    assert response.status_code == 401
