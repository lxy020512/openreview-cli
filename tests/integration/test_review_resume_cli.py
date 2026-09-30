import asyncio
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from openreview_cli.app import app
from openreview_cli.pipeline.base import Stage
from openreview_cli.pipeline.runner import Pipeline
from openreview_cli.review.checkpoints import CheckpointError
from openreview_cli.review.playbook import load_bundled
from openreview_cli.review.runner import run_review

pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    # These tests exercise review commands, not global user-state initialization.
    monkeypatch.setattr("openreview_cli.app._init", lambda *a, **k: None)
    for module in ("openreview_cli.app", "openreview_cli.config.paths"):
        monkeypatch.setattr(module + ".get_config_dir", lambda: tmp_path)
        monkeypatch.setattr(module + ".get_data_dir", lambda: tmp_path)


def test_cli_resume_forwarding_and_force_requires_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr("openreview_cli.app._emit_reviews", lambda *a, **k: None)
    monkeypatch.setattr("openreview_cli.review.run_review", lambda **k: _record_reply(calls, k, []))
    result = CliRunner().invoke(
        app, ["precheck", "review", "input.docx", "--resume", "--no-grounding"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["resume"] is True
    result = CliRunner().invoke(app, ["precheck", "review", "input.docx", "--force-review"])
    assert result.exit_code != 0
    assert len(calls) == 1


def test_fatal_safety_error_bypasses_pipeline_and_runner_and_halts_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(Stage):
        name = "broken"
        critical = False

        async def run(self, ctx: Any) -> Any:
            raise CheckpointError("checkpoint storage unavailable")

    with pytest.raises(CheckpointError):
        asyncio.run(Pipeline([Broken()]).run({}))
    files = [tmp_path / "one.docx", tmp_path / "two.docx"]
    for path in files:
        path.write_bytes(b"synthetic")
    calls: list[Any] = []

    def fail(**kwargs: Any) -> Any:
        calls.append(kwargs["doc_path"])
        raise CheckpointError("checkpoint storage unavailable")

    monkeypatch.setattr("openreview_cli.review.runner._run_review_doc_pipeline", fail)
    with pytest.raises(CheckpointError):
        run_review([str(path) for path in files])
    assert calls == [files[0]]


def test_cli_fatal_error_is_nonzero_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(**kwargs: Any) -> Any:
        raise CheckpointError("checkpoint storage unavailable")

    monkeypatch.setattr("openreview_cli.review.run_review", fail)
    result = CliRunner().invoke(app, ["precheck", "review", "input.docx", "--resume"])
    assert result.exit_code != 0
    assert "checkpoint storage unavailable" in result.output


def test_checkpoint_clear_preserves_old_version_and_cost_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openreview_cli.review.checkpoints import document_hash
    from openreview_cli.storage.checkpoints import CheckpointStore
    from openreview_cli.storage.database import get_connection

    monkeypatch.setattr("openreview_cli.config.paths.get_data_dir", lambda: tmp_path)
    path = tmp_path / "input.docx"
    path.write_bytes(b"new version")
    store = CheckpointStore(tmp_path / "openreview.db")
    store.get_or_create("new", document_hash(path))
    store.get_or_create("old", "oldhash")
    result = CliRunner().invoke(app, ["precheck", "checkpoints-clear", str(path)])
    assert result.exit_code == 0, result.output
    assert "1 run(s), 0 step(s)" in result.output
    with get_connection(tmp_path / "openreview.db") as conn:
        assert (
            conn.execute("SELECT identity_digest FROM review_checkpoint_runs").fetchone()[0]
            == "old"
        )
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='cost_logs'").fetchone()


def test_runner_actual_pipeline_wiring_and_resume_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from openreview_cli.pipeline.adapters.parse import ParseStage
    from openreview_cli.pipeline.adapters.strip import StripStage
    from openreview_cli.review import extraction, qa

    category = load_bundled().categories[0]
    path = tmp_path / "input.docx"
    path.write_bytes(b"synthetic document")
    clauses = [SimpleNamespace(id="c1", text=category.name + " synthetic")]

    async def parse(self: Any, ctx: Any) -> Any:
        return {"document": SimpleNamespace(source_path=path, page_count=1), "clauses": clauses}

    async def strip(self: Any, ctx: Any) -> Any:
        return {"stripped_clauses": clauses}

    monkeypatch.setattr(ParseStage, "run", parse)
    monkeypatch.setattr(StripStage, "run", strip)
    monkeypatch.setattr(
        "openreview_cli.review.checkpoints.runtime_snapshot", lambda *a: {"effective": "fixed"}
    )
    calls: list[Any] = []
    monkeypatch.setattr(
        extraction,
        "call_gateway_chat",
        lambda *a, **k: _record_reply(
            calls,
            k["session_id"],
            '{"position":"acceptable","confidence":0.8,"citation":"synthetic","category_match":true}',
        ),
    )
    monkeypatch.setattr(
        qa,
        "call_gateway_chat",
        lambda *a, **k: _record_reply(
            calls,
            k["session_id"],
            '{"verdict":"agree","revised_position":null,"rationale":"","citation_valid":true,"position_valid":true,"category_valid":true,"confidence_valid":true}',
        ),
    )
    report = run_review([str(path)], resume=True)
    assert len(report) == 1
    assert len(calls) == 2
    first_session = calls[0]
    assert len(run_review([str(path)], resume=True)) == 1
    assert len(calls) == 2
    assert len(run_review([str(path)], resume=True, force_review=True)) == 1
    assert calls == [first_session] * 4


def _record_reply(calls: list[Any], record: Any, response: Any) -> Any:
    calls.append(record)
    return response
