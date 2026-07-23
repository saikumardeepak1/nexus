"""LangGraph RAG orchestration graph (see docs/TDD.md section 3.3 and issue
#14).

This module wires `hybrid_search_service`, `reranking_service`, and
`generation_service` together into a single `StateGraph`: `retrieve` ->
`rerank` -> `generate` -> `END`. Each node calls exactly one of those three
modules and writes its output into shared graph state for the next node to
read, so the graph itself contains no retrieval, reranking, or generation
logic of its own, only the wiring between the three.

Conversation history
----------------------
The `messages` table and conversation persistence layer do not exist yet
(issue #16, not built yet). This module's contract is deliberately narrow: it
accepts already-loaded conversation history as a plain
`list[generation_service.ConversationTurn]` input to `run_rag_graph`, the
same shape `generation_service.build_prompt` already expects. Issue #16 will
be responsible for loading a conversation's prior turns from Postgres
(windowed to the most recent N turns, per docs/TDD.md section 3.3) and
calling `run_rag_graph` with that list; this graph never touches the
database to build history itself, and never uses LangGraph's own
checkpointer for conversation memory, matching the TDD's explicit decision
to keep conversation history queryable/persisted the same way the rest of
the app's data is rather than inside a graph-specific store.

Database session injection
----------------------------
`retrieve` needs an active `AsyncSession` to call `hybrid_search_service.
hybrid_search`. Sessions are request-scoped and owned by the caller (a
FastAPI route's `get_session` dependency, or a test's transactional fixture),
not something this module should construct itself. LangGraph's supported
mechanism for handing a node run-scoped external resources it did not create
is `context_schema`: a node function that declares a `runtime` parameter
receives a `langgraph.runtime.Runtime[GraphContext]` whose `.context` is
whatever was passed to `compiled_graph.ainvoke(..., context=...)` for that
specific call. This is used here (`GraphContext` carries the session) rather
than a closure/`functools.partial` capturing the session at graph-build
time, because the compiled graph is built once as a module-level singleton
(the same lazy-singleton pattern `reranking_service._get_model` and
`generation_service._get_client` use for their own expensive-to-build
objects) and reused across every call; a closure would instead tie a single
compiled graph instance to one specific session for its entire lifetime,
which is wrong once more than one request can be in flight. `context_schema`
keeps the graph reusable across concurrent calls, each with its own session,
while still keeping the session out of `RagState` (state is the data the
graph reasons over and could in principle be checkpointed/serialized;
a live database connection is neither).

Streaming
----------
`generate` calls `generation_service.generate_answer` (the non-streaming,
build-prompt-then-await-the-full-answer convenience wrapper), not
`stream_answer` directly. This graph's job is proving the three nodes
compose correctly end to end, which this issue's acceptance criteria checks
by asserting on a final answer/citations result, not token-by-token
delivery. `generation_service`'s own docstring already anticipates the SSE
message endpoint (issue #16) and the chat UI (issue #17) as the layers that
need real incremental streaming to a client; wiring `stream_answer` through
a LangGraph node would require the graph to yield partial state updates
mid-node (LangGraph supports this via custom stream modes, but it is
meaningfully more machinery for a graph that has no HTTP route to stream to
yet). Using `generate_answer` here keeps this issue's scope to the
orchestration wiring itself, with true end-to-end SSE streaming a deliberate
follow-up once issue #16 gives this graph an actual client connection to
stream toward.
"""

import uuid
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import generation_service, hybrid_search_service, reranking_service
from app.services.generation_service import ConversationTurn, ResolvedCitation
from app.services.hybrid_search_service import HybridSearchResult
from app.services.reranking_service import RerankCandidate

# Deliberately no `from __future__ import annotations` in this module (unlike
# most other modules in app/services/): with postponed evaluation active,
# `TypedDict`'s own metaclass reads `NotRequired[...]` annotations as plain
# strings rather than resolving them, and silently treats every field below
# as required instead of only `query`/`organization_id`/`history`. Keeping
# annotations eagerly evaluated here is what makes `NotRequired` actually
# take effect on `RagState.__required_keys__`/`__optional_keys__` at runtime.


class RagState(TypedDict):
    """Shared state threaded through the graph's nodes.

    `query`, `organization_id`, and `history` are set once, from
    `run_rag_graph`'s arguments, before the graph starts. `retrieved` is
    written by `retrieve`, read by `rerank`. `reranked` is written by
    `rerank`, read by `generate`. `answer`/`citations` are written by
    `generate` and are the only keys `run_rag_graph` reads back out of the
    final state.

    The four downstream keys are `NotRequired`: the initial state
    `run_rag_graph` builds only sets `query`/`organization_id`/`history`,
    so a state value's true type before its writing node has run is "key
    absent", not "key present with an empty placeholder" -- `NotRequired`
    documents that accurately for mypy rather than lying with a default that
    implies the key is always there.
    """

    query: str
    organization_id: uuid.UUID
    history: list[ConversationTurn]
    retrieved: NotRequired[list[HybridSearchResult]]
    reranked: NotRequired[list[RerankCandidate]]
    answer: NotRequired[str]
    citations: NotRequired[list[ResolvedCitation]]


@dataclass
class GraphContext:
    """Run-scoped external resource(s) injected into node calls via
    LangGraph's `context_schema` mechanism (see the "Database session
    injection" note in the module docstring). The only resource a node
    needs that this module cannot own itself: an active `AsyncSession`,
    supplied fresh by `run_rag_graph`'s caller on every invocation.
    """

    session: AsyncSession


@dataclass(frozen=True)
class RagResult:
    """What `run_rag_graph` hands back to its caller: just the pieces
    issue #16's message endpoint actually needs (the answer text and its
    resolved citations), not the full internal `RagState` -- a caller has no
    business reaching into `retrieved`/`reranked`, those are this graph's
    own intermediate bookkeeping.
    """

    answer: str
    citations: list[ResolvedCitation]


async def retrieve(state: RagState, runtime: Runtime[GraphContext]) -> dict[str, object]:
    """Hybrid search node: calls `hybrid_search_service.hybrid_search` with
    the query/organization_id from state and the session from run-scoped
    context, and writes the fused candidate list into `state["retrieved"]`
    for `rerank` to read next.
    """
    results = await hybrid_search_service.hybrid_search(
        runtime.context.session,
        state["organization_id"],
        state["query"],
    )
    return {"retrieved": results}


def rerank(state: RagState) -> dict[str, object]:
    """Reranking node: converts `state["retrieved"]`'s `HybridSearchResult`
    entries into the `RerankCandidate` shape `reranking_service.rerank`
    expects (`chunk_id` stringified, since `RerankCandidate.chunk_id` is an
    opaque `str` while `HybridSearchResult.chunk_id` is a `uuid.UUID`),
    scores them against the query, and writes the top-K into
    `state["reranked"]` for `generate` to read next.

    Synchronous, not async: `reranking_service.rerank` is itself a
    synchronous call (the same local cross-encoder inference
    `hybrid_search_service.hybrid_search` already calls inline for query
    embedding rather than off-loading to a thread), so this node matches
    that existing repo convention rather than introducing a new one.
    """
    candidates = [
        RerankCandidate(chunk_id=str(result.chunk_id), content=result.content)
        for result in state["retrieved"]
    ]
    reranked = reranking_service.rerank(state["query"], candidates)
    return {"reranked": reranked}


async def generate(state: RagState) -> dict[str, object]:
    """Generation node: calls `generation_service.generate_answer` with the
    query, conversation history, and reranked candidates from state, and
    writes the final answer text and resolved citations into state (see the
    "Streaming" note in the module docstring for why this calls
    `generate_answer` rather than `stream_answer`).
    """
    result = await generation_service.generate_answer(
        state["query"], state["history"], state["reranked"]
    )
    return {"answer": result.answer, "citations": result.citations}


_graph: CompiledStateGraph[RagState, GraphContext, RagState, RagState] | None = None


def _get_graph() -> CompiledStateGraph[RagState, GraphContext, RagState, RagState]:
    """Return the process-wide compiled graph, building it on first use.

    Building a `StateGraph` and compiling it is cheap relative to model
    loading or a network call, but there is still no reason to rebuild it
    per invocation -- the same lazy-singleton pattern
    `reranking_service._get_model` and `generation_service._get_client` use
    for their own once-per-process objects. The compiled graph carries no
    per-call state itself (that lives in `RagState`/`GraphContext`, both
    fresh per `ainvoke` call), so reusing one instance across concurrent
    calls is safe.
    """
    global _graph
    if _graph is None:
        builder = StateGraph(
            state_schema=RagState,
            context_schema=GraphContext,
            input_schema=RagState,
            output_schema=RagState,
        )
        builder.add_node("retrieve", retrieve)
        builder.add_node("rerank", rerank)
        builder.add_node("generate", generate)
        builder.add_edge(START, "retrieve")
        builder.add_edge("retrieve", "rerank")
        builder.add_edge("rerank", "generate")
        builder.add_edge("generate", END)
        _graph = builder.compile()
    return _graph


async def run_rag_graph(
    session: AsyncSession,
    organization_id: uuid.UUID,
    query: str,
    history: list[ConversationTurn] | None = None,
) -> RagResult:
    """Build the initial graph state, run the compiled graph end to end,
    and return the final answer and its resolved citations.

    Args:
        session: an active async SQLAlchemy session, scoped to the caller's
            request/test and injected into `retrieve` via `GraphContext`.
        organization_id: the tenant to scope retrieval to.
        query: the user's current question.
        history: prior turns of this conversation, oldest first, already
            windowed to the most recent N turns by the caller (issue #16;
            see the "Conversation history" note in the module docstring).
            Defaults to an empty list for the first turn of a new
            conversation.

    Returns:
        A `RagResult` with the generated answer text and its resolved
        citations.
    """
    initial_state: RagState = {
        "query": query,
        "organization_id": organization_id,
        "history": list(history) if history else [],
    }
    graph = _get_graph()
    final_state = await graph.ainvoke(
        initial_state,
        context=GraphContext(session=session),
    )
    return RagResult(answer=final_state["answer"], citations=final_state["citations"])
