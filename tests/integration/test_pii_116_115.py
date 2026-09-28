"""#116/#115: engine-level regression tests (real Presidio analyzer + shared spaCy model)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from openreview_cli.parsing.models import Clause, Document
from openreview_cli.pii.engine import PiiEngine, strip_pii

AUTO_CONTRACT = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "pii"
    / "seeded_contracts"
    / "auto"
    / "auto_contract_1.txt"
)


def _clause(text: str) -> Clause:
    return Clause(
        id="1",
        title=None,
        text=text,
        level=1,
        parent_id=None,
        source_page=1,
        source_paragraph=None,
        source_span=None,
    )


def _doc() -> Document:
    return Document(
        source_path=Path("/tmp/test.pdf"),
        format="pdf",
        page_count=1,
        clause_count=1,
        parse_duration_seconds=0.1,
        warnings=[],
    )


@pytest.mark.integration
def test_engine_pins_the_bundled_suffix_snapshot(monkeypatch: MonkeyPatch) -> None:
    """The engine must pin tldextract off the network independent of the session fixture."""
    import tldextract
    import tldextract.tldextract as tldextract_impl
    from tldextract.cache import DiskCache

    monkeypatch.setattr(tldextract_impl, "TLD_EXTRACTOR", tldextract.TLDExtract())
    assert tldextract_impl.TLD_EXTRACTOR.suffix_list_urls, "expected the network default"

    def _no_fetch(*args: object, **kwargs: object) -> str:
        raise RuntimeError("tldextract network fetch attempted")

    monkeypatch.setattr(DiskCache, "cached_fetch_url", _no_fetch)

    engine = PiiEngine(threshold=0.7)
    engine._ensure_analyzer()

    assert tldextract_impl.TLD_EXTRACTOR.suffix_list_urls == ()

    entities = engine.detect_on_page("Email: john@acme.com.")
    assert any(e.entity_type == "EMAIL_ADDRESS" for e in entities)


@pytest.mark.integration
def test_strip_pii_labels_an_overlapping_span_with_the_specific_type(
    pii_engine: PiiEngine,
) -> None:
    result = strip_pii(
        [_clause("Tax ID is 11-7654320")], _doc(), strip_metadata=False, engine=pii_engine
    )
    assert result.stripped_text == "Tax ID is [TAX_ID_1]"
    assert result.mapping == {"TAX_ID_1": "11-7654320"}


@pytest.mark.integration
def test_detect_on_page_still_reports_the_span(pii_engine: PiiEngine) -> None:
    entities = pii_engine.detect_on_page("Tax ID is 11-7654320")
    tax_ids = [e for e in entities if e.entity_type == "TAX_ID"]
    assert tax_ids, f"TAX_ID not detected: {entities}"
    assert tax_ids[0].start == 10
    assert tax_ids[0].end == 20
    assert tax_ids[0].original_value == "11-7654320"


@pytest.mark.integration
def test_seeded_contract_uses_the_specific_type_for_overlapping_values(
    pii_engine: PiiEngine,
) -> None:
    text = AUTO_CONTRACT.read_text(encoding="utf-8").strip()
    result = strip_pii([_clause(text)], _doc(), strip_metadata=False, engine=pii_engine)

    assert "Tax ID is [TAX_ID_1]." in result.stripped_text
    assert "Registration number is [REG_1]." in result.stripped_text
    assert result.mapping["TAX_ID_1"] == "11-7654320"
    assert result.mapping["REG_1"] == "REG-100001"
    assert result.mapping["PHONE_1"] == "555-0101"

    assert "[DATE_1]" not in result.stripped_text
    assert "[DATE_3]" not in result.stripped_text
    assert "[PARTY_E]" not in result.stripped_text
    assert result.stripped_text.count("[TAX_ID_1]") == 1
    assert result.stripped_text.count("[REG_1]") == 1
    assert result.stripped_text.count("[PHONE_1]") == 1
    assert not result.stripped_text.rstrip().endswith("[TAX_ID_1]")
