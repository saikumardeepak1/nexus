"""Wraps sentence-transformers (BAAI/bge-small-en-v1.5) to embed document
chunks at ingestion time and queries at retrieval time, run fully locally
(see docs/TDD.md section 3.2).

This module is deliberately standalone: it has no database, Qdrant, or
Redis dependency, and does not know it is being called from the ingestion
pipeline or the retrieval path (both separate concerns, built in other
issues), which keeps it trivially unit-testable in isolation.

Model lifecycle
----------------
The ``SentenceTransformer`` instance is expensive to construct (it loads
model weights from disk, downloading them from Hugging Face on first run)
so it is loaded exactly once per process and reused for every call, via a
module-level singleton behind ``get_model()``. Both ``embed_documents`` and
``embed_query`` call ``get_model()``, so document and query embeddings are
always produced by the same model instance and therefore land in the same
vector space, which is what makes their cosine similarity meaningful.

Query instruction prefix
-------------------------
BGE models are trained so that, for asymmetric retrieval (short query
against a longer passage), prepending an instruction to the query -- but
not to the passage -- improves retrieval quality. The model card for
``BAAI/bge-small-en-v1.5`` on Hugging Face
(https://huggingface.co/BAAI/bge-small-en-v1.5) recommends the instruction
``"Represent this sentence for searching relevant passages: "`` for queries
in retrieval tasks, and states plainly that "in all cases, no instruction
needs to be added to passages." That is why ``embed_query`` prepends
``_QUERY_INSTRUCTION`` and ``embed_documents`` does not add anything.
"""

from __future__ import annotations

import threading
from typing import cast

from sentence_transformers import SentenceTransformer

from app.core.config import settings

# See the "Query instruction prefix" note above for why this applies only
# to queries and not to document chunks.
_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

_model: SentenceTransformer | None = None
_model_lock = threading.Lock()


def get_model() -> SentenceTransformer:
    """Return the process-wide ``SentenceTransformer`` instance, loading it
    on first call and reusing it on every call after that.

    The double-checked lock avoids a redundant (and slow -- it involves
    disk/network I/O) construction if two threads race to load the model
    before either has finished, without paying lock overhead on the
    common path once the model is already loaded.
    """
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = SentenceTransformer(settings.embedding_model_name)
    return _model


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed a batch of document chunk texts, no instruction prefix (per
    the BGE model card guidance for passages, see module docstring).

    Args:
        texts: Chunk contents to embed, in any order.

    Returns:
        One embedding vector per input text, same order as ``texts``.
        Empty if ``texts`` is empty.
    """
    if not texts:
        return []
    model = get_model()
    embeddings = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
    return cast(list[list[float]], embeddings.tolist())


def embed_query(text: str) -> list[float]:
    """Embed a single retrieval query, with the BGE-recommended instruction
    prefix prepended (see module docstring) so it is comparable against
    document embeddings produced by ``embed_documents``.

    Args:
        text: The raw user query text (the instruction prefix is added
            internally; callers should not add it themselves).

    Returns:
        The query's embedding vector.
    """
    model = get_model()
    embedding = model.encode(
        _QUERY_INSTRUCTION + text, convert_to_numpy=True, normalize_embeddings=True
    )
    return cast(list[float], embedding.tolist())
