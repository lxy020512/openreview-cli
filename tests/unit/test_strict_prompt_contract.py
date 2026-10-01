"""Strict extraction prompts must advertise the schema checkpointing accepts."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from openreview_cli.prompts.defaults import load_defaults
from openreview_cli.prompts.store import PromptStore
from openreview_cli.review import extraction
from openreview_cli.review.checkpoints import CheckpointCodec
from openreview_cli.review.models import Position
from openreview_cli.review.playbook import load_bundled
from openreview_cli.review.prompts import MODE_VOCABULARY, _build_extraction_messages_common


def _build_messages(mode: str = "precheck", **kwargs: Any) -> list[dict[str, str]]:
    return _build_extraction_messages_common(
        clause_text="This clause concerns another category.",
        category_id="test-cat",
        category_name="Test Category",
        category_description="A test category",
        preferred_desc="Preferred",
        preferred_exemplars=["Example 1"],
        acceptable_desc="Acceptable",
        acceptable_exemplars=["Example 2"],
        walkaway_desc="Walkaway",
        walkaway_exemplars=["Example 3"],
        default_position="acceptable",
        mode=mode,
        **kwargs,
    )


def _advertised_positions(messages: list[dict[str, str]]) -> list[str]:
    position_line = next(
        line for line in messages[1]["content"].splitlines() if line.startswith('  "position":')
    )
    return re.findall(r'"([^"]+)"', position_line)[1:]


@pytest.mark.parametrize("mode", MODE_VOCABULARY)
def test_strict_prompt_advertises_only_validator_positions(mode: str) -> None:
    positions = _advertised_positions(_build_messages(mode, strict=True))
    assert positions == [position.value for position in Position]
    for position in positions:
        raw = json.dumps(
            {"position": position, "confidence": 0.0, "citation": "", "category_match": False}
        )
        assert extraction._strict_response_valid(raw)


def test_strict_prompt_guides_mismatch_and_insufficient_evidence_to_uncertain() -> None:
    user = _build_messages(strict=True)[1]["content"]
    assert "does not match this category" in user
    assert "evidence is insufficient" in user
    assert '"position": "uncertain" and "category_match": false' in user
    assert "Default position (if no specific indicators match)" not in user


def test_default_prompt_retains_existing_fallback_contract() -> None:
    messages = _build_messages()
    assert _build_messages(strict=False) == messages
    assert _advertised_positions(messages) == ["preferred", "acceptable", "walkaway", "no-match"]
    assert (
        "### Default position (if no specific indicators match)\nacceptable\n\n"
        in (messages[1]["content"])
    )


@pytest.mark.parametrize("strict", [False, True])
def test_extraction_sends_the_prompt_for_its_validation_mode(
    strict: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages_sent: list[list[dict[str, str]]] = []

    def mock_chat(_slot: str, messages: list[dict[str, str]], **_kwargs: Any) -> str:
        messages_sent.append(messages)
        return '{"position":"uncertain","confidence":0.0,"citation":"","category_match":false}'

    monkeypatch.setattr(extraction, "call_gateway_chat", mock_chat)
    category = load_bundled().categories[0]
    result = extraction.extract_clause("text", "c1", category, "extraction", strict=strict)
    assert result.error is None
    assert len(messages_sent) == 1
    expected_positions = (
        [position.value for position in Position]
        if strict
        else ["preferred", "acceptable", "walkaway", "no-match"]
    )
    assert _advertised_positions(messages_sent[0]) == expected_positions


def test_strict_uncertain_category_mismatch_is_checkpointable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = '{"position":"uncertain","confidence":0.0,"citation":"","category_match":false}'
    monkeypatch.setattr(extraction, "call_gateway_chat", lambda *a, **k: raw)
    category = load_bundled().categories[0]
    result = extraction.extract_clause("text", "c1", category, "extraction", strict=True)
    assert result.error is None
    assert result.position is Position.UNCERTAIN
    assert result.position is not category.default_position
    codec = CheckpointCodec(Fernet.generate_key())
    restored = codec.decode(
        codec.encode(result),
        clause_id="c1",
        category=category.id,
        text="current text",
        extraction_model="extraction",
        qa_model="extraction",
    )
    assert restored is not None
    assert restored.position is Position.UNCERTAIN
    assert restored.error is None
    assert restored.clause_text == "current text"


def test_default_prompt_versions_are_inactive_until_explicitly_bound(tmp_path: Path) -> None:
    store = PromptStore(tmp_path / "prompts.sqlite")
    store.init()
    load_defaults(store)
    assert store.get_latest("extraction").content
    assert store.get_latest("reasoning").content
    assert store.bindings() == []
    assert store.resolve("extraction") == ""
    assert store.resolve("reasoning") == ""
    store.bind("extraction", "extraction", 1)
    assert store.resolve("extraction")
    assert store.resolve("reasoning") == ""
