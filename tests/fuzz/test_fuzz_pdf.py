"""W2 fuzz suite: the PDF ingestion boundary.

Every case feeds a hostile or malformed input to a real parser entry point and
asserts the outcome is EXACTLY one of: a ``ParseError`` whose category is one of
``file_not_found`` / ``unsupported_format`` / ``empty`` / ``corrupt`` /
``password_protected`` / ``no_text``, or a clean success. Never an unhandled
exception, never a traceback, never a hang. Each case also proves the code under
test was actually reached (plan section 9.3, rule 2).

Existing tracked fixtures are reused, not regenerated (plan section 2.8).
``tests/integration/test_error_handling.py:11-95`` already asserts exit 8 for
corrupt / empty / unsupported / missing / no-text; this suite adds the
genuinely new hostile inputs (a directory named ``*.pdf``, an unreadable file,
trailing junk past ``%%EOF``, a renamed DOCX, a huge claimed page count, a
vector-only no-text page, garbage that passes the EOF gate) rather than
re-asserting those.

Known-broken behaviour (plan section 3 sharp edges 1-4) is asserted as the
CORRECT behaviour and carries ``xfail(strict=True)`` tied to a register row.
"""

from __future__ import annotations

import os
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
PDF_FIXTURES = FIXTURES / "pdf"

pytestmark = pytest.mark.fuzz

# Deterministic profile (plan section 13, "Hypothesis determinism"): a fixed
# seed and a low example budget, so a failure is reproducible rather than flaky.
settings.register_profile(
    "w2_fuzz",
    max_examples=25,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
settings.load_profile("w2_fuzz")

_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

# Arbitrary bytes, plus bytes shaped to pass the EOF gate so ``pymupdf`` (the
# parse boundary) is actually exercised, not just the pre-checks.
_PDF_BYTES = st.one_of(
    st.binary(max_size=512),
    st.binary(max_size=512).map(lambda b: b + b"%%EOF\n"),
    st.binary(max_size=300).map(lambda b: b"%PDF-1.4\n" + b + b"\n%%EOF\n"),
    st.binary(min_size=10, max_size=512).map(lambda b: b + b"%%EOF"),
)


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


@pytest.mark.parametrize(
    ("build", "expected_category", "library"),
    [
        pytest.param(corpus_docs.zero_byte, "empty", False, id="zero-byte"),
        pytest.param(corpus_docs.truncated_pdf, "corrupt", False, id="truncated-pdf"),
        pytest.param(corpus_docs.renamed_docx, "corrupt", False, id="renamed-docx"),
        pytest.param(corpus_docs.no_text_pdf, "no_text", True, id="no-text-pdf"),
    ],
)
def test_pdf_corpus_rejects_with_a_known_category(
    build: Callable[[Path], Path],
    expected_category: str,
    library: bool,
    tmp_path: Path,
) -> None:
    outcome, counters = _run(build, tmp_path)
    _probe.assert_route_reached(counters)
    _probe.assert_categorical(outcome)
    assert outcome.category == expected_category
    if library:
        _probe.assert_library_reached(counters)


def test_pdf_garbage_passing_the_eof_gate_reaches_pymupdf(tmp_path: Path) -> None:
    """Bytes that are not a PDF but end in ``%%EOF`` must reach the parser."""
    path = tmp_path / "garbage.pdf"
    path.write_bytes(b"NOT A PDF AT ALL just text." * 3 + b"%%EOF\n")
    with _probe.instrument_entries() as counters:
        outcome = _probe.run_entry(path)
    _probe.assert_route_reached(counters)
    _probe.assert_categorical(outcome)
    assert outcome.category == "corrupt"
    _probe.assert_library_reached(counters)


def test_pdf_valid_document_succeeds_and_reaches_pymupdf(tmp_path: Path) -> None:
    """Reuses the tracked valid fixture rather than regenerating one."""
    path = PDF_FIXTURES / "simple_contract.pdf"
    assert path.exists(), "tracked PDF fixture is missing"
    with _probe.instrument_entries() as counters:
        outcome = _probe.run_entry(path)
    _probe.assert_route_reached(counters)
    _probe.assert_categorical(outcome)
    assert outcome.ok, f"a valid PDF must parse, got {outcome.error!r}"
    assert outcome.clause_count > 0
    _probe.assert_library_reached(counters)


# ── Sharp edges 1, 2 and 4: assert the CORRECT behaviour, xfail until fixed ──


@pytest.mark.xfail(
    strict=True,
    reason="RT-010: a directory named *.pdf escapes as an uncaught IsADirectoryError",
)
def test_pdf_directory_named_pdf_is_a_clean_parse_error(tmp_path: Path) -> None:
    outcome, counters = _run(corpus_docs.empty_dir_as_pdf, tmp_path)
    _probe.assert_route_reached(counters)
    _probe.assert_categorical(outcome)


@pytest.mark.skipif(_IS_ROOT, reason="directory permissions do not deny reads to root")
@pytest.mark.xfail(
    strict=True,
    reason="RT-010: an unreadable *.pdf escapes as an uncaught PermissionError",
)
def test_pdf_unreadable_is_a_clean_parse_error(tmp_path: Path) -> None:
    outcome, counters = _run(corpus_docs.unreadable_pdf, tmp_path)
    _probe.assert_route_reached(counters)
    _probe.assert_categorical(outcome)


@pytest.mark.xfail(
    strict=True,
    reason="RT-011: a valid PDF with more than 10 bytes of trailing junk is rejected as corrupt",
)
def test_pdf_trailing_junk_after_eof_is_accepted(tmp_path: Path) -> None:
    outcome, counters = _run(corpus_docs.pdf_with_trailing_junk, tmp_path)
    _probe.assert_route_reached(counters)
    _probe.assert_categorical(outcome)
    assert outcome.ok, "a valid PDF with trailing junk must parse, not be rejected as corrupt"
    _probe.assert_library_reached(counters)


@pytest.mark.xfail(
    strict=True,
    reason="RT-013: an enormous claimed page count escapes as an uncaught RuntimeError",
)
def test_pdf_huge_page_count_is_a_clean_parse_error(tmp_path: Path) -> None:
    outcome, counters = _run(corpus_docs.huge_page_count_pdf, tmp_path)
    _probe.assert_route_reached(counters)
    _probe.assert_categorical(outcome)


# ── Property-based fuzzing: one bytes strategy per parser entry point ───────


def _assert_pdf_property(entry: str, data: bytes, tmp_path: Path) -> None:
    path = tmp_path / "fuzz.pdf"
    path.write_bytes(data)
    with _probe.instrument_entries() as counters:
        outcome = _probe.run_entry(path, entry)
    _probe.assert_route_reached(counters)
    _probe.assert_categorical(outcome)


@given(data=_PDF_BYTES)
def test_property_stream_clauses_never_raises_unhandled(data: bytes, tmp_path: Path) -> None:
    _assert_pdf_property("stream_clauses", data, tmp_path)


@given(data=_PDF_BYTES)
def test_property_parse_document_never_raises_unhandled(data: bytes, tmp_path: Path) -> None:
    _assert_pdf_property("parse_document", data, tmp_path)


@given(data=_PDF_BYTES)
def test_property_parser_for_never_raises_unhandled(data: bytes, tmp_path: Path) -> None:
    _assert_pdf_property("_parser_for", data, tmp_path)


# ── Negative control: the oracle must be able to fail ───────────────────────


def test_negative_control_oracle_rejects_a_broken_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid document fed to a deliberately broken parser MUST be caught.

    The oracle cannot certify a parser it cannot fail: this replaces the shared
    routing function with a stub that raises a raw ``RuntimeError`` (exactly the
    failure the categorical predicate exists to catch) and asserts the predicate
    rejects the resulting outcome. The ``pytest.raises`` below is the assertion
    that the oracle is not vacuous.
    """
    valid = tmp_path / "valid.pdf"
    valid.write_bytes((PDF_FIXTURES / "simple_contract.pdf").read_bytes())

    def broken_route(_path: object) -> None:
        raise RuntimeError("deliberately broken parser stub")

    monkeypatch.setattr("openreview_cli.parsing.stream._parser_for", broken_route)

    outcome = _probe.run_entry(valid)
    assert not outcome.ok
    with pytest.raises(AssertionError):
        _probe.assert_categorical(outcome)
