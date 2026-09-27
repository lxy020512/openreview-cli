"""Shared probe harness for the W3 LLM-response fuzzing suites.

Private to ``tests/fuzz`` (there is no ``tests/fuzz/conftest.py``; the plan
deleted it), so nothing here is a fixture. It supplies three things the three
W3 suites share:

* the documented safe-default response shape (``_SAFE_DEFAULT_RESPONSE`` in
  ``review/extraction.py``) and a minimal valid review ``Category`` /
  ``ClauseAssessment``;
* a canned-gateway double built on
  ``tests.helpers.mock_gateway._MockGateway`` so a case can drive
  ``extract_clause`` / ``verify_assessment`` end to end with one hostile reply
  and no provider;
* a call counter used to prove the code under test was actually reached — the
  anti-vacuity requirement of plan section 9.3, rule 2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openreview_cli.review.models import (
    Category,
    ClauseAssessment,
    Position,
    PositionDef,
    QAVerdict,
)
from openreview_cli.review.prompts import _build_extraction_messages_common
from tests.helpers.mock_gateway import _MockGateway

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    import pytest

# The documented extraction fallback shape (_SAFE_DEFAULT_RESPONSE).
SAFE_DEFAULT_RESPONSE: dict[str, Any] = {
    "position": "uncertain",
    "confidence": 0.0,
    "citation": "",
    "category_match": False,
}
RESPONSE_KEYS = frozenset(SAFE_DEFAULT_RESPONSE)


class CallCounter:
    """Wrap a callable and count every call to it."""

    def __init__(self, real: Callable[..., Any]) -> None:
        self.real = real
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self.real(*args, **kwargs)


def instrument(monkeypatch: pytest.MonkeyPatch, module: ModuleType, name: str) -> CallCounter:
    """Patch ``module.name`` with a counting pass-through and return the counter.

    The review modules bind their imports into their own namespace, so the
    attribute on the *consumer* module is what must be replaced.
    """
    counter = CallCounter(getattr(module, name))
    monkeypatch.setattr(module, name, counter)
    return counter


class CannedGateway(_MockGateway):
    """A mock gateway that returns one canned hostile reply and counts calls."""

    def __init__(self, response: str) -> None:
        super().__init__()
        self.response = response
        self.chat_call_count = 0

    def chat(
        self,
        slot: str,
        messages: list[dict[str, str]],
        *,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        self.chat_call_count += 1
        return self.response

    def call_gateway_chat(
        self,
        slot: str,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        """Signature-compatible replacement for ``review._gateway.call_gateway_chat``."""
        return self.chat(slot, messages)


def install_gateway(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, response: str
) -> CannedGateway:
    """Replace ``module.call_gateway_chat`` with a canned-gateway double.

    Returns the double so the caller can assert the model call was reached.
    """
    gateway = CannedGateway(response)
    monkeypatch.setattr(module, "call_gateway_chat", gateway.call_gateway_chat)
    return gateway


def make_category() -> Category:
    """A minimal valid review category whose ``default_position`` is acceptable."""
    definition = PositionDef(description="definition", exemplars=["example clause"])
    return Category(
        id="confidentiality",
        name="Confidentiality",
        description="Confidentiality obligations",
        preferred=definition,
        acceptable=definition,
        walkaway=definition,
        default_position=Position.ACCEPTABLE,
    )


def make_assessment(
    position: Position = Position.PREFERRED,
    confidence: float = 0.9,
) -> ClauseAssessment:
    """A valid extraction assessment for the QA stage to verify."""
    return ClauseAssessment(
        clause_id="clause-1",
        clause_text="The parties shall keep information confidential.",
        playbook_category="confidentiality",
        position=position,
        confidence=confidence,
        citation="keep information confidential",
        qa_verdict=QAVerdict.uncertain,
        extraction_model="extraction-slot",
        qa_model="qa-slot",
    )


def build_extraction_prompt(clause_text: str) -> list[dict[str, str]]:
    """Build the real extraction messages for *clause_text* (precheck mode)."""
    return _build_extraction_messages_common(
        clause_text=clause_text,
        category_id="confidentiality",
        category_name="Confidentiality",
        category_description="Confidentiality obligations",
        preferred_desc="Preferred.",
        preferred_exemplars=["example clause"],
        acceptable_desc="Acceptable.",
        acceptable_exemplars=["example clause"],
        walkaway_desc="Walkaway.",
        walkaway_exemplars=["example clause"],
        default_position="acceptable",
        mode="precheck",
    )


def assert_safe_response(result: object) -> None:
    """Assert *result* is exactly the documented dict shape or a populated reply."""
    assert isinstance(result, dict), f"expected a dict, got {type(result).__name__}"
    assert set(result) == RESPONSE_KEYS, f"unexpected keys: {sorted(result)}"
