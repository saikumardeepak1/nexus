"""Parses raw document bytes and splits the extracted text into overlapping
chunks sized for retrieval (see docs/TDD.md section 3.2).

This module is deliberately pure: it takes raw bytes plus a content type and
returns an ordered list of ``Chunk`` value objects. It has no database or
Qdrant dependency and does not know it is being called from the ingestion
pipeline (see the ``process_document`` worker job, a separate issue), which
keeps it trivially unit-testable in isolation.

Splitting strategy
-------------------
Text is split on paragraph and sentence boundaries rather than at a hard
character cutoff, so a chunk boundary doesn't land mid-sentence and leave a
chunk unreadable on its own. Sentences are greedily packed into a chunk up
to ``chunk_size`` characters; a single sentence longer than ``chunk_size``
(rare, but real for run-on legal or technical prose) is hard-split on word
boundaries as a fallback so no chunk grows unbounded. Consecutive chunks
share their last ``chunk_overlap`` characters' worth of sentences, so a
concept split across a boundary still appears in full in at least one chunk.

For PDF input, chunking happens per page: sentences never merge across a
page boundary, so ``page_number`` on every chunk is exact rather than
approximate. Plain text has no page concept, so ``page_number`` is always
``None`` and the whole document is chunked as a single sequence of pages
containing one page kept purely as an internal implementation aid.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.core.config import settings

# Matches a run of two or more newlines (with optional whitespace on the
# blank line) -- the paragraph boundary within a page's extracted text.
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")

# Splits on sentence-ending punctuation followed by whitespace. A simple
# heuristic (misses some abbreviations like "Dr.") rather than a full NLP
# sentence tokenizer: good enough for retrieval-chunk boundaries, and avoids
# pulling in an extra dependency beyond pypdf for this issue's scope.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


class ChunkingError(Exception):
    """Base class for all errors raised by chunking_service."""


class UnsupportedContentTypeError(ChunkingError):
    """Raised when the given content type/filename isn't PDF or plain text."""


class UnparsableDocumentError(ChunkingError):
    """Raised when the file's bytes can't be parsed as the detected type
    (corrupt PDF, wrong magic bytes, encrypted with no accessible text, etc).
    """


class EmptyDocumentError(ChunkingError):
    """Raised when parsing succeeds but no extractable text is found (e.g. a
    scanned/image-only PDF with no text layer, or a zero-byte text file).
    """


@dataclass(frozen=True)
class Chunk:
    """One chunk of a document's parsed text, ready to be embedded and
    persisted by the ingestion pipeline (a separate concern from this
    module -- see app/models/chunk.py for the eventual DB row shape).
    """

    content: str
    chunk_index: int
    page_number: int | None


_PDF_CONTENT_TYPES = {"application/pdf", "pdf"}
_TEXT_CONTENT_TYPES = {"text/plain", "text", "txt"}


def _normalize_content_type(content_type: str, filename: str | None) -> str:
    """Resolve a caller-supplied MIME type (or bare extension) plus an
    optional filename into ``"pdf"`` or ``"text"``. The MIME type/extension
    takes priority; the filename extension is only consulted as a fallback
    when the content type itself is empty or unrecognized, so a caller that
    only has a filename can still get a sensible result.
    """
    candidates = [content_type.strip().lower()]
    if filename and "." in filename:
        candidates.append(filename.rsplit(".", 1)[-1].strip().lower())

    for candidate in candidates:
        if candidate in _PDF_CONTENT_TYPES:
            return "pdf"
        if candidate in _TEXT_CONTENT_TYPES:
            return "text"

    raise UnsupportedContentTypeError(
        f"Unsupported content type {content_type!r} (filename={filename!r}); "
        "expected a PDF or plain text document."
    )


def _extract_pdf_pages(content: bytes) -> list[str]:
    """Return extracted text per page, in page order (0-indexed here;
    callers convert to the 1-indexed page numbers used for citations).
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as exc:
        raise UnparsableDocumentError(f"Could not parse PDF: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - pypdf can raise several non-PdfReadError
        # exception types (e.g. a plain ValueError) on malformed input; any
        # of them means "this isn't a usable PDF", not an app crash.
        raise UnparsableDocumentError(f"Could not parse PDF: {exc}") from exc

    if not pages:
        raise UnparsableDocumentError("PDF has no pages.")

    return pages


def _split_into_sentences(text: str) -> list[str]:
    """Split a block of text into paragraph-then-sentence units, collapsing
    internal line-wrap whitespace within each paragraph. Returns a flat,
    ordered list of non-empty sentence strings.
    """
    sentences: list[str] = []
    for paragraph in _PARAGRAPH_SPLIT_RE.split(text):
        # Collapse line-wrap newlines/extra whitespace within a paragraph
        # (PDF text extraction inserts a newline at every wrapped line, not
        # just at real paragraph breaks).
        normalized = " ".join(paragraph.split())
        if not normalized:
            continue
        sentences.extend(s for s in _SENTENCE_SPLIT_RE.split(normalized) if s)
    return sentences


def _hard_split(sentence: str, max_size: int) -> list[str]:
    """Split an oversized sentence on word boundaries into pieces no larger
    than ``max_size``. Fallback path for pathological input (e.g. a run-on
    sentence, or text with no sentence-ending punctuation at all).

    A single word longer than ``max_size`` on its own (e.g. a URL or a long
    identifier with no spaces) can't be split on a word boundary at all; as
    a last resort that one word is split at the character level so no piece
    ever exceeds ``max_size``, which every caller relies on.
    """
    words = sentence.split(" ")
    pieces: list[str] = []
    current = ""
    for word in words:
        if len(word) > max_size:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(word[i : i + max_size] for i in range(0, len(word), max_size))
            continue
        candidate = f"{current} {word}".strip()
        if len(candidate) > max_size and current:
            pieces.append(current)
            current = word
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def _trailing_words(text: str, max_chars: int) -> str:
    """Return the longest whole-word suffix of ``text`` that fits within
    ``max_chars`` characters. Used to build the overlap seed at a word
    boundary rather than a sentence boundary, so overlap is still produced
    even when the single sentence nearest a chunk boundary is itself larger
    than ``chunk_overlap``.
    """
    if max_chars <= 0:
        return ""
    words: list[str] = []
    length = 0
    for word in reversed(text.split()):
        added = len(word) + (1 if words else 0)
        # Unlike a typical greedy accumulator, this check applies even to
        # the very first (last-in-text) candidate word: if that one word
        # alone doesn't fit the budget, the caller's invariant that the
        # returned string never exceeds max_chars must still hold, so we
        # return an empty seed rather than force an over-budget word in.
        if length + added > max_chars:
            break
        words.insert(0, word)
        length += added
    return " ".join(words)


def _pack_sentences(
    sentences: list[str], chunk_size: int, chunk_overlap: int
) -> list[str]:
    """Greedily pack sentences into chunks up to ``chunk_size`` characters.
    Consecutive chunks share a trailing/leading overlap of up to
    ``chunk_overlap`` characters, taken at a word boundary from the end of
    the chunk just closed, so a concept split across a boundary still reads
    in full in at least one chunk.
    """
    # Expand any single sentence longer than chunk_size into hard-split
    # pieces up front, so the packing loop below never has to special-case
    # an oversized unit.
    units: list[str] = []
    for sentence in sentences:
        if len(sentence) > chunk_size:
            units.extend(_hard_split(sentence, chunk_size))
        else:
            units.append(sentence)

    chunks: list[str] = []
    current_units: list[str] = []

    def _joined_len(parts: list[str]) -> int:
        # len of " ".join(parts) without materializing the string.
        return sum(len(p) for p in parts) + max(len(parts) - 1, 0)

    i = 0
    while i < len(units):
        unit = units[i]
        candidate_len = _joined_len([*current_units, unit])

        if current_units and candidate_len > chunk_size:
            # Current chunk is full: close it out and seed the next chunk
            # with a trailing, word-bounded slice of the chunk just closed.
            # The seed is capped so it always leaves room for `unit` (which
            # is guaranteed <= chunk_size on its own by the hard-split
            # above): worst case the seed is empty, which always fits.
            closed_text = " ".join(current_units)
            room_for_seed = max(chunk_size - len(unit) - 1, 0)
            seed_budget = min(chunk_overlap, room_for_seed)
            seed_text = _trailing_words(closed_text, seed_budget)
            chunks.append(closed_text)
            current_units = [seed_text] if seed_text else []
            continue  # retry the same unit against the freshly-seeded chunk

        current_units.append(unit)
        i += 1

    if current_units:
        chunks.append(" ".join(current_units))
    return chunks


def _chunk_page_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    sentences = _split_into_sentences(text)
    if not sentences:
        return []
    return _pack_sentences(sentences, chunk_size, chunk_overlap)


def chunk_document(
    content: bytes,
    content_type: str,
    *,
    filename: str | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> list[Chunk]:
    """Parse ``content`` (PDF or plain text bytes) and split it into
    overlapping chunks.

    Args:
        content: Raw file bytes.
        content_type: MIME type (``"application/pdf"``, ``"text/plain"``) or
            bare extension (``"pdf"``, ``"txt"``) of the document.
        filename: Optional original filename, consulted as a fallback for
            content-type detection when ``content_type`` is empty or not
            recognized.
        chunk_size: Max characters per chunk. Defaults to
            ``settings.chunk_size``.
        chunk_overlap: Characters of shared context between consecutive
            chunks. Defaults to ``settings.chunk_overlap``.

    Returns:
        Chunks in document order, with ``chunk_index`` numbered sequentially
        from 0 across the whole document.

    Raises:
        UnsupportedContentTypeError: content_type/filename isn't PDF or text.
        UnparsableDocumentError: the bytes can't be parsed as the detected type.
        EmptyDocumentError: parsing succeeded but no text was extracted.
    """
    resolved_chunk_size = chunk_size if chunk_size is not None else settings.chunk_size
    resolved_chunk_overlap = (
        chunk_overlap if chunk_overlap is not None else settings.chunk_overlap
    )
    if resolved_chunk_size <= 0:
        raise ValueError("chunk_size must be positive.")
    if resolved_chunk_overlap < 0 or resolved_chunk_overlap >= resolved_chunk_size:
        raise ValueError("chunk_overlap must be non-negative and smaller than chunk_size.")

    doc_type = _normalize_content_type(content_type, filename)

    if doc_type == "pdf":
        pages = _extract_pdf_pages(content)
        page_texts: list[tuple[str, int | None]] = [
            (text, index + 1) for index, text in enumerate(pages)
        ]
    else:
        text = content.decode("utf-8", errors="replace")
        page_texts = [(text, None)]

    chunks: list[Chunk] = []
    chunk_index = 0
    for text, page_number in page_texts:
        for chunk_text in _chunk_page_text(text, resolved_chunk_size, resolved_chunk_overlap):
            chunks.append(
                Chunk(content=chunk_text, chunk_index=chunk_index, page_number=page_number)
            )
            chunk_index += 1

    if not chunks:
        raise EmptyDocumentError("No extractable text found in document.")

    return chunks
