"""Synthetic default-path contracts confirmed against upstream db184390e7b2.

These tests preserve successful non-resume behavior; they do not remeasure the
historical review-accuracy receipt or validate strict checkpoint responses.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, replace
from itertools import product
from types import ModuleType
from typing import Any, TypedDict
from unittest.mock import Mock

import pytest

from openreview_cli.gateway.models import CapabilityRequirement
from openreview_cli.recovery.models import RecoveryContext
from openreview_cli.review import _gateway, extraction, qa
from openreview_cli.review.models import (
    Category,
    ClauseAssessment,
    Position,
    PositionDef,
    QAVerdict,
)

CONFIDENCES = [0, 0.0, math.nextafter(0.5, 0), 0.5, math.nextafter(0.5, 1), 1, 1.0]
EXTRACTION_REPLY: dict[str, Any] = {
    "position": "acceptable",
    "confidence": 0.5,
    "citation": "引文",
    "category_match": True,
}
QA_REPLY: dict[str, Any] = {
    "verdict": "agree",
    "revised_position": None,
    "rationale": "理由",
    "citation_valid": True,
    "position_valid": True,
    "category_valid": True,
    "confidence_valid": True,
}


class DefaultOptions(TypedDict, total=False):
    strict: bool


@pytest.fixture(params=[{}, {"strict": False}], ids=["omitted-strict", "explicit-false"])
def default_options(request: pytest.FixtureRequest) -> DefaultOptions:
    return {"strict": False} if request.param else {}


@pytest.fixture
def category() -> Category:
    definition = PositionDef("synthetic", ["example"])
    return Category(
        "synthetic", "Synthetic 类别", "", definition, definition, definition, Position.ACCEPTABLE
    )


def _assessment(confidence: float = 0.5) -> ClauseAssessment:
    return ClauseAssessment(
        "synthetic-1",
        "合成 ``` clause",
        "synthetic",
        Position.ACCEPTABLE,
        confidence,
        "引文",
        QAVerdict.agree,
        "extract-slot",
        "legacy-qa-slot",
    )


def _chat(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    payload: dict[str, Any],
    fenced: bool = False,
) -> Mock:
    raw = json.dumps(payload, ensure_ascii=False)
    stub = Mock(return_value=f"```json\n{raw}\n```" if fenced else raw)
    monkeypatch.setattr(module, "call_gateway_chat", stub)
    return stub


def _assert_call(stub: Mock, slot: str, context: dict[str, Any]) -> list[dict[str, str]]:
    assert stub.call_count == 1
    args, kwargs = stub.call_args
    assert len(args) == 2 and args[0] == slot
    assert kwargs == {"requirement": CapabilityRequirement(capability="reasoning"), **context}
    for key, value in context.items():
        assert kwargs[key] is value
    assert [message["role"] for message in args[1]] == ["system", "user"]
    assert "```\n合成 ` ` ` clause\n```" in args[1][1]["content"]
    messages: list[dict[str, str]] = args[1]
    return messages


def _context() -> dict[str, Any]:
    return {
        "session_id": "receipt-session",
        "coordinator": object(),
        "recovery_ctx": object(),
        "provider_list": ["provider-a", "provider-b"],
    }


@pytest.mark.parametrize(
    "changes",
    [
        *[{"position": position.value} for position in Position],
        *[{"confidence": confidence} for confidence in CONFIDENCES],
        *[{"citation": citation} for citation in ("", "引文 😀")],
        {"category_match": True},
        {"category_match": False},
    ],
)
def test_default_extraction_success_fields_and_call(
    changes: dict[str, Any],
    category: Category,
    default_options: DefaultOptions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = EXTRACTION_REPLY | changes
    stub = _chat(monkeypatch, extraction, payload)
    context = _context()
    result = extraction.extract_clause(
        "合成 ``` clause", "synthetic-1", category, "extract-slot", **context, **default_options
    )
    expected = replace(
        _assessment(),
        position=Position(payload["position"]),
        confidence=float(payload["confidence"]),
        citation=payload["citation"],
        qa_model="extract-slot",
    )
    assert asdict(result) == asdict(expected)
    assert result.is_amber is False
    messages = _assert_call(stub, "extract-slot", context)
    assert (
        messages[0]["content"]
        == "You are a legal contract analyst. Your task is to classify a single clause from a Non-Disclosure Agreement against a 3-position playbook. Respond ONLY with valid JSON — no preamble, no explanation."
    )
    assert (
        '"position": "preferred" | "acceptable" | "walkaway" | "no-match"' in messages[1]["content"]
    )
    assert (
        "### Default position (if no specific indicators match)\nacceptable\n\n"
        in messages[1]["content"]
    )


@pytest.mark.parametrize("default", [Position.PREFERRED, Position.ACCEPTABLE, Position.WALKAWAY])
def test_legacy_no_match_uses_category_default(
    default: Position,
    category: Category,
    default_options: DefaultOptions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _chat(monkeypatch, extraction, EXTRACTION_REPLY | {"position": "no-match"})
    result = extraction.extract_clause(
        "合成 ``` clause",
        "synthetic-1",
        replace(category, default_position=default),
        "extract-slot",
        **default_options,
    )
    assert asdict(result) == asdict(
        replace(_assessment(), position=default, qa_model="extract-slot")
    )


def test_no_category_skips_gateway(
    default_options: DefaultOptions, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _chat(monkeypatch, extraction, EXTRACTION_REPLY)
    result = extraction.extract_clause(
        "合成 ``` clause", "synthetic-1", None, "extract-slot", **default_options
    )
    expected = replace(
        _assessment(),
        playbook_category="no-match",
        position=Position.UNCERTAIN,
        confidence=0.0,
        citation="",
        qa_verdict=QAVerdict.uncertain,
        qa_model="extract-slot",
    )
    assert asdict(result) == asdict(expected)
    assert result.is_amber is False
    stub.assert_not_called()


@pytest.mark.parametrize("fenced", [False, True])
def test_default_success_accepts_plain_and_fenced_json(
    fenced: bool,
    category: Category,
    default_options: DefaultOptions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _chat(monkeypatch, extraction, EXTRACTION_REPLY, fenced)
    result = extraction.extract_clause(
        "合成 ``` clause", "synthetic-1", category, "extract-slot", **default_options
    )
    assert asdict(result) == asdict(replace(_assessment(), qa_model="extract-slot"))
    _chat(monkeypatch, qa, QA_REPLY, fenced)
    assert qa.verify_assessment(result, category, "qa-slot", **default_options) is result
    assert asdict(result) == asdict(replace(_assessment(), qa_model="extract-slot"))


QA_CASES = [
    *[({"verdict": verdict.value}, 0.5) for verdict in QAVerdict],
    *[
        ({"revised_position": position}, 0.5)
        for position in [None, *[position.value for position in Position]]
    ],
    *[
        (
            {"verdict": verdict.value}
            | dict(
                zip(
                    ("citation_valid", "position_valid", "category_valid", "confidence_valid"),
                    flags,
                    strict=True,
                )
            ),
            0.5,
        )
        for verdict in QAVerdict
        for flags in product((False, True), repeat=4)
    ],
    *[({}, confidence) for confidence in CONFIDENCES],
    *[
        ({"rationale": rationale, "revised_position": "preferred"}, 0.5)
        for rationale in ("", "理由 😀")
    ],
]


@pytest.mark.parametrize(("changes", "confidence"), QA_CASES)
def test_default_qa_mutates_same_object_and_retains_legacy_model(
    changes: dict[str, Any],
    confidence: float,
    category: Category,
    default_options: DefaultOptions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = QA_REPLY | changes
    stub = _chat(monkeypatch, qa, payload)
    assessment = replace(
        _assessment(confidence),
        qa_revised_position=Position.WALKAWAY,
        qa_revised_rationale="existing",
    )
    expected = replace(assessment, qa_verdict=QAVerdict(payload["verdict"]))
    revised = payload["revised_position"]
    if revised is not None and revised != "acceptable":
        expected.qa_revised_position = Position(revised)
        expected.qa_revised_rationale = payload["rationale"]
    expected.is_amber = (
        payload["verdict"] != "agree"
        or confidence < 0.5
        or not all(payload[key] for key in ("citation_valid", "position_valid", "category_valid"))
    )
    context = _context()
    result = qa.verify_assessment(assessment, category, "qa-slot", **context, **default_options)
    assert result is assessment
    assert asdict(result) == asdict(expected)
    assert result.is_amber == expected.is_amber
    messages = _assert_call(stub, "qa-slot", context)
    assert (
        messages[0]["content"]
        == "You are a senior legal review QA analyst. Your job is to verify an extraction agent's clause assessment. Check for errors in position assignment, category matching, citation accuracy, and confidence calibration. Respond ONLY with valid JSON."
    )
    assert f"- Position: acceptable\n- Confidence: {confidence}\n" in messages[1]["content"]


def test_gateway_wrapper_success_uses_one_original_call(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = Mock()
    gateway.chat.return_value = "synthetic success"
    constructor = Mock(return_value=gateway)
    router = ModuleType("openreview_cli.gateway.router")
    router.Gateway = constructor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, router.__name__, router)
    messages = [{"role": "user", "content": "synthetic"}]
    requirement = CapabilityRequirement(capability="reasoning")
    coordinator = Mock()
    context = RecoveryContext()
    result = _gateway.call_gateway_chat(
        "extract-slot",
        messages,
        requirement=requirement,
        session_id="receipt-session",
        coordinator=coordinator,
        recovery_ctx=context,
        provider_list=["provider-a"],
    )
    assert result == "synthetic success"
    constructor.assert_called_once_with()
    gateway.chat.assert_called_once_with(
        "extract-slot", messages, requirement=requirement, session_id="receipt-session"
    )
    assert context.provider_list == []
    assert coordinator.mock_calls == []
