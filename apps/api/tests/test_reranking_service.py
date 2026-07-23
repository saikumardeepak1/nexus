"""Unit tests for app.services.reranking_service. No database, Qdrant, or
other service dependency -- this module scores a query against a list of
candidate texts entirely in-process, per docs/TDD.md section 3.2 and issue
#12.

The fixture corpus below is hand-written, not generated at test time: eight
short passages across two unrelated topics (espresso extraction and
Kubernetes networking), with one passage per topic known ahead of time to be
the single most relevant answer to the query used here. Real model inference
(BAAI/bge-reranker-base, downloaded once from HuggingFace Hub on first run)
backs every test except test_model_loaded_once_not_per_call, which mocks
CrossEncoder specifically to make the "loaded once" claim checkable without
relying on flaky timing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services import reranking_service
from app.services.reranking_service import RerankCandidate, rerank

# --- Fixture corpus -------------------------------------------------------
# Two unrelated topics so a cross-encoder actually has something to
# discriminate between; passages within a topic range from directly on-point
# to merely topic-adjacent, so scoring can't just key off "mentions the
# topic word."

_ESPRESSO_ON_POINT = RerankCandidate(
    chunk_id="espresso-temp",
    content=(
        "Espresso should be extracted with water between 90 and 96 degrees "
        "Celsius; brewing cooler under-extracts and tastes sour, brewing "
        "hotter over-extracts and tastes bitter and burnt."
    ),
)
_ESPRESSO_TANGENTIAL_1 = RerankCandidate(
    chunk_id="espresso-history",
    content=(
        "Espresso originated in Italy in the early twentieth century, when "
        "engineers began experimenting with steam pressure to brew coffee "
        "faster than a drip pot."
    ),
)
_ESPRESSO_TANGENTIAL_2 = RerankCandidate(
    chunk_id="espresso-grind",
    content=(
        "A fine, consistent grind is essential for espresso; too coarse and "
        "the shot runs too fast, too fine and the machine chokes."
    ),
)
_ESPRESSO_TANGENTIAL_3 = RerankCandidate(
    chunk_id="espresso-milk",
    content=(
        "Steaming milk for a cappuccino involves introducing air early to "
        "build microfoam, then submerging the wand to heat the milk through."
    ),
)
_K8S_ON_POINT = RerankCandidate(
    chunk_id="k8s-networking",
    content=(
        "Every pod in a Kubernetes cluster gets its own IP address, and pods "
        "on different nodes can reach each other directly without NAT, which "
        "is what the Kubernetes networking model requires of any CNI plugin."
    ),
)
_K8S_TANGENTIAL_1 = RerankCandidate(
    chunk_id="k8s-scheduling",
    content=(
        "The Kubernetes scheduler assigns a pod to a node based on resource "
        "requests, taints and tolerations, and affinity rules."
    ),
)
_K8S_TANGENTIAL_2 = RerankCandidate(
    chunk_id="k8s-secrets",
    content=(
        "Kubernetes Secrets store sensitive values like API keys, base64 "
        "encoded rather than encrypted by default at rest."
    ),
)
_K8S_TANGENTIAL_3 = RerankCandidate(
    chunk_id="k8s-rollout",
    content=(
        "A rolling update replaces pods gradually according to "
        "maxSurge/maxUnavailable settings, so a deployment never drops to "
        "zero ready replicas."
    ),
)

_QUERY = "What temperature should espresso be brewed at?"

# The known-relevant passage (_ESPRESSO_ON_POINT) is placed *last*, as far
# as possible from where a naive pass-through would put a "top" result, so a
# top-1/top-2 match after rerank proves real re-scoring rather than an
# accidental echo of input order.
_CANDIDATES_RELEVANT_LAST = [
    _K8S_ON_POINT,
    _K8S_TANGENTIAL_1,
    _ESPRESSO_TANGENTIAL_1,
    _K8S_TANGENTIAL_2,
    _ESPRESSO_TANGENTIAL_2,
    _K8S_TANGENTIAL_3,
    _ESPRESSO_TANGENTIAL_3,
    _ESPRESSO_ON_POINT,
]


def test_relevant_passage_lands_top_two_despite_low_input_position() -> None:
    results = rerank(_QUERY, _CANDIDATES_RELEVANT_LAST, top_k=5)

    top_two_ids = [r.chunk_id for r in results[:2]]
    assert _ESPRESSO_ON_POINT.chunk_id in top_two_ids


def test_output_order_differs_from_input_order() -> None:
    results = rerank(_QUERY, _CANDIDATES_RELEVANT_LAST, top_k=len(_CANDIDATES_RELEVANT_LAST))

    input_order = [c.chunk_id for c in _CANDIDATES_RELEVANT_LAST]
    output_order = [r.chunk_id for r in results]

    assert output_order != input_order
    # Specifically: the known-relevant passage moved from last to somewhere
    # earlier, not just an unrelated shuffle among the irrelevant ones.
    assert output_order.index(_ESPRESSO_ON_POINT.chunk_id) < input_order.index(
        _ESPRESSO_ON_POINT.chunk_id
    )


def test_top_k_truncates_results() -> None:
    results = rerank(_QUERY, _CANDIDATES_RELEVANT_LAST, top_k=3)
    assert len(results) == 3


def test_scores_are_populated_and_descending() -> None:
    results = rerank(_QUERY, _CANDIDATES_RELEVANT_LAST, top_k=len(_CANDIDATES_RELEVANT_LAST))

    scores = [r.relevance_score for r in results]
    assert all(score is not None for score in scores)
    assert scores == sorted(scores, reverse=True)


def test_empty_candidates_returns_empty_list() -> None:
    assert rerank(_QUERY, [], top_k=5) == []


def test_non_positive_top_k_returns_empty_list() -> None:
    assert rerank(_QUERY, _CANDIDATES_RELEVANT_LAST, top_k=0) == []
    assert rerank(_QUERY, _CANDIDATES_RELEVANT_LAST, top_k=-1) == []


def test_model_loaded_once_not_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies the process-wide caching in `_get_model`: constructing
    `CrossEncoder` is expensive (it loads model weights from disk/HuggingFace
    Hub), so `rerank` must reuse one instance across calls rather than
    reconstructing it every time. Mocks `CrossEncoder` itself (rather than
    asserting on timing, which would be flaky) so the constructor call count
    is directly checkable.
    """
    monkeypatch.setattr(reranking_service, "_model", None)

    mock_instance = MagicMock()
    mock_instance.predict.return_value = [0.9, 0.1]
    mock_constructor = MagicMock(return_value=mock_instance)
    monkeypatch.setattr(reranking_service, "CrossEncoder", mock_constructor)

    two_candidates = [
        RerankCandidate(chunk_id="a", content="alpha"),
        RerankCandidate(chunk_id="b", content="beta"),
    ]

    rerank("query one", two_candidates, top_k=2)
    rerank("query two", two_candidates, top_k=2)
    rerank("query three", two_candidates, top_k=2)

    assert mock_constructor.call_count == 1
