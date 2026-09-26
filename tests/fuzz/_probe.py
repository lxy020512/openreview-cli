"""Shared probe harness for the W2 document fuzzing suites.

Private to ``tests/fuzz`` (the plan deleted a ``tests/fuzz/conftest.py``, so
nothing here is a fixture). It runs a parser entry point deterministically,
captures its Python-level output, and proves the code under test was actually
reached — the anti-vacuity requirement of plan section 9.3, rule 2.

Two reachability signals are recorded:

* ``route_calls`` counts entry into ``parsing.stream._parser_for``, the shared
  validate-and-route function every document entry point goes through.
* ``pdf_open_calls`` / ``docx_open_calls`` count entry into the underlying
  libraries (``pymupdf.open`` / ``docx.Document``), i.e. the actual parse
  boundary. A case "rejected before the parse boundary" (a 0-byte file, an
  unsupported extension) has ``route_calls >= 1`` but ``*_open_calls == 0``.
"""

from __future__ import annotations

import io
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import docx
import pymupdf

from openreview_cli.parsing import stream as _stream
from openreview_cli.parsing.models import ParseError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# A case that runs longer than this wall-clock bound is a hang, not a rejection.
PER_CASE_TIMEOUT_SECONDS = 30.0
TRACEBACK_MARKER = "Traceback (most recent call last)"

# The only categories the document parsers may raise (plan section W2).
VALID_CATEGORIES = frozenset(
    {"file_not_found", "unsupported_format", "empty", "corrupt", "password_protected", "no_text"}
)

EntryPoint = str  # one of: "stream_clauses", "parse_document", "_parser_for"


@dataclass
class Counters:
    """Reachability counters populated by :func:`instrument_entries`."""

    route_calls: int = 0
    pdf_open_calls: int = 0
    docx_open_calls: int = 0


@dataclass
class Outcome:
    """The observed result of running one parser entry point on one input."""

    ok: bool
    category: str | None
    error: BaseException | None
    stdout: str
    stderr: str
    elapsed: float
    clause_count: int = 0


@contextmanager
def instrument_entries() -> Iterator[Counters]:
    """Count entry into the routing function and the two document libraries."""
    counters = Counters()
    stream_module: Any = _stream
    pdf_module: Any = pymupdf
    docx_module: Any = docx
    real_route: Any = _stream._parser_for
    real_pdf_open: Any = pymupdf.open
    real_docx_document: Any = docx.Document

    def counting_route(path: str | Path) -> Any:
        counters.route_calls += 1
        return real_route(path)

    def counting_pdf_open(*args: Any, **kwargs: Any) -> Any:
        counters.pdf_open_calls += 1
        return real_pdf_open(*args, **kwargs)

    def counting_docx_document(*args: Any, **kwargs: Any) -> Any:
        counters.docx_open_calls += 1
        return real_docx_document(*args, **kwargs)

    stream_module._parser_for = counting_route
    pdf_module.open = counting_pdf_open
    docx_module.Document = counting_docx_document
    try:
        yield counters
    finally:
        stream_module._parser_for = real_route
        pdf_module.open = real_pdf_open
        docx_module.Document = real_docx_document


def run_entry(path: Path, entry: EntryPoint = "stream_clauses") -> Outcome:
    """Run *entry* on *path*, capturing stdout/stderr and the elapsed time."""
    if entry not in ("stream_clauses", "parse_document", "_parser_for"):
        raise ValueError(f"unknown entry point: {entry!r}")
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out_buf, err_buf
    started = time.perf_counter()
    ok = False
    category: str | None = None
    error: BaseException | None = None
    clause_count = 0
    try:
        if entry == "stream_clauses":
            for _clause in _stream.stream_clauses(path):
                clause_count += 1
        elif entry == "parse_document":
            _doc, clauses = _stream.parse_document(path)
            clause_count = len(clauses)
        else:
            _stream._parser_for(path)
        ok = True
    except Exception as exc:  # every exception type is classified by the oracle
        error = exc
        category = getattr(exc, "category", None)
    finally:
        elapsed = time.perf_counter() - started
        sys.stdout, sys.stderr = old_out, old_err
    return Outcome(
        ok=ok,
        category=category,
        error=error,
        stdout=out_buf.getvalue(),
        stderr=err_buf.getvalue(),
        elapsed=elapsed,
        clause_count=clause_count,
    )


def assert_categorical(outcome: Outcome) -> None:
    """Assert the outcome is a clean success or a valid ``ParseError`` category.

    Never an unhandled exception type, never a traceback on a captured surface,
    never a hang.
    """
    assert TRACEBACK_MARKER not in outcome.stdout, f"traceback on stdout: {outcome.stdout[:400]}"
    assert TRACEBACK_MARKER not in outcome.stderr, f"traceback on stderr: {outcome.stderr[:400]}"
    assert outcome.elapsed < PER_CASE_TIMEOUT_SECONDS, f"case hung: {outcome.elapsed:.1f}s"
    if outcome.ok:
        return
    assert isinstance(outcome.error, ParseError), (
        f"unhandled exception type {type(outcome.error).__name__}: {outcome.error!r}"
    )
    assert outcome.category in VALID_CATEGORIES, f"unknown category: {outcome.category!r}"


def assert_route_reached(counters: Counters) -> None:
    """Assert ``_parser_for`` (the shared routing function) actually ran."""
    assert counters.route_calls >= 1, "the parser entry point was never reached"


def assert_library_reached(counters: Counters) -> None:
    """Assert at least one document library was actually opened."""
    assert counters.pdf_open_calls + counters.docx_open_calls >= 1, (
        "the parse boundary (pymupdf.open / docx.Document) was never reached"
    )
