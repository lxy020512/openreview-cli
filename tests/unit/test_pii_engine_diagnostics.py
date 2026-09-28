"""#116: PII failure diagnostics — the cause survives and the phase is honest."""

from __future__ import annotations

from typing import Any

import pytest
from _pytest.monkeypatch import MonkeyPatch

from openreview_cli.parsing.models import Clause
from openreview_cli.pii.engine import PiiEngine
from openreview_cli.pii.models import PartialProcessingError, PiiError


def _clause(n: int, *, non_english: bool = False) -> Clause:
    return Clause(
        id=f"c{n}",
        title=f"Clause {n}",
        text="Text of clause, contact john@example.com.",
        level=1,
        parent_id=None,
        source_page=n,
        source_paragraph=None,
        source_span=None,
        is_non_english=non_english,
    )


def test_partial_processing_error_chains_the_first_failure(monkeypatch: MonkeyPatch) -> None:
    engine = PiiEngine(threshold=0.7)
    sentinel = RuntimeError("presidio boom")

    def _boom(text: str, **kwargs: Any) -> list[Any]:
        raise sentinel

    monkeypatch.setattr(engine, "detect_on_page", _boom)

    with pytest.raises(PartialProcessingError) as exc_info:
        engine.detect_all_pages([_clause(1)])

    assert exc_info.value.__cause__ is sentinel


def test_partial_processing_error_str_carries_no_failure_text(monkeypatch: MonkeyPatch) -> None:
    engine = PiiEngine(threshold=0.7)
    secret = "SECRET-CONTRACT-CANARY"

    def _boom(text: str, **kwargs: Any) -> list[Any]:
        raise RuntimeError(secret)

    monkeypatch.setattr(engine, "detect_on_page", _boom)

    with pytest.raises(PartialProcessingError) as exc_info:
        engine.detect_all_pages([_clause(1), _clause(2)])

    message = str(exc_info.value)
    assert "page" in message
    assert secret not in message
    assert secret not in repr(exc_info.value)


class _PatternRecognizerBoom:
    """Analyzer double that delegates to a real pattern recognizer that raises."""

    def __call__(self) -> _PatternRecognizerBoom:
        return self

    def analyze(self, **kwargs: Any) -> list[Any]:
        from presidio_analyzer import Pattern, PatternRecognizer

        class _Boom(PatternRecognizer):
            def __init__(self) -> None:
                super().__init__(supported_entity="TAX_ID", patterns=[Pattern("boom", r"\d+", 1.0)])

            def analyze(self, *args: Any, **kwargs: Any) -> list[Any]:
                raise RuntimeError("pattern recognizer boom")

        return _Boom().analyze(text=kwargs["text"], entities=kwargs["entities"])


class _NlpEngineBoom:
    """Analyzer double whose failure carries no pattern-recognizer identity."""

    def __call__(self) -> _NlpEngineBoom:
        return self

    def analyze(self, **kwargs: Any) -> list[Any]:
        raise RuntimeError("nlp engine boom")


def test_pattern_recognizer_failure_reports_regex_phase(monkeypatch: MonkeyPatch) -> None:
    engine = PiiEngine(threshold=0.7)
    monkeypatch.setattr(engine, "_ensure_analyzer", _PatternRecognizerBoom())

    with pytest.raises(PiiError) as exc_info:
        engine.detect_on_page("Tax ID is 11-7654320")

    assert exc_info.value.phase == "regex phase"


def test_nlp_engine_failure_reports_ner_phase(monkeypatch: MonkeyPatch) -> None:
    engine = PiiEngine(threshold=0.7)
    monkeypatch.setattr(engine, "_ensure_analyzer", _NlpEngineBoom())

    with pytest.raises(PiiError) as exc_info:
        engine.detect_on_page("John Smith works at Acme Corp.")

    assert exc_info.value.phase == "NER phase"


def test_non_english_failure_keeps_the_language_based_label(monkeypatch: MonkeyPatch) -> None:
    engine = PiiEngine(threshold=0.7)
    monkeypatch.setattr(engine, "_ensure_analyzer", _NlpEngineBoom())

    with pytest.raises(PiiError) as exc_info:
        engine.detect_on_page("Contact 555-9999.", is_non_english=True)

    assert exc_info.value.phase == "regex phase"


class _EmailValidatorBoom:
    """Analyzer double that runs Presidio's real email recognizer with tldextract disabled."""

    def __call__(self) -> _EmailValidatorBoom:
        return self

    def analyze(self, **kwargs: Any) -> list[Any]:
        from presidio_analyzer.predefined_recognizers.generic.email_recognizer import (
            EmailRecognizer,
        )

        return EmailRecognizer().analyze(text=kwargs["text"], entities=kwargs["entities"])


def test_tldextract_email_validation_failure_reports_regex_phase(
    monkeypatch: MonkeyPatch,
) -> None:
    import tldextract

    def _no_extract(pattern_text: str) -> Any:
        raise RuntimeError("socket blocked")

    monkeypatch.setattr(tldextract, "extract", _no_extract)

    engine = PiiEngine(threshold=0.7)
    monkeypatch.setattr(engine, "_ensure_analyzer", _EmailValidatorBoom())

    with pytest.raises(PiiError) as exc_info:
        engine.detect_on_page("Email: a@b.com")

    assert exc_info.value.phase == "regex phase"
