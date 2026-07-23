"""RQ job definitions run by the worker process.

``process_document`` is enqueued by ``app.services.ingestion_service`` right
after a document upload is persisted (see docs/TDD.md sections 3.2 and 3.5).

The body below is a placeholder: it only proves the enqueue/dequeue contract
works end to end. The real parsing/chunking/embedding/indexing pipeline
lands in later issues (chunking, async pipeline, embedding, Qdrant
indexing) and will replace this function's body without changing its name
or signature, so ``queue.enqueue("app.workers.jobs.process_document", ...)``
call sites do not need to change when that lands.
"""

import logging

logger = logging.getLogger(__name__)


def process_document(document_id: str) -> None:
    """Process an uploaded document: parse, chunk, embed, and index it.

    Placeholder for now -- just logs that the job ran. A later issue fills
    in the real body (parse with pypdf/plain text, chunk, embed via
    sentence-transformers, upsert into Qdrant, write Chunk rows, and flip
    Document.status to "ready" or "failed").
    """
    logger.info("process_document invoked (placeholder body)", extra={"document_id": document_id})
