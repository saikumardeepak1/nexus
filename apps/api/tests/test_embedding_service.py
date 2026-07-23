"""Unit tests for app.services.embedding_service. No database, Qdrant, or
Redis needed -- this service is standalone (text in, vectors out), per
docs/TDD.md section 3.2.

These tests exercise the real BAAI/bge-small-en-v1.5 model via
sentence-transformers (downloaded from Hugging Face on first run in a
fresh environment), except for the "loaded once" test, which mocks
``SentenceTransformer`` itself since that is the cleanest way to count
constructions without relying on timing.
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import pytest

from app.services import embedding_service
from app.services.embedding_service import embed_documents, embed_query, get_model


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b)


@pytest.fixture(autouse=True)
def _reset_model_singleton() -> None:
    """Each test gets a clean view of the module-level singleton so tests
    that mock ``SentenceTransformer`` don't leak a mock instance into a
    test that expects the real model, and vice versa.
    """
    embedding_service._model = None
    yield
    embedding_service._model = None


# --- Dimensionality --------------------------------------------------------


def test_embed_documents_dimensionality_matches_model() -> None:
    model = get_model()
    expected_dim = model.get_sentence_embedding_dimension()

    vectors = embed_documents(["The quick brown fox jumps over the lazy dog."])

    assert len(vectors) == 1
    assert len(vectors[0]) == expected_dim


def test_embed_query_dimensionality_matches_model() -> None:
    model = get_model()
    expected_dim = model.get_sentence_embedding_dimension()

    vector = embed_query("What does the fox jump over?")

    assert len(vector) == expected_dim


def test_embed_documents_empty_list_returns_empty_list() -> None:
    assert embed_documents([]) == []


# --- Determinism ------------------------------------------------------------


def test_embed_documents_is_deterministic() -> None:
    text = "Nexus retrieves cited passages instead of hallucinating answers."

    first = embed_documents([text])[0]
    second = embed_documents([text])[0]

    assert first == second


def test_embed_query_is_deterministic() -> None:
    text = "How does Nexus avoid hallucinating answers?"

    first = embed_query(text)
    second = embed_query(text)

    assert first == second


# --- Similarity sanity -------------------------------------------------------


def test_near_duplicate_text_scores_higher_than_unrelated_text() -> None:
    anchor = "The company's refund policy allows returns within 30 days of purchase."
    near_duplicate = "Customers can return purchased items within 30 days for a refund."
    unrelated = "The mitochondria is the powerhouse of the cell."

    anchor_vec, near_duplicate_vec, unrelated_vec = embed_documents(
        [anchor, near_duplicate, unrelated]
    )

    duplicate_similarity = _cosine_similarity(anchor_vec, near_duplicate_vec)
    unrelated_similarity = _cosine_similarity(anchor_vec, unrelated_vec)

    assert duplicate_similarity > unrelated_similarity


def test_query_embedding_scores_relevant_document_higher_than_unrelated() -> None:
    """Sanity check for the actual retrieval use case: a query embedded via
    embed_query should be more similar to the document chunk it's asking
    about than to an unrelated chunk, using the same model/vector space.
    """
    relevant_doc = "Employees can request parental leave through the HR portal."
    unrelated_doc = "The recipe calls for two cups of flour and a teaspoon of salt."
    query = "How do I request parental leave?"

    relevant_vec, unrelated_vec = embed_documents([relevant_doc, unrelated_doc])
    query_vec = embed_query(query)

    relevant_similarity = _cosine_similarity(query_vec, relevant_vec)
    unrelated_similarity = _cosine_similarity(query_vec, unrelated_vec)

    assert relevant_similarity > unrelated_similarity


# --- Model loaded once, not per call -----------------------------------------


def test_model_is_loaded_once_across_multiple_calls() -> None:
    fake_instance = MagicMock()
    fake_instance.get_sentence_embedding_dimension.return_value = 384
    fake_instance.encode.return_value = MagicMock(
        tolist=MagicMock(side_effect=lambda: [0.0] * 384)
    )

    with patch(
        "app.services.embedding_service.SentenceTransformer", return_value=fake_instance
    ) as mock_constructor:
        embed_documents(["first call"])
        embed_documents(["second call"])
        embed_query("third call, a query")
        get_model()

        assert mock_constructor.call_count == 1
