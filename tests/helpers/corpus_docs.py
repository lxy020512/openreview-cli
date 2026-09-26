"""Hostile document corpus generators (PDF and DOCX).

Owned by W2 (fuzzing, document parsing). W0 created every function stubbed to
raise ``NotImplementedError`` so a workstream that starts early fails loudly
instead of silently sharing the file; W2 fills the bodies.

Every generator writes its file into *tmp_path* and returns the path. The
inputs are deliberately small and deterministic. Existing tracked fixtures
under ``tests/fixtures`` are reused (never regenerated or duplicated); see
plan section 2.8 for the fixture inventory.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

# A zip bomb is capped well below the plan's memory ceiling: the compressed
# ``word/document.xml`` is a few KB on disk and inflates to 4 MB (a ~800x
# ratio), far past any sane decompression ratio while staying disk- and
# memory-safe (plan section W2: "under ~1 MB ... at most ~32 MB").
_ZIP_BOMB_UNCOMPRESSED_CHARS = 4_000_000

_CONTRACT_SENTENCE = "The Receiving Party shall hold the Confidential Information in confidence. "

# The DOCX package parts python-docx needs to open a minimal document.
_DOCX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd'
    '.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_DOCX_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# A syntactically minimal PDF whose page tree *claims* an enormous page count.
# The EOF gate accepts it (it ends with ``%%EOF``), then the page iterator is
# asked for a hundred million pages.
_HUGE_PAGE_COUNT_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 100000000/Kids[3 0 R]>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>endobj\n"
    b"4 0 obj<</Length 44>>stream\n"
    b"BT /F1 12 Tf 72 720 Td (Hello) Tj ET\n"
    b"endstream\n"
    b"endobj\n"
    b"trailer<</Root 1 0 R>>\n"
    b"%%EOF\n"
)


def _valid_pdf_bytes(lines: list[str] | None = None) -> bytes:
    """Return the bytes of a small, valid, single-page text PDF."""
    import pymupdf

    doc: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    page = doc.new_page()
    y = 50.0
    for line in lines or [
        "Article I: Definitions",
        "Section 1.1: Confidential Information",
        "The Receiving Party shall hold the Confidential Information in confidence.",
    ]:
        page.insert_text((50, y), line, fontname="helv", fontsize=10)
        y += 15
    data: bytes = doc.tobytes()
    doc.close()
    return data


def zero_byte(tmp_path: Path) -> Path:
    """A 0-byte file presented as a document."""
    path = tmp_path / "zero_byte.pdf"
    path.write_bytes(b"")
    return path


def truncated_pdf(tmp_path: Path) -> Path:
    """A real PDF with its tail removed at an offset other than the existing fixture's.

    The tracked ``corrupt.pdf`` fixture drops the last 512 bytes; this drops the
    last 100, so it is a distinct truncation offset that still removes ``%%EOF``.
    """
    path = tmp_path / "truncated.pdf"
    path.write_bytes(_valid_pdf_bytes()[:-100])
    return path


def pdf_with_trailing_junk(tmp_path: Path) -> Path:
    """A valid PDF with well over 10 bytes of junk after ``%%EOF``."""
    path = tmp_path / "trailing_junk.pdf"
    path.write_bytes(_valid_pdf_bytes() + b"\n" + b"trailing junk past the ten byte eof scan")
    return path


def renamed_docx(tmp_path: Path) -> Path:
    """A DOCX renamed to ``.pdf`` (reuses the tracked DOCX fixture bytes)."""
    source = _FIXTURES_DIR / "docx" / "simple_contract.docx"
    path = tmp_path / "renamed.pdf"
    path.write_bytes(source.read_bytes())
    return path


def no_text_pdf(tmp_path: Path) -> Path:
    """A PDF with no extractable text layer (a vector-only page)."""
    import pymupdf

    path = tmp_path / "no_text.pdf"
    doc: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    page = doc.new_page()
    rect = pymupdf.Rect(50, 50, 300, 300)  # type: ignore[no-untyped-call]
    page.draw_rect(rect, color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
    doc.save(str(path))
    doc.close()
    return path


def zip_bomb_docx(tmp_path: Path) -> Path:
    """A DOCX whose zip entries inflate far past any sane decompression ratio."""
    path = tmp_path / "zip_bomb.docx"
    body = "A" * _ZIP_BOMB_UNCOMPRESSED_CHARS
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_WORD_NS}"><w:body>'
        f"<w:p><w:r><w:t>{body}</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        archive.writestr("_rels/.rels", _DOCX_RELS)
        archive.writestr("word/document.xml", document_xml)
    return path


def empty_dir_as_pdf(tmp_path: Path) -> Path:
    """A directory named ``*.pdf`` (reaches the parser's unguarded ``open``)."""
    path = tmp_path / "directory.pdf"
    path.mkdir()
    return path


def unreadable_pdf(tmp_path: Path) -> Path:
    """A ``*.pdf`` whose permissions deny reads (``PermissionError``)."""
    path = tmp_path / "unreadable.pdf"
    path.write_bytes(_valid_pdf_bytes())
    path.chmod(0o000)
    return path


def huge_page_count_pdf(tmp_path: Path) -> Path:
    """A PDF claiming an enormous page count."""
    path = tmp_path / "huge_page_count.pdf"
    path.write_bytes(_HUGE_PAGE_COUNT_PDF)
    return path


def large_text_pdf(tmp_path: Path) -> Path:
    """A large but well-formed PDF (~1.6 MB of extractable text across 400 pages).

    Used only by the standalone ``memory`` structural streaming case.
    """
    import pymupdf

    path = tmp_path / "large_text.pdf"
    text = (_CONTRACT_SENTENCE * 50)[:4000]
    doc: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    rect = pymupdf.Rect(36, 36, 576, 756)  # type: ignore[no-untyped-call]
    for _ in range(400):
        page = doc.new_page()
        page.insert_textbox(rect, text, fontname="helv", fontsize=6)
    doc.save(str(path))
    doc.close()
    return path


def corrupt_docx(tmp_path: Path) -> Path:
    """Bytes that are not a zip archive, presented as a ``.docx``."""
    path = tmp_path / "corrupt.docx"
    path.write_bytes(b"this is not a zip archive, not an OOXML package at all")
    return path


def truncated_docx(tmp_path: Path) -> Path:
    """A real DOCX truncated part way through (reuses the tracked fixture bytes)."""
    source = _FIXTURES_DIR / "docx" / "simple_contract.docx"
    data = source.read_bytes()
    path = tmp_path / "truncated.docx"
    path.write_bytes(data[: len(data) * 2 // 5])
    return path


def pdf_renamed_as_docx(tmp_path: Path) -> Path:
    """A PDF renamed to ``.docx`` (reaches ``python-docx``'s own open)."""
    path = tmp_path / "pdf_as.docx"
    path.write_bytes(_valid_pdf_bytes())
    return path
