import asyncio
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openreview_cli.review import extraction, qa
from openreview_cli.review.checkpoints import CheckpointError, CheckpointSession
from openreview_cli.review.pipeline import ReviewStage
from openreview_cli.review.playbook import load_bundled

pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


@pytest.fixture
def checkpoint_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    playbook = load_bundled()
    category = playbook.categories[0]
    doc = tmp_path / "nda.docx"
    doc.write_bytes(b"synthetic document")
    monkeypatch.setattr(
        "openreview_cli.review.checkpoints.runtime_snapshot", lambda *a: {"config": "fixed"}
    )

    def create() -> Any:
        return CheckpointSession(
            doc,
            settings=lambda: {"playbook": asdict(playbook)},
            slots=("extraction", "reasoning"),
            db_path=tmp_path / "db.sqlite",
            key_path=tmp_path / "config" / "key",
        )

    def stage() -> Any:
        return ReviewStage(playbook, qa_model="reasoning", checkpoints=create())

    clauses = [SimpleNamespace(id="c1", text=category.name + " text")]
    ctx = {"clauses": clauses, "document": SimpleNamespace(source_path=doc, page_count=1)}
    return stage, ctx, create


def test_qa_interruption_reuses_extraction_snapshot(
    checkpoint_case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage, ctx, create = checkpoint_case
    calls: list[Any] = []
    monkeypatch.setattr(
        extraction,
        "call_gateway_chat",
        lambda *a, **k: _record_reply(
            calls,
            "extraction",
            '{"position":"acceptable","confidence":0.8,"citation":"evidence","category_match":true}',
        ),
    )

    def interrupted(*a: Any, **k: Any) -> Any:
        calls.append("qa")
        raise KeyboardInterrupt()

    monkeypatch.setattr(qa, "call_gateway_chat", interrupted)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(stage().run(ctx))
    monkeypatch.setattr(
        qa,
        "call_gateway_chat",
        lambda *a, **k: _record_reply(
            calls,
            "qa",
            '{"verdict":"disagree","revised_position":"walkaway","rationale":"revision","citation_valid":true,"position_valid":true,"category_valid":true,"confidence_valid":true}',
        ),
    )
    result = asyncio.run(stage().run(ctx))
    assert calls == ["extraction", "qa", "qa"]
    assert result["review_assessments"][0].qa_revised_rationale == "revision"
    assert (
        create()
        .load(ctx["clauses"][0], "extraction", result["review_assessments"][0].playbook_category)
        .qa_revised_rationale
        is None
    )
    asyncio.run(stage().run(ctx))
    assert len(calls) == 3


def test_failed_qa_retries_only_qa_and_guard_change_halts(
    checkpoint_case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage, ctx, create = checkpoint_case
    calls: list[Any] = []
    monkeypatch.setattr(
        extraction,
        "call_gateway_chat",
        lambda *a, **k: _record_reply(
            calls,
            "extraction",
            '{"position":"uncertain","confidence":0.8,"citation":"","category_match":true}',
        ),
    )
    monkeypatch.setattr(
        qa, "call_gateway_chat", lambda *a, **k: _record_reply(calls, "qa", "bad json")
    )
    assert asyncio.run(stage().run(ctx))["review_assessments"][0].error == "qa_invalid_response"
    asyncio.run(stage().run(ctx))
    assert calls == ["extraction", "qa", "qa"]
    checkpoints = create()
    ctx["document"].source_path.write_bytes(b"changed")
    with pytest.raises(CheckpointError, match="checkpoint inputs changed"):
        checkpoints.ensure_current()


def test_no_match_has_no_calls(checkpoint_case: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    stage, ctx, create = checkpoint_case
    ctx["clauses"][0].text = "123 xyz"

    def forbidden(*a: Any, **k: Any) -> Any:
        raise AssertionError("no model call expected")

    monkeypatch.setattr(extraction, "call_gateway_chat", forbidden)
    monkeypatch.setattr(qa, "call_gateway_chat", forbidden)
    assert asyncio.run(stage().run(ctx))["review_assessments"][0].playbook_category == "no-match"
    assert asyncio.run(stage().run(ctx))["review_assessments"][0].playbook_category == "no-match"


def test_config_change_after_reply_prevents_completed_write(
    checkpoint_case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openreview_cli.storage.database import get_connection

    stage, ctx, create = checkpoint_case
    state = {"value": "original"}
    monkeypatch.setattr(
        "openreview_cli.review.checkpoints.runtime_snapshot", lambda *a: state.copy()
    )

    def change_after_reply(*a: Any, **k: Any) -> Any:
        state["value"] = "changed"
        return '{"position":"acceptable","confidence":0.8,"citation":"","category_match":true}'

    monkeypatch.setattr(extraction, "call_gateway_chat", change_after_reply)
    review = stage()
    with pytest.raises(CheckpointError, match="checkpoint inputs changed"):
        asyncio.run(review.run(ctx))
    with get_connection(review._checkpoints.db_path) as conn:
        row = conn.execute("SELECT status,payload FROM review_checkpoint_steps").fetchone()
        assert row["status"] == "running"
        assert row["payload"] is None


def _record_reply(calls: list[Any], record: Any, response: Any) -> Any:
    calls.append(record)
    return response
