from typing import Any

import pytest

from openreview_cli.review import extraction, qa
from openreview_cli.review.models import ClauseAssessment, Position, QAVerdict
from openreview_cli.review.playbook import load_bundled


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"position":"invented","confidence":0.8,"citation":"x","category_match":true}',
        '{"position":"no-match","confidence":0.0,"citation":"","category_match":false}',
        '{"position":"acceptable","confidence":true,"citation":"x","category_match":true}',
    ],
)
def test_strict_extraction_invalid_is_fixed_safe_error(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    category = load_bundled().categories[0]
    monkeypatch.setattr(extraction, "call_gateway_chat", lambda *a, **k: raw)
    result = extraction.extract_clause("text", "c1", category, "extraction", strict=True)
    assert result.error == "extraction_invalid_response"


def test_strict_exception_does_not_leak_provider_data(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    category = load_bundled().categories[0]

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("PRIVATE-RAW-PROVIDER-ERROR")

    monkeypatch.setattr(extraction, "call_gateway_chat", fail)
    result = extraction.extract_clause("text", "c1", category, "extraction", strict=True)
    assert result.error == "extraction_call_failed"
    monkeypatch.setattr(qa, "call_gateway_chat", fail)
    result = qa.verify_assessment(result, category, "reasoning", strict=True)
    assert result.error == "qa_call_failed"
    assert "PRIVATE-RAW" not in caplog.text


def test_strict_qa_rejects_fallback_and_invalid_boolean(monkeypatch: pytest.MonkeyPatch) -> None:
    category = load_bundled().categories[0]
    for raw in ("bad", '{"verdict":"agree","citation_valid":"true"}'):
        assessment = ClauseAssessment(
            "c1",
            "text",
            category.id,
            Position.ACCEPTABLE,
            0.8,
            "quote",
            QAVerdict.agree,
            "extraction",
            "extraction",
        )
        monkeypatch.setattr(qa, "call_gateway_chat", lambda *a, _raw=raw, **k: _raw)
        assert (
            qa.verify_assessment(assessment, category, "reasoning", strict=True).error
            == "qa_invalid_response"
        )
