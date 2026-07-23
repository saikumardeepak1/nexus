"""Unit tests for app.services.chunking_service. No database needed -- this
service is pure (raw bytes in, chunks out), per docs/TDD.md section 3.2.

Fixtures used:
- tests/fixtures/multi_page.pdf: a real 3-page PDF (generated once with
  reportlab, see the generator script referenced in the PR description) with
  distinct, known text on each page.
- tests/fixtures/plain_text.txt: a real multi-paragraph plain text file.
"""

from pathlib import Path

import pytest

from app.services import chunking_service
from app.services.chunking_service import (
    Chunk,
    EmptyDocumentError,
    UnparsableDocumentError,
    UnsupportedContentTypeError,
    chunk_document,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


# --- PDF fixture ---------------------------------------------------------


def test_pdf_chunks_have_correct_page_numbers_in_order() -> None:
    content = _read_fixture("multi_page.pdf")
    chunks = chunk_document(content, "application/pdf")

    assert len(chunks) > 0
    # Page numbers must be present, 1-indexed, and non-decreasing across the
    # document (chunking never merges text across a page boundary).
    page_numbers = [c.page_number for c in chunks]
    assert all(p is not None for p in page_numbers)
    assert page_numbers == sorted(page_numbers)  # type: ignore[type-var]
    assert set(page_numbers) == {1, 2, 3}
    assert page_numbers[0] == 1
    assert page_numbers[-1] == 3


def test_pdf_chunk_index_is_sequential_from_zero() -> None:
    content = _read_fixture("multi_page.pdf")
    chunks = chunk_document(content, "application/pdf")

    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_pdf_extraction_is_sane_not_garbled() -> None:
    """Eyeball-level sanity check: real page content, not mojibake or
    binary noise, and text lands on the page it actually came from.
    """
    content = _read_fixture("multi_page.pdf")
    chunks = chunk_document(content, "application/pdf")

    page_1_text = " ".join(c.content for c in chunks if c.page_number == 1)
    page_2_text = " ".join(c.content for c in chunks if c.page_number == 2)
    page_3_text = " ".join(c.content for c in chunks if c.page_number == 3)

    assert "Remote Work Policy" in page_1_text
    assert "remotely up to three days per week" in page_1_text
    assert "Expense Reimbursement" in page_2_text
    assert "twenty five dollars" in page_2_text
    assert "Time Off" in page_3_text
    assert "accrue paid time off" in page_3_text

    # Print for real, manual eyeballing per the task's verification step.
    print("\n--- sample extracted chunks (real PDF fixture) ---")
    for c in chunks:
        print(f"[chunk_index={c.chunk_index} page={c.page_number}] {c.content!r}")


# --- Plain text fixture ---------------------------------------------------


def test_plain_text_chunks_have_page_number_none() -> None:
    content = _read_fixture("plain_text.txt")
    chunks = chunk_document(content, "text/plain")

    assert len(chunks) > 0
    assert all(c.page_number is None for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_plain_text_extraction_is_sane() -> None:
    content = _read_fixture("plain_text.txt")
    chunks = chunk_document(content, "text/plain")
    full_text = " ".join(c.content for c in chunks)

    assert "Nexus Onboarding Guide" in full_text
    assert "inline citation back to the specific chunk" in full_text


def test_content_type_accepts_bare_extension_and_filename_fallback() -> None:
    content = _read_fixture("plain_text.txt")
    by_extension = chunk_document(content, "txt")
    by_filename_fallback = chunk_document(content, "", filename="notes.txt")
    by_mime = chunk_document(content, "text/plain")

    assert by_extension == by_mime
    assert by_filename_fallback == by_mime


# --- Overlap and sizing ----------------------------------------------------


def _shared_prefix_suffix(first: str, second: str) -> str:
    """The longest prefix of `second` that is also a suffix of `first` --
    i.e. the literal overlap text the packing algorithm seeded `second`
    with. Longer candidate lengths are tried first so the full overlap is
    returned, not just the shortest match.
    """
    for length in range(min(len(first), len(second)), 0, -1):
        candidate = second[:length]
        if first.endswith(candidate):
            return candidate
    return ""


def test_overlap_actually_overlaps_between_consecutive_chunks() -> None:
    content = _read_fixture("multi_page.pdf")
    # Small chunk_size relative to page length forces multiple chunks per
    # page, which is what we need to exercise the overlap behavior.
    chunks = chunk_document(content, "application/pdf", chunk_size=200, chunk_overlap=60)

    same_page_pairs = [
        (a, b)
        for a, b in zip(chunks, chunks[1:], strict=False)
        if a.page_number == b.page_number
    ]
    assert len(same_page_pairs) > 0

    for first, second in same_page_pairs:
        overlap = _shared_prefix_suffix(first.content, second.content)
        assert overlap, (
            f"expected literal shared text between consecutive chunks, got "
            f"first={first.content!r} second={second.content!r}"
        )
        # The shared text is real overlap content, not just a joining space.
        assert len(overlap.split()) >= 1


def test_chunk_size_respects_configured_limit() -> None:
    content = _read_fixture("multi_page.pdf")
    chunk_size = 150
    chunks = chunk_document(content, "application/pdf", chunk_size=chunk_size, chunk_overlap=20)

    assert len(chunks) > 1
    for c in chunks:
        assert len(c.content) <= chunk_size, f"chunk exceeded chunk_size: {c.content!r}"


def test_chunk_size_respects_limit_with_a_single_oversized_word() -> None:
    # A single "word" longer than chunk_size (e.g. a URL or long token) has
    # no word boundary to split on; it must still be hard-split so no chunk
    # ever exceeds chunk_size, and chunking must terminate rather than hang.
    long_word = "a" * 500
    text = f"Short lead in. {long_word} Short trailer sentence here."
    chunks = chunk_document(text.encode(), "text/plain", chunk_size=50, chunk_overlap=10)

    assert len(chunks) > 1
    for c in chunks:
        assert len(c.content) <= 50


def test_larger_chunk_size_produces_fewer_chunks() -> None:
    content = _read_fixture("multi_page.pdf")
    small = chunk_document(content, "application/pdf", chunk_size=150, chunk_overlap=20)
    large = chunk_document(content, "application/pdf", chunk_size=1500, chunk_overlap=100)

    assert len(large) < len(small)


def test_default_chunk_size_and_overlap_come_from_settings() -> None:
    from app.core.config import settings

    content = _read_fixture("plain_text.txt")
    default_chunks = chunk_document(content, "text/plain")
    explicit_chunks = chunk_document(
        content, "text/plain", chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap
    )

    assert default_chunks == explicit_chunks


def test_invalid_overlap_configuration_raises_value_error() -> None:
    content = _read_fixture("plain_text.txt")
    with pytest.raises(ValueError):
        chunk_document(content, "text/plain", chunk_size=100, chunk_overlap=100)
    with pytest.raises(ValueError):
        chunk_document(content, "text/plain", chunk_size=100, chunk_overlap=150)
    with pytest.raises(ValueError):
        chunk_document(content, "text/plain", chunk_size=0)


# --- Error handling ---------------------------------------------------------


def test_unsupported_content_type_raises_typed_exception() -> None:
    with pytest.raises(UnsupportedContentTypeError):
        chunk_document(b"whatever", "application/msword")


def test_unparsable_pdf_bytes_raise_typed_exception_not_raw_traceback() -> None:
    with pytest.raises(UnparsableDocumentError):
        chunk_document(b"this is not a real pdf file at all", "application/pdf")


def test_empty_text_document_raises_typed_exception() -> None:
    with pytest.raises(EmptyDocumentError):
        chunk_document(b"", "text/plain")


def test_whitespace_only_text_document_raises_typed_exception() -> None:
    with pytest.raises(EmptyDocumentError):
        chunk_document(b"   \n\n   \n  ", "text/plain")


def test_pdf_with_no_text_layer_raises_typed_exception() -> None:
    """A structurally valid PDF (real pypdf-writable page) with no
    extractable text -- e.g. a scanned/image-only page -- must raise
    EmptyDocumentError, not silently produce zero chunks or crash.
    """
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)

    with pytest.raises(EmptyDocumentError):
        chunk_document(buffer.getvalue(), "application/pdf")


def test_chunk_is_frozen_dataclass_value_object() -> None:
    chunk = Chunk(content="hello", chunk_index=0, page_number=1)
    assert chunk.content == "hello"
    with pytest.raises(AttributeError):
        chunk.content = "changed"  # type: ignore[misc]


def test_chunking_error_hierarchy() -> None:
    assert issubclass(UnsupportedContentTypeError, chunking_service.ChunkingError)
    assert issubclass(UnparsableDocumentError, chunking_service.ChunkingError)
    assert issubclass(EmptyDocumentError, chunking_service.ChunkingError)
