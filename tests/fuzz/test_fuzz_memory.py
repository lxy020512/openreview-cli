"""W2 structural streaming assertion for the large document case.

Standalone ``memory`` case (plan section W2): the document parser must stream,
so the peak memory delta of the parse call is a small fraction of the extracted
text size, not proportional to it. This deliberately does NOT use the 110 MB
constitutional floor (``memory_tracker``) and is not in the ``fast`` pool.

Run standalone::

    uv run pytest -m memory tests/fuzz -q
"""

from __future__ import annotations

import tracemalloc
from pathlib import Path

import pytest

from openreview_cli.parsing.stream import stream_clauses
from tests.fuzz import _probe
from tests.helpers import corpus_docs

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PDF_FIXTURES = FIXTURES / "pdf"

pytestmark = [pytest.mark.fuzz, pytest.mark.memory]

# The fraction of the extracted text size the parse call's peak delta may use.
_PEAK_FRACTION = 0.20


def test_large_document_parse_peak_stays_a_fraction_of_extracted_text(tmp_path: Path) -> None:
    path = corpus_docs.large_text_pdf(tmp_path)
    # Warm the lazy-loaded nupunkt model so the one-time load is not counted.
    _probe.run_entry(PDF_FIXTURES / "simple_contract.pdf")

    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    text_bytes = 0
    clause_count = 0
    with _probe.instrument_entries() as counters:
        for clause in stream_clauses(path):
            text_bytes += len(clause.text.encode("utf-8"))
            clause_count += 1
    after = tracemalloc.take_snapshot()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    _probe.assert_library_reached(counters)
    assert clause_count > 0
    assert text_bytes > 1_000_000, f"document too small to be a streaming test: {text_bytes} B"

    retained = sum(
        stat.size_diff for stat in after.compare_to(before, "filename") if stat.size_diff > 0
    )
    assert peak < _PEAK_FRACTION * text_bytes, (
        f"peak parse delta {peak} B is not under {_PEAK_FRACTION:.0%} of "
        f"extracted text {text_bytes} B (ratio {peak / text_bytes:.2f})"
    )
    assert retained < _PEAK_FRACTION * text_bytes, (
        f"retained delta {retained} B is not under {_PEAK_FRACTION:.0%} of "
        f"extracted text {text_bytes} B (ratio {retained / text_bytes:.2f})"
    )
