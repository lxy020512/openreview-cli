from pathlib import Path

import pytest

"""Privacy checks limited to the new checkpoint tables, not all upstream storage."""

from types import SimpleNamespace

from openreview_cli.review.checkpoints import CheckpointSession
from openreview_cli.review.models import ClauseAssessment, Position, QAVerdict


def test_checkpoint_database_and_wal_do_not_contain_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "PRIVATE-FILENAME.docx"
    source.write_bytes(b"PRIVATE-CONTRACT-BYTES")
    monkeypatch.setattr(
        "openreview_cli.review.checkpoints.runtime_snapshot",
        lambda *a: {"api_key": "SYNTHETIC-PRIVATE-CREDENTIAL"},
    )
    session = CheckpointSession(
        source,
        settings=lambda: {"privacy": "strict"},
        slots=("extraction", "reasoning"),
        db_path=tmp_path / "db.sqlite",
        key_path=tmp_path / "isolated-config" / "key",
    )
    clause = SimpleNamespace(id="c1", text="PRIVATE-CLAUSE-TEXT")
    assessment = ClauseAssessment(
        "c1",
        clause.text,
        "term",
        Position.ACCEPTABLE,
        0.8,
        "PRIVATE-EMAIL@example.test",
        QAVerdict.agree,
        "extraction",
        "reasoning",
        qa_revised_rationale="PRIVATE-NAME",
    )
    session.save(clause, "qa", assessment)
    for file in tmp_path.glob("db.sqlite*"):
        contents = file.read_bytes()
        for secret in (
            b"PRIVATE-CLAUSE",
            b"PRIVATE-EMAIL",
            b"PRIVATE-NAME",
            b"PRIVATE-FILENAME",
            b"SYNTHETIC-PRIVATE",
            session.key,
        ):
            assert secret not in contents
    assert "PRIVATE" not in caplog.text
    loaded = session.load(clause, "qa", "term")
    assert loaded is not None
    assert loaded.citation == "PRIVATE-EMAIL@example.test"
    session.store.set_step(
        session.identity,
        clause.id,
        session.clause_hash(clause),
        "term",
        "qa",
        "completed",
        b"PRIVATE-CORRUPTED-PAYLOAD",
    )
    assert session.load(clause, "qa", "term") is None
    assert "PRIVATE-CORRUPTED" not in caplog.text
