"""Conversation and message routes (see docs/TDD.md section 3.4 and issue #16).

Session-authenticated only (``require_session``, not ``require_api_key`` /
``require_organization``): a conversation belongs to one specific user, not
just an organization (``Conversation.user_id``), and the dashboard chat UI
(issue #17) is the only client this issue anticipates, matching TDD.md
section 3.6's description of ``require_session`` as "used on dashboard
routes." Every route below is scoped to both the caller's organization_id
*and* user_id: a user cannot read or post to a conversation that belongs to
another organization (required by this issue's acceptance criteria) or to a
different user within their own organization (a stricter, additional
invariant, since a conversation is a private chat thread, not shared org-wide
data like a document).

Streaming decision
--------------------
``docs/TDD.md`` section 3.4 says this endpoint "streams the generated answer
(SSE) with citations." Two ways to satisfy that were genuinely open (see
issue #16 and the "Streaming" note in ``app/graph/rag_graph.py``'s module
docstring, which deliberately left this to this issue):

(a) Call ``run_rag_graph`` as-is (it uses the non-streaming
    ``generate_answer`` wrapper internally) and have this endpoint fake SSE
    by sending the complete answer as a single event once the graph
    finishes.
(b) Call ``hybrid_search_service`` and ``reranking_service`` directly from
    this route (the same two steps ``run_rag_graph``'s ``retrieve``/``rerank``
    nodes already encapsulate) and call ``generation_service.stream_answer``
    directly, forwarding each real token delta to the client as it arrives.

This module implements **(b)**. ``generation_service.stream_answer`` exists
specifically for this: its own module docstring already anticipates this
endpoint as the caller that needs "real token-by-token delivery rather than
a single end-of-generation payload," and the chat UI (issue #17, built next)
gets a materially better experience from real incremental tokens than from a
single large SSE event with an identical shape. The cost is a small amount
of duplicated sequencing: this module re-implements the
retrieve-then-rerank-then-build-prompt chain ``run_rag_graph`` already wires
together, rather than reusing it, since ``run_rag_graph`` has no way to hand
back control mid-generation for this module to forward partial output. A
client should expect two SSE event types from this endpoint: zero or more
``event: delta`` events (``data: {"text": "..."}``, one per generated text
chunk, in order) followed by exactly one terminal ``event: done``
(``data: {"message_id": "...", "citations": [...]}``) once the full answer
has been generated and persisted. ``run_rag_graph`` is not used by this
module at all -- it remains exercised directly by its own tests
(``tests/test_rag_graph.py``) and is available for any future non-streaming
caller, but this endpoint does not currently call it.

Conversation history and message ordering
-------------------------------------------
``Message.created_at`` has a ``server_default=func.now()``, but this module
sets it explicitly (``datetime.now(UTC)``) on every ``Message`` it
constructs instead of relying on that default. Postgres's ``now()`` returns
the *start time of the current transaction*, frozen across every statement
and savepoint within it, not the wall-clock time of each individual insert;
a client sending two messages back to back is (in production) two separate
requests, each its own top-level transaction, so this would usually be fine
in production regardless. But it would silently break ordering the moment
more than one message is written inside a single transaction (e.g. exactly
the kind of transactional test fixture this repo's integration tests use,
see tests/conftest.py), where every message written during a test would
otherwise get an identical ``created_at``. Setting it explicitly in Python
sidesteps that entirely: it reflects real wall-clock time regardless of the
surrounding transaction's boundaries, which is what history-window ordering
(``_load_recent_history`` below) actually needs.
"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.db import get_session
from app.core.security import require_session
from app.models import Citation, Conversation, Message, User
from app.schemas.conversation import (
    ConversationCreateRequest,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationResponse,
    MessageCreateRequest,
    MessageResponse,
)
from app.services import generation_service, hybrid_search_service, reranking_service
from app.services.generation_service import ConversationTurn
from app.services.reranking_service import RerankCandidate

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


async def _get_owned_conversation(
    conversation_id: uuid.UUID, user: User, session: AsyncSession
) -> Conversation:
    """Fetch ``conversation_id``, scoped to ``user``'s organization *and*
    the user themselves. 404s (rather than 403s) for a conversation that
    exists but belongs to someone else, matching ``documents.py``'s
    not-found-vs-not-yours pattern so a caller cannot distinguish "does not
    exist" from "not yours" and probe for other users'/orgs' conversation ids.
    """
    conversation = await session.get(Conversation, conversation_id)
    if (
        conversation is None
        or conversation.organization_id != user.organization_id
        or conversation.user_id != user.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    return conversation


async def _load_recent_history(
    session: AsyncSession, conversation_id: uuid.UUID, window: int
) -> list[ConversationTurn]:
    """Load the most recent ``window`` messages of ``conversation_id``,
    oldest first, as ``ConversationTurn``s ready for
    ``generation_service.build_prompt``.

    Called *before* the new user message is persisted, so the returned
    history never needs to (and never accidentally could) include the very
    message this call is about to answer -- the current question is passed
    to the RAG pipeline separately as ``query``, not as part of history.

    Ordered by ``created_at`` descending (see the "Conversation history and
    message ordering" note in the module docstring for why that column is
    set explicitly rather than left to the database) with ``id`` as a tie
    breaker for the vanishingly unlikely case of an exact timestamp
    collision, limited to ``window``, then reversed back into chronological
    (oldest-first) order, since that is what
    ``generation_service.build_prompt`` expects.
    """
    if window <= 0:
        return []
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(window)
    )
    recent = list(result.scalars().all())
    recent.reverse()
    return [ConversationTurn(role=message.role, content=message.content) for message in recent]


def _sse_event(event: str, data: dict[str, object]) -> bytes:
    """Format one Server-Sent Event: an ``event:`` line naming the event
    type, a ``data:`` line carrying its JSON payload, and the blank line
    SSE requires to terminate the event.
    """
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreateRequest,
    user: User = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> Conversation:
    """Start a new, empty conversation owned by the calling user."""
    conversation = Conversation(
        organization_id=user.organization_id, user_id=user.id, title=body.title
    )
    session.add(conversation)
    await session.commit()
    return conversation


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    user: User = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> ConversationListResponse:
    """List every conversation owned by the calling user, newest first."""
    result = await session.execute(
        select(Conversation)
        .where(Conversation.organization_id == user.organization_id)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.created_at.desc())
    )
    conversations = list(result.scalars().all())
    return ConversationListResponse(
        conversations=[ConversationResponse.model_validate(c) for c in conversations]
    )


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> ConversationDetailResponse:
    """Fetch one conversation plus its full message history (oldest first),
    each assistant message's citations inlined so a client can render
    sources without a second round trip.
    """
    conversation = await _get_owned_conversation(conversation_id, user, session)

    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .options(selectinload(Message.citations))
        .order_by(Message.created_at.asc(), Message.id.asc())
    )
    messages = list(result.scalars().unique().all())

    return ConversationDetailResponse(
        id=conversation.id,
        organization_id=conversation.organization_id,
        user_id=conversation.user_id,
        title=conversation.title,
        created_at=conversation.created_at,
        messages=[MessageResponse.model_validate(message) for message in messages],
    )


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    body: MessageCreateRequest,
    user: User = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Send a new message: persist it, run the RAG pipeline over it with the
    conversation's windowed prior history as context, and stream the
    generated answer back over SSE (see the "Streaming decision" note in the
    module docstring), persisting the assistant's answer and its citations
    once generation completes.
    """
    conversation = await _get_owned_conversation(conversation_id, user, session)

    # Load history before persisting the new message (see _load_recent_history's
    # docstring): the window must never include the message it is about to answer.
    history = await _load_recent_history(
        session, conversation.id, settings.conversation_history_window
    )

    user_message = Message(
        conversation_id=conversation.id,
        role="user",
        content=body.content,
        created_at=datetime.now(UTC),
    )
    session.add(user_message)
    # Committed before the RAG pipeline runs so the user's message is
    # durable even if retrieval/generation fails partway through below.
    await session.commit()

    retrieved = await hybrid_search_service.hybrid_search(
        session, user.organization_id, body.content
    )
    candidates = [
        RerankCandidate(chunk_id=str(result.chunk_id), content=result.content)
        for result in retrieved
    ]
    reranked = reranking_service.rerank(body.content, candidates)
    prompt = generation_service.build_prompt(body.content, history, reranked)

    async def event_stream() -> AsyncIterator[bytes]:
        chunks: list[str] = []
        async for delta in generation_service.stream_answer(prompt):
            chunks.append(delta)
            yield _sse_event("delta", {"text": delta})

        answer = "".join(chunks)
        citations = generation_service.parse_citations(answer, reranked)

        assistant_message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=answer,
            created_at=datetime.now(UTC),
        )
        session.add(assistant_message)
        await session.flush()

        for citation in citations:
            session.add(
                Citation(
                    message_id=assistant_message.id,
                    chunk_id=uuid.UUID(citation.chunk_id),
                    relevance_score=(
                        citation.relevance_score if citation.relevance_score is not None else 0.0
                    ),
                )
            )
        await session.commit()

        yield _sse_event(
            "done",
            {
                "message_id": str(assistant_message.id),
                "citations": [
                    {
                        "chunk_id": citation.chunk_id,
                        "relevance_score": citation.relevance_score,
                        "marker": citation.marker,
                        "text_position": citation.text_position,
                    }
                    for citation in citations
                ],
            },
        )

    return StreamingResponse(event_stream(), media_type="text/event-stream")
