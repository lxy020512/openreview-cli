"""Hostile document corpus generators (PDF and DOCX).

Owned by W2 (fuzzing, document parsing). W0 created every function stubbed to
raise ``NotImplementedError`` so a workstream that starts early fails loudly
instead of silently sharing the file; W2 fills the bodies.
"""

from __future__ import annotations

from pathlib import Path


def zero_byte(tmp_path: Path) -> Path:
    """A 0-byte file presented as a document."""
    raise NotImplementedError("owned by W2")


def truncated_pdf(tmp_path: Path) -> Path:
    """A real PDF with its tail removed at an offset other than the existing fixture's."""
    raise NotImplementedError("owned by W2")


def pdf_with_trailing_junk(tmp_path: Path) -> Path:
    """A valid PDF with well over 10 bytes of junk after ``%%EOF``."""
    raise NotImplementedError("owned by W2")


def renamed_docx(tmp_path: Path) -> Path:
    """A DOCX renamed to ``.pdf``."""
    raise NotImplementedError("owned by W2")


def no_text_pdf(tmp_path: Path) -> Path:
    """A PDF with no extractable text layer."""
    raise NotImplementedError("owned by W2")


def zip_bomb_docx(tmp_path: Path) -> Path:
    """A DOCX whose zip entries inflate far past any sane decompression ratio."""
    raise NotImplementedError("owned by W2")


def empty_dir_as_pdf(tmp_path: Path) -> Path:
    """A directory named ``*.pdf`` (reaches the parser's unguarded ``open``)."""
    raise NotImplementedError("owned by W2")


def unreadable_pdf(tmp_path: Path) -> Path:
    """A ``*.pdf`` whose permissions deny reads (``PermissionError``)."""
    raise NotImplementedError("owned by W2")


def huge_page_count_pdf(tmp_path: Path) -> Path:
    """A PDF claiming an enormous page count."""
    raise NotImplementedError("owned by W2")
