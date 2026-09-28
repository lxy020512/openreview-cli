"""#108: populating ``Clause.parent_id`` must reach the graph and the chunker.

The parser-side linking is covered by ``tests/integration/test_stream_clauses.py``.
These tests pin the two downstream consequences a real numbered document now
produces: ``parent_child`` edges / depth / score in the clause graph, and the
``parent_chunk_id`` / ``structural_location`` ripple in the chunker. Unnumbered
documents must stay flat, so the flat fixture is pinned as the negative control.
"""

from pathlib import Path

import pytest

from openreview_cli.chunking.models import Chunk
from openreview_cli.chunking.stream import stream_chunks
from openreview_cli.parsing.stream import parse_document
from openreview_cli.tui.domain.graph import graph_summary_via_tui

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PDF = FIXTURES / "pdf"
DOCX = FIXTURES / "docx"

pytestmark = pytest.mark.integration


def test_numbered_pdf_builds_a_real_hierarchy() -> None:
    summary = graph_summary_via_tui(PDF / "simple_contract.pdf")

    assert summary.node_count == 11
    assert summary.edge_count == 8
    assert summary.parent_child_edge_count == 8
    assert summary.metrics.max_depth == 2
    assert summary.score == 89


def test_numbered_docx_builds_the_same_hierarchy() -> None:
    summary = graph_summary_via_tui(DOCX / "simple_contract.docx")

    assert summary.parent_child_edge_count == 8
    assert summary.metrics.max_depth == 2
    assert summary.score == 89


def test_unnumbered_document_stays_flat() -> None:
    summary = graph_summary_via_tui(PDF / "flat_document.pdf")

    assert summary.parent_child_edge_count == 0
    assert summary.metrics.max_depth == 1
    assert summary.score == 98


def _first_chunk_by_clause(chunks: list[Chunk]) -> dict[str, Chunk]:
    first: dict[str, Chunk] = {}
    for chunk in chunks:
        first.setdefault(chunk.source_clause_id, chunk)
    return first


def test_section_chunk_points_at_its_article_chunk() -> None:
    _doc, clauses = parse_document(PDF / "simple_contract.pdf")
    first = _first_chunk_by_clause(list(stream_chunks(clauses, show_progress=False)))

    article = first["clause-0"]
    section = first["clause-1"]

    assert article.parent_chunk_id is None
    assert section.parent_chunk_id == article.id
    assert section.structural_location == "clause-0/clause-1"


def test_flat_document_chunks_have_no_parent_chunk() -> None:
    _doc, clauses = parse_document(PDF / "flat_document.pdf")
    chunks = list(stream_chunks(clauses, show_progress=False))

    assert all(chunk.parent_chunk_id is None for chunk in chunks)
    assert all("/" not in (chunk.structural_location or "") for chunk in chunks)
