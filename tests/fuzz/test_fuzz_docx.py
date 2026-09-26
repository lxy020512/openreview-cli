"""W2 fuzz suite: the DOCX ingestion boundary.

Same shape as ``test_fuzz_pdf.py``: every input's outcome must be exactly one of
a ``ParseError`` with a known category or a clean success, each case proves the
parse boundary was reached, and known-broken behaviour carries
``xfail(strict=True)`` tying it to a register row.

The five tracked DOCX fixtures (plan section 2.8) are reused as the valid
inputs; the malformed inputs are generated into ``tmp_path`` by
``tests/helpers/corpus_docs.py``. No malformed DOCX fixture is committed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.fuzz import _probe
from tests.helpers import corpus_docs

if TYPE_CHECKING:
    from collections.abc import Callable

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
DOCX_FIXTURES = FIXTURES / "docx"

pytestmark = pytest.mark.fuzz

# Same deterministic profile as the PDF suite (plan section 13).
settings.register_profile(
    "w2_fuzz",
    max_examples=25,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
settings.load_profile("w2_fuzz")

_DOCX_BYTES = st.binary(max_size=1024)


def _run(
    build: Callable[[Path], Path],
    tmp_path: Path,
    entry: str = "stream_clauses",
) -> tuple[_probe.Outcome, _probe.Counters]:
    path = build(tmp_path)
    with _probe.instrument_entries() as counters:
        outcome = _probe.run_entry(path, entry)
    return outcome, counters


# ── Corpus cases: a known category, rejected cleanly ────────────────────────


def test_docx_zero_byte_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty.docx"
    path.write_bytes(b"")
    with _probe.instrument_entries() as counters:
        outcome = _probe.run_entry(path)
    _probe.assert_route_reached(counters)
    _probe.assert_categorical(outcome)
    assert outcome.category == "empty"


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(corpus_docs.corrupt_docx, id="corrupt-docx"),
        pytest.param(corpus_docs.truncated_docx, id="truncated-docx"),
        pytest.param(corpus_docs.pdf_renamed_as_docx, id="pdf-renamed-docx"),
    ],
)
def test_docx_corpus_rejects_as_corrupt_at_the_parse_boundary(
    build: Callable[[Path], Path], tmp_path: Path
) -> None:
    outcome, counters = _run(build, tmp_path)
    _probe.assert_route_reached(counters)
    _probe.assert_categorical(outcome)
    assert outcome.category == "corrupt"
    _probe.assert_library_reached(counters)


@pytest.mark.parametrize(
    "name",
    ["simple_contract.docx", "flat_document.docx", "with_headings.docx"],
)
def test_docx_valid_fixture_succeeds_and_reaches_python_docx(name: str, tmp_path: Path) -> None:
    path = DOCX_FIXTURES / name
    assert path.exists(), f"tracked DOCX fixture {name} is missing"
    with _probe.instrument_entries() as counters:
        outcome = _probe.run_entry(path)
    _probe.assert_route_reached(counters)
    _probe.assert_categorical(outcome)
    assert outcome.ok, f"a valid DOCX must parse, got {outcome.error!r}"
    assert outcome.clause_count > 0
    _probe.assert_library_reached(counters)


# ── Sharp edge 3: a zip bomb is expanded, not rejected ──────────────────────


@pytest.mark.xfail(
    strict=True,
    reason="RT-012: a DOCX zip bomb is expanded (no decompression ratio guard) instead of rejected",
)
def test_docx_zip_bomb_is_rejected(tmp_path: Path) -> None:
    outcome, counters = _run(corpus_docs.zip_bomb_docx, tmp_path)
    _probe.assert_route_reached(counters)
    _probe.assert_categorical(outcome)
    assert not outcome.ok, "a zip bomb must be rejected, not expanded and parsed"
    _probe.assert_library_reached(counters)


# ── Property-based fuzzing: one bytes strategy per parser entry point ───────


def _assert_docx_property(entry: str, data: bytes, tmp_path: Path) -> None:
    path = tmp_path / "fuzz.docx"
    path.write_bytes(data)
    with _probe.instrument_entries() as counters:
        outcome = _probe.run_entry(path, entry)
    _probe.assert_route_reached(counters)
    _probe.assert_categorical(outcome)


@given(data=_DOCX_BYTES)
def test_property_stream_clauses_never_raises_unhandled(data: bytes, tmp_path: Path) -> None:
    _assert_docx_property("stream_clauses", data, tmp_path)


@given(data=_DOCX_BYTES)
def test_property_parse_document_never_raises_unhandled(data: bytes, tmp_path: Path) -> None:
    _assert_docx_property("parse_document", data, tmp_path)


@given(data=_DOCX_BYTES)
def test_property_parser_for_never_raises_unhandled(data: bytes, tmp_path: Path) -> None:
    _assert_docx_property("_parser_for", data, tmp_path)


# ── Negative control: the oracle must be able to fail ───────────────────────


def test_negative_control_oracle_rejects_a_broken_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid document fed to a deliberately broken parser MUST be caught."""
    valid = tmp_path / "valid.docx"
    valid.write_bytes((DOCX_FIXTURES / "simple_contract.docx").read_bytes())

    def broken_route(_path: object) -> None:
        raise RuntimeError("deliberately broken parser stub")

    monkeypatch.setattr("openreview_cli.parsing.stream._parser_for", broken_route)

    outcome = _probe.run_entry(valid)
    assert not outcome.ok
    with pytest.raises(AssertionError):
        _probe.assert_categorical(outcome)
