"""Cross-encoder reranking of hybrid-search candidates (see docs/TDD.md
section 3.2).

This module is deliberately standalone: it takes a query plus an
already-assembled list of candidate chunk texts and returns them re-ordered
by cross-encoder relevance. It has no dependency on Qdrant, Postgres, Redis,
the embedding model, or `hybrid_search_service` (issue #11, a separate,
not-yet-built concern) -- callers are responsible for producing the
candidate list however they like (hybrid search, a fixture in a test, or
anything else).

Cross-encoder vs. bi-encoder
-----------------------------
`embedding_service` (a separate module) uses a bi-encoder
(`sentence_transformers.SentenceTransformer`) that embeds the query and each
passage *independently* into vectors compared by cosine similarity -- cheap
enough to run against an entire corpus. A cross-encoder
(`sentence_transformers.CrossEncoder`) instead feeds the query and a passage
into the model *together* as one input and outputs a single relevance score
for that pair. That joint attention over both texts is more accurate than
comparing separately-embedded vectors, but it means scoring is O(candidates)
model calls with no way to precompute a passage's representation ahead of
time -- feasible only against a small candidate set (the hybrid-search
shortlist), never a whole corpus. This is why reranking is a second pass
over a handful of candidates, not a replacement for retrieval.

Model lifecycle
----------------
`CrossEncoder(...)` loads model weights from disk/HuggingFace Hub, which is
expensive (real work, not something to repeat per request). `_get_model`
lazily constructs the model once on first use and caches it in the
module-level `_model` global, so every call after the first reuses the same
loaded model for the lifetime of the process -- the "loaded once at process
startup" requirement, without paying the load cost for processes (e.g. a
test run that never calls `rerank`) that never need the model at all.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from sentence_transformers import CrossEncoder

from app.core.config import settings

_model: CrossEncoder | None = None


@dataclass(frozen=True)
class RerankCandidate:
    """One candidate chunk to be scored against a query, and (once scored)
    the result of that scoring.

    Deliberately small and independent of any DB/Qdrant row shape, so
    `hybrid_search_service` (later) has a clean, stable contract to build on
    rather than depending on this module's internals -- the same shape
    `LexicalSearchResult` fills for `lexical_search_service`.

    `chunk_id` is an opaque identifier (e.g. `str(chunk.id)`): this module
    never looks it up or interprets it, it only carries it through so a
    caller can map a reranked result back to its source chunk.
    """

    chunk_id: str
    content: str
    relevance_score: float | None = None


def _get_model() -> CrossEncoder:
    """Return the process-wide `CrossEncoder`, constructing it on first use.

    Every subsequent call reuses the same instance rather than reloading
    model weights, which is what makes `rerank` safe to call once per
    request instead of once per process.
    """
    global _model
    if _model is None:
        _model = CrossEncoder(settings.reranker_model_name)
    return _model


def rerank(
    query: str,
    candidates: list[RerankCandidate],
    top_k: int = 5,
) -> list[RerankCandidate]:
    """Score `candidates` against `query` with the local BGE cross-encoder
    and return the top `top_k`, sorted by descending relevance.

    Args:
        query: the user's search/question text.
        candidates: chunks to score, in whatever order the caller assembled
            them (e.g. a hybrid-search candidate set). Input order carries
            no meaning here and is not preserved in the output.
        top_k: maximum number of results to return. Non-positive values
            (or an empty `candidates` list) short-circuit to an empty list
            before the model is ever invoked.

    Returns:
        Up to `top_k` `RerankCandidate` instances, each with
        `relevance_score` populated, ordered highest score first.
    """
    if not candidates or top_k <= 0:
        return []

    model = _get_model()
    pairs = [(query, candidate.content) for candidate in candidates]
    # sentence-transformers' installed CrossEncoder.predict signature types
    # its argument against a large multi-modal (text/image/audio/video) pair
    # union to support inputs this module never passes; mypy's list
    # invariance then can't match our plain list[tuple[str, str]] against
    # that union even though every element is a valid member of it. This is
    # a typing-only mismatch (verified against real model output in
    # tests/test_reranking_service.py), not a real bug.
    scores = model.predict(pairs)  # type: ignore[arg-type]

    scored = [
        replace(candidate, relevance_score=float(score))
        for candidate, score in zip(candidates, scores, strict=True)
    ]
    scored.sort(key=_relevance_score, reverse=True)

    return scored[:top_k]


def _relevance_score(candidate: RerankCandidate) -> float:
    """Sort key for a scored `RerankCandidate`.

    `relevance_score` is typed `float | None` because an unscored
    `RerankCandidate` (as constructed by a caller before `rerank` runs) has
    none yet; every candidate this function is ever called on has already
    been through the `replace(...)` above, so the assertion never fires in
    practice and exists only to keep mypy honest about that invariant.
    """
    assert candidate.relevance_score is not None
    return candidate.relevance_score
