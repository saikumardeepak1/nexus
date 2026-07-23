"""Citation-constrained answer generation via Gemini (see docs/TDD.md section
3.2 and issue #15).

This module is the last step of the retrieval pipeline: it takes a user's
query, the conversation's prior turns, and the reranked candidate chunks
(`reranking_service.RerankCandidate`, already scored and ordered by
`reranking_service.rerank`) and turns them into a citation-backed answer. It
has no dependency on Postgres, Qdrant, or the LangGraph orchestration graph
(issue #14, not built yet, will call this module as one node) -- callers are
responsible for producing the candidate list and history however they like
(hybrid search + reranking, a fixture in a test, or anything else), the same
standalone-module shape `reranking_service` and `hybrid_search_service`
already use.

Citation marker format
-----------------------
The model is instructed to cite claims with a bracketed, 1-indexed marker
tied to the candidate's position in the numbered context block it was shown,
e.g. ``[1]`` for the first passage, ``[2]`` for the second, and ``[1][3]``
when a claim is supported by more than one passage. This format is:

- Easy for the model to produce reliably (it is a common citation style in
  its training data, unlike a bespoke tag format).
- Easy to parse deterministically with a single regex, with no ambiguity
  about which passage a marker refers to (position, not chunk_id or content
  matching).
- Easy to resolve: marker ``n`` maps to ``candidates[n - 1]`` in the exact
  list the model was shown, so resolution never depends on the model
  echoing back an opaque `chunk_id` string correctly.

A marker whose number does not correspond to any passage actually shown to
the model (e.g. ``[7]`` when only 5 passages were provided) is a model
hallucination -- `parse_citations` drops it from the resolved citation list
rather than keeping it or raising, since keeping an unresolvable citation
would silently point at a source that was never shown to the model, and
raising would take down an otherwise-good answer over one bad marker.

Streaming
----------
`stream_answer` is an async generator yielding text deltas as Gemini's
streaming API produces them (`client.aio.models.generate_content_stream`),
not a single blocking call, since the API surface this module ultimately
feeds (`POST /v1/conversations/{id}/messages`, issue #16, not built yet)
streams the answer to the client over SSE, and the chat UI (issue #17, not
built yet) needs real token-by-token delivery rather than a single
end-of-generation payload. Citation parsing runs over the *full* generated
text after streaming completes (`generate_answer`, or the caller's own
accumulation of `stream_answer`'s output) rather than per-chunk, since a
citation marker like ``[12]`` can itself be split across two streamed
chunks (``"[1"`` then ``"2]"``) and there is no reliable way to parse a
marker that might not be fully arrived yet.

Client lifecycle
------------------
`Client(...)` is constructed once and cached in the module-level `_client`
global, the same lazy-singleton pattern `reranking_service._get_model` uses
for `CrossEncoder` -- constructing a client is cheap relative to model
loading, but there is still no reason to rebuild it per call, and caching it
keeps the mocking surface for tests identical to `reranking_service`'s: tests
monkeypatch `Client` (the constructor imported into this module's namespace)
rather than `_get_client` itself, so what's under test is genuinely "what
does this module do with the SDK", not a stub of this module's own logic.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

from google.genai import Client, types

from app.core.config import settings
from app.services.reranking_service import RerankCandidate

_client: Client | None = None

# Matches a bracketed, 1-indexed citation marker like "[1]" or "[12]"
# anywhere in the model's generated text. See the "Citation marker format"
# note in the module docstring.
_CITATION_MARKER_PATTERN = re.compile(r"\[(\d+)\]")

SYSTEM_INSTRUCTION = (
    "You are Nexus, an assistant that answers questions strictly from the "
    "numbered context passages the user provides, never from outside "
    "knowledge. Follow these rules exactly:\n"
    "1. Answer using ONLY information contained in the numbered context "
    "passages below the question. Do not use any information you know from "
    "outside those passages, even if you are confident it is correct.\n"
    "2. Every factual claim in your answer must be immediately followed by "
    "a citation marker in the form [n], where n is the number of the "
    "context passage that supports the claim, e.g. \"Espresso is brewed at "
    "90-96C [1].\" If a claim is supported by more than one passage, cite "
    "all of them, e.g. [1][3].\n"
    "3. Only use passage numbers that were actually given to you. Never "
    "invent a number higher than the number of passages provided, and "
    "never cite a passage that does not support the claim next to it.\n"
    "4. If the provided passages do not contain enough information to "
    "answer the question, say so explicitly instead of guessing or filling "
    "the gap with outside knowledge.\n"
    "5. Prior turns of the conversation are provided for context only; the "
    "citation rules above still apply only to the numbered passages given "
    "with the current question, since only those passages are shown to you "
    "again with each new question."
)


@dataclass(frozen=True)
class ConversationTurn:
    """One prior turn of the conversation, used as generation context.

    `role` is `"user"` or `"assistant"`, mirroring the `Message.role` values
    the (not-yet-built) conversation persistence layer (issue #16) will read
    these from -- this module only cares about the two roles, not any other
    metadata a `Message` row might carry.
    """

    role: str
    content: str


@dataclass(frozen=True)
class GenerationPrompt:
    """The fully-assembled prompt, ready to hand to Gemini.

    Kept as a plain dataclass (system instruction, history, final user
    message) rather than an SDK type so `build_prompt` stays pure and
    trivially assertable in tests -- `stream_answer` is the only place this
    gets converted into `google.genai.types` objects for the actual API
    call.
    """

    system_instruction: str
    history: list[ConversationTurn]
    user_message: str


@dataclass(frozen=True)
class ResolvedCitation:
    """A citation marker found in generated text, resolved back to the real
    candidate chunk it refers to.

    `marker` is the citation number as it appeared in the text (e.g. `2` for
    `"[2]"`); `text_position` is the character offset of that marker's `[`
    within the full answer text, so a caller (e.g. a future UI) can place an
    inline citation exactly where the model put it rather than only at the
    end of the answer.
    """

    chunk_id: str
    relevance_score: float | None
    marker: int
    text_position: int


@dataclass(frozen=True)
class GenerationResult:
    """The final output of generation: the raw answer text (citation
    markers left in place, not stripped -- a caller/UI resolves them into
    links, it does not need the markers removed to do that) plus every
    marker that successfully resolved to a real candidate.
    """

    answer: str
    citations: list[ResolvedCitation]


def _format_context(candidates: list[RerankCandidate]) -> str:
    """Render `candidates` as a numbered block the model can cite by
    position, 1-indexed to match the citation marker format (see the
    "Citation marker format" note in the module docstring).
    """
    if not candidates:
        return "(no context passages were retrieved for this question)"
    return "\n\n".join(
        f"[{index}] {candidate.content}" for index, candidate in enumerate(candidates, start=1)
    )


def build_prompt(
    query: str,
    history: list[ConversationTurn],
    candidates: list[RerankCandidate],
) -> GenerationPrompt:
    """Assemble the full generation prompt from the user's query, prior
    conversation turns, and the reranked candidate chunks.

    Pure function: no I/O, no Gemini call, just string assembly -- entirely
    testable against hand-built `ConversationTurn`/`RerankCandidate` lists
    with no mocking required.

    Args:
        query: the user's current question.
        history: prior turns of this conversation, oldest first. Passed
            through unchanged into `GenerationPrompt.history`; empty for the
            first turn of a new conversation.
        candidates: reranked chunks (`reranking_service.rerank`'s output, or
            an equivalent hand-built list for tests), highest relevance
            first. Numbered 1-indexed in the context block, matching the
            citation marker format the model is instructed to use.

    Returns:
        A `GenerationPrompt` ready for `stream_answer`.
    """
    context_block = _format_context(candidates)
    user_message = f"Context passages:\n{context_block}\n\nQuestion:\n{query}"
    return GenerationPrompt(
        system_instruction=SYSTEM_INSTRUCTION,
        history=list(history),
        user_message=user_message,
    )


def _get_client() -> Client:
    """Return the process-wide `Client`, constructing it on first use (see
    the "Client lifecycle" note in the module docstring).
    """
    global _client
    if _client is None:
        _client = Client(api_key=settings.gemini_api_key)
    return _client


def _build_contents(prompt: GenerationPrompt) -> list[types.ContentUnion]:
    """Convert a `GenerationPrompt`'s history and final user message into
    the shape `generate_content_stream`'s `contents` parameter expects.

    History turns map `"assistant"` to Gemini's `"model"` role (Gemini has
    no `"assistant"` role) and everything else to `"user"`; the final turn
    is always the assembled context-block-plus-question user message,
    appended after history so it is the most recent turn in the
    conversation the model sees.

    Typed `list[types.ContentUnion]` rather than `list[types.Content]`: the
    SDK's `contents` parameter is typed as
    `Union[ContentListUnion, ContentListUnionDict]` where
    `ContentListUnion = Union[ContentUnion, list[ContentUnion]]` --
    `list[types.Content]` is a plain `Content` list, but under mypy's
    invariant generics that is not automatically compatible with
    `list[ContentUnion]` even though `Content` is one of `ContentUnion`'s
    members, so the list has to be declared with the wider element type
    from construction for the call in `stream_answer` to type-check.
    """
    contents: list[types.ContentUnion] = [
        types.Content(
            role="model" if turn.role == "assistant" else "user",
            parts=[types.Part.from_text(text=turn.content)],
        )
        for turn in prompt.history
    ]
    contents.append(
        types.Content(role="user", parts=[types.Part.from_text(text=prompt.user_message)])
    )
    return contents


async def stream_answer(prompt: GenerationPrompt) -> AsyncIterator[str]:
    """Stream Gemini's answer to `prompt` as text deltas, as they arrive.

    An async generator, not a blocking call returning one complete string:
    the (not-yet-built) message endpoint (issue #16) streams the answer to
    the client over SSE as it generates, and the chat UI (issue #17) needs
    real token-by-token delivery -- both need this module to yield
    incrementally rather than buffer the whole answer before returning
    anything.

    Args:
        prompt: the assembled prompt from `build_prompt`.

    Yields:
        Text deltas in generation order. A chunk with no text (e.g. a
        stream chunk carrying only safety/finish metadata) yields nothing
        for that chunk rather than an empty string, so callers concatenating
        yielded values never need to filter empties themselves.
    """
    client = _get_client()
    contents = _build_contents(prompt)
    config = types.GenerateContentConfig(system_instruction=prompt.system_instruction)

    stream = await client.aio.models.generate_content_stream(
        model=settings.gemini_model_name,
        contents=contents,
        config=config,
    )
    async for chunk in stream:
        if chunk.text:
            yield chunk.text


def parse_citations(
    answer_text: str,
    candidates: list[RerankCandidate],
) -> list[ResolvedCitation]:
    """Parse citation markers out of `answer_text` and resolve each one to
    the real candidate chunk it refers to.

    Every ``[n]`` marker resolves against `candidates[n - 1]` (1-indexed to
    match the numbering `_format_context` showed the model). A marker whose
    number falls outside `candidates`' range -- the model hallucinated a
    passage number that was never shown to it -- is dropped from the
    returned list rather than kept or raised on (see the "Citation marker
    format" note in the module docstring); it is not an error condition for
    this function, just an unresolvable marker.

    A marker that repeats (e.g. ``[1]`` cited twice in the same answer)
    produces one `ResolvedCitation` per occurrence, each with its own
    `text_position`, since each occurrence is a separate place in the answer
    a caller/UI would want to render an inline citation link.

    Args:
        answer_text: the full generated answer text (after streaming
            completes), markers left in place.
        candidates: the exact candidate list the model was shown, in the
            same order `build_prompt` numbered them in -- resolution is by
            position, so a different order or a different list than what
            the model actually saw will resolve markers to the wrong chunks.

    Returns:
        Every marker in `answer_text` that resolved to a real candidate, in
        the order it appears in the text. Empty if `answer_text` contains no
        markers, or none of them resolve.
    """
    citations: list[ResolvedCitation] = []
    for match in _CITATION_MARKER_PATTERN.finditer(answer_text):
        marker = int(match.group(1))
        index = marker - 1
        if 0 <= index < len(candidates):
            candidate = candidates[index]
            citations.append(
                ResolvedCitation(
                    chunk_id=candidate.chunk_id,
                    relevance_score=candidate.relevance_score,
                    marker=marker,
                    text_position=match.start(),
                )
            )
    return citations


async def generate_answer(
    query: str,
    history: list[ConversationTurn],
    candidates: list[RerankCandidate],
) -> GenerationResult:
    """Convenience wrapper: build the prompt, stream the answer to
    completion, and parse its citations -- for callers that want one
    complete, citation-resolved result rather than raw token-by-token
    streaming (e.g. a non-streaming caller, or a test asserting on the final
    answer). The (not-yet-built) LangGraph `generate` node and the SSE
    message endpoint (issues #14/#16) are expected to call `stream_answer`
    directly instead, so they can forward each delta to the client as it
    arrives rather than waiting for the full answer here first.

    Args:
        query: the user's current question.
        history: prior turns of this conversation, oldest first.
        candidates: reranked chunks, highest relevance first.

    Returns:
        A `GenerationResult` with the full answer text and its resolved
        citations.
    """
    prompt = build_prompt(query, history, candidates)
    chunks = [chunk async for chunk in stream_answer(prompt)]
    answer = "".join(chunks)
    citations = parse_citations(answer, candidates)
    return GenerationResult(answer=answer, citations=citations)
