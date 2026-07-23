"""Unit tests for app.services.generation_service. No database, Qdrant, or
live Gemini call -- every test here mocks the `google.genai` client boundary
(the `Client` constructor imported into the module's namespace, the same
pattern `test_reranking_service.py` uses for `CrossEncoder`) rather than
stubbing this module's own functions, per docs/TDD.md section 3.2 and issue
#15.

A live smoke test against the real Gemini API is intentionally out of scope
here (see the PR description) -- these tests only prove the prompt
construction, streaming behavior, and citation resolution logic this module
owns, not that Gemini itself behaves as documented.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.services import generation_service
from app.services.generation_service import (
    ConversationTurn,
    build_prompt,
    generate_answer,
    parse_citations,
    stream_answer,
)
from app.services.reranking_service import RerankCandidate

# --- Fake google-genai client -----------------------------------------
# Mocks at the SDK boundary: a fake `Client` standing in for
# `google.genai.Client`, with just enough surface
# (`.aio.models.generate_content_stream`) to exercise this module's code,
# and a `captured` dict so tests can assert on exactly what this module
# sent to the SDK.


@dataclass
class _FakeChunk:
    """Stands in for one `google.genai.types.GenerateContentResponse`
    streamed chunk -- this module only ever reads `.text` off it.
    """

    text: str


class _FakeStream:
    """Stands in for the `AsyncIterator[GenerateContentResponse]`
    `generate_content_stream` returns.
    """

    def __init__(self, chunks: list[_FakeChunk]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> AsyncIterator[_FakeChunk]:
        return self._generate()

    async def _generate(self) -> AsyncIterator[_FakeChunk]:
        for chunk in self._chunks:
            yield chunk


@dataclass
class _FakeModels:
    chunks: list[_FakeChunk]
    captured: dict[str, Any]

    async def generate_content_stream(
        self, *, model: str, contents: object, config: object
    ) -> _FakeStream:
        self.captured["model"] = model
        self.captured["contents"] = contents
        self.captured["config"] = config
        return _FakeStream(self.chunks)


@dataclass
class _FakeAio:
    models: _FakeModels


@dataclass
class _FakeClient:
    """Stands in for `google.genai.Client`. Constructed by a factory
    monkeypatched over `generation_service.Client`, mirroring
    `test_reranking_service.py`'s `mock_constructor` pattern for
    `CrossEncoder`.
    """

    aio: _FakeAio
    api_key: str = field(default="", kw_only=True)


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch, chunks: list[str]
) -> dict[str, Any]:
    """Monkeypatch `generation_service.Client` so the next call to
    `_get_client()` builds a `_FakeClient` streaming `chunks` (as text
    deltas, in order) instead of a real SDK client, and reset the
    module-level client cache so the fake is actually used. Returns the
    `captured` dict the fake client's `generate_content_stream` fills in
    with whatever this module passed it.
    """
    captured: dict[str, Any] = {}
    fake_chunks = [_FakeChunk(text=text) for text in chunks]

    def _fake_constructor(*, api_key: str) -> _FakeClient:
        return _FakeClient(aio=_FakeAio(models=_FakeModels(fake_chunks, captured)), api_key=api_key)

    monkeypatch.setattr(generation_service, "Client", _fake_constructor)
    monkeypatch.setattr(generation_service, "_client", None)
    return captured


# --- Fixture candidates -------------------------------------------------

_ESPRESSO_TEMP = RerankCandidate(
    chunk_id="chunk-espresso-temp",
    content=(
        "Espresso should be extracted with water between 90 and 96 degrees "
        "Celsius."
    ),
    relevance_score=0.91,
)
_ESPRESSO_GRIND = RerankCandidate(
    chunk_id="chunk-espresso-grind",
    content="A fine, consistent grind is essential for espresso.",
    relevance_score=0.74,
)
_ESPRESSO_MILK = RerankCandidate(
    chunk_id="chunk-espresso-milk",
    content="Steaming milk for a cappuccino builds microfoam.",
    relevance_score=0.52,
)

_CANDIDATES = [_ESPRESSO_TEMP, _ESPRESSO_GRIND, _ESPRESSO_MILK]
_QUERY = "What temperature should espresso be brewed at?"


# --- build_prompt ---------------------------------------------------------


def test_prompt_contains_constraint_instruction() -> None:
    prompt = build_prompt(_QUERY, [], _CANDIDATES)

    assert "ONLY" in prompt.system_instruction
    assert "outside" in prompt.system_instruction.lower()
    assert "[n]" in prompt.system_instruction or "[1]" in prompt.system_instruction


def test_prompt_contains_numbered_chunk_content() -> None:
    prompt = build_prompt(_QUERY, [], _CANDIDATES)

    assert "[1] " + _ESPRESSO_TEMP.content in prompt.user_message
    assert "[2] " + _ESPRESSO_GRIND.content in prompt.user_message
    assert "[3] " + _ESPRESSO_MILK.content in prompt.user_message


def test_prompt_contains_the_query() -> None:
    prompt = build_prompt(_QUERY, [], _CANDIDATES)
    assert _QUERY in prompt.user_message


def test_prompt_with_no_candidates_does_not_crash_and_says_so() -> None:
    prompt = build_prompt(_QUERY, [], [])
    assert "no context passages" in prompt.user_message.lower()


def test_prompt_includes_conversation_history() -> None:
    history = [
        ConversationTurn(role="user", content="What is a good espresso grind size?"),
        ConversationTurn(role="assistant", content="A fine, consistent grind [1]."),
    ]
    prompt = build_prompt(_QUERY, history, _CANDIDATES)

    assert prompt.history == history


# --- stream_answer: incremental streaming --------------------------------


async def test_stream_answer_yields_chunks_incrementally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of `stream_answer` being an async generator is that a
    caller can act on each piece of text as it arrives rather than waiting
    for the full answer -- this asserts the yielded sequence matches the
    mocked stream's chunk boundaries exactly (three separate yields, not one
    concatenated string).
    """
    _install_fake_client(monkeypatch, ["Espresso is brewed at ", "90-96C ", "[1]."])

    prompt = build_prompt(_QUERY, [], _CANDIDATES)
    received: list[str] = []
    async for delta in stream_answer(prompt):
        received.append(delta)

    assert received == ["Espresso is brewed at ", "90-96C ", "[1]."]


async def test_stream_answer_skips_empty_text_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_client(monkeypatch, ["Hello", "", " world"])

    prompt = build_prompt(_QUERY, [], _CANDIDATES)
    received = [delta async for delta in stream_answer(prompt)]

    assert received == ["Hello", " world"]


async def test_stream_answer_sends_prompt_to_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_fake_client(monkeypatch, ["answer"])

    prompt = build_prompt(_QUERY, [], _CANDIDATES)
    async for _ in stream_answer(prompt):
        pass

    assert captured["config"].system_instruction == prompt.system_instruction
    sent_contents = captured["contents"]
    assert sent_contents[-1].role == "user"
    assert sent_contents[-1].parts[0].text == prompt.user_message


async def test_stream_answer_includes_history_in_contents_sent_to_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-turn scenario: a follow-up question's prompt must carry the
    prior turn's content through to what is actually sent to the Gemini
    client, not just into `GenerationPrompt.history`.
    """
    captured = _install_fake_client(monkeypatch, ["follow-up answer"])

    first_turn_answer = "A fine, consistent grind is best [2]."
    history = [
        ConversationTurn(role="user", content="What grind size for espresso?"),
        ConversationTurn(role="assistant", content=first_turn_answer),
    ]
    prompt = build_prompt("And what temperature?", history, _CANDIDATES)

    async for _ in stream_answer(prompt):
        pass

    sent_contents = captured["contents"]
    # Two history turns plus the final user turn.
    assert len(sent_contents) == 3
    assert sent_contents[0].role == "user"
    assert sent_contents[0].parts[0].text == "What grind size for espresso?"
    # Gemini has no "assistant" role -- the prior assistant turn maps to "model".
    assert sent_contents[1].role == "model"
    assert sent_contents[1].parts[0].text == first_turn_answer
    assert sent_contents[2].role == "user"
    assert "And what temperature?" in sent_contents[2].parts[0].text


# --- parse_citations -------------------------------------------------------


def test_citation_markers_resolve_to_correct_chunk_ids() -> None:
    text = "Espresso is brewed at 90-96C [1]. Use a fine grind [2]."

    citations = parse_citations(text, _CANDIDATES)

    assert [c.chunk_id for c in citations] == [
        _ESPRESSO_TEMP.chunk_id,
        _ESPRESSO_GRIND.chunk_id,
    ]
    assert [c.marker for c in citations] == [1, 2]
    assert citations[0].relevance_score == _ESPRESSO_TEMP.relevance_score
    assert citations[1].relevance_score == _ESPRESSO_GRIND.relevance_score


def test_citation_text_positions_point_at_the_marker_in_the_text() -> None:
    text = "Brewed at 90-96C [1]."
    citations = parse_citations(text, _CANDIDATES)

    assert len(citations) == 1
    assert text[citations[0].text_position : citations[0].text_position + 3] == "[1]"


def test_hallucinated_citation_marker_is_dropped_not_raised() -> None:
    # Only 3 candidates were provided, so [7] refers to nothing real.
    text = "Espresso is best served warm [7]."

    citations = parse_citations(text, _CANDIDATES)

    assert citations == []


def test_mix_of_valid_and_hallucinated_markers_keeps_only_valid_ones() -> None:
    text = "Brewed at 90-96C [1]. Some claim [9]. Fine grind helps [2]."

    citations = parse_citations(text, _CANDIDATES)

    assert [c.marker for c in citations] == [1, 2]
    assert [c.chunk_id for c in citations] == [
        _ESPRESSO_TEMP.chunk_id,
        _ESPRESSO_GRIND.chunk_id,
    ]


def test_marker_zero_and_negative_style_out_of_range_is_dropped() -> None:
    # "[0]" is out of range since markers are 1-indexed.
    text = "No such passage [0]."
    assert parse_citations(text, _CANDIDATES) == []


def test_no_markers_returns_empty_list() -> None:
    assert parse_citations("Espresso is delicious.", _CANDIDATES) == []


def test_repeated_marker_produces_one_citation_per_occurrence() -> None:
    text = "Brewed hot [1]. Confirmed again [1]."
    citations = parse_citations(text, _CANDIDATES)

    assert [c.marker for c in citations] == [1, 1]
    assert citations[0].text_position != citations[1].text_position


def test_empty_candidates_drops_every_marker() -> None:
    text = "This claims something [1]."
    assert parse_citations(text, []) == []


# --- generate_answer (end-to-end convenience wrapper) ----------------------


async def test_generate_answer_returns_full_text_and_resolved_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_client(
        monkeypatch, ["Espresso is brewed at 90-96C ", "[1].", " Use a fine grind [2]."]
    )

    result = await generate_answer(_QUERY, [], _CANDIDATES)

    assert result.answer == "Espresso is brewed at 90-96C [1]. Use a fine grind [2]."
    assert [c.chunk_id for c in result.citations] == [
        _ESPRESSO_TEMP.chunk_id,
        _ESPRESSO_GRIND.chunk_id,
    ]


async def test_generate_answer_drops_hallucinated_citation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_client(monkeypatch, ["Answer citing a passage that was never given [9]."])

    result = await generate_answer(_QUERY, [], _CANDIDATES)

    assert result.citations == []
    # The raw text is preserved even though its only citation didn't resolve.
    assert "[9]" in result.answer
