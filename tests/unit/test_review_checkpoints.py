from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from openreview_cli.review.checkpoints import CheckpointCodec, CheckpointError, load_checkpoint_key
from openreview_cli.review.models import ClauseAssessment, Position, QAVerdict


def assessment() -> ClauseAssessment:
    return ClauseAssessment(
        "c1",
        "PRIVATE-CONTRACT-TEXT",
        "term",
        Position.ACCEPTABLE,
        0.8,
        "PRIVATE-CITATION",
        QAVerdict.agree,
        "extraction",
        "reasoning",
    )


def test_encrypted_whitelist_roundtrip() -> None:
    codec = CheckpointCodec(Fernet.generate_key())
    blob = codec.encode(assessment())
    assert b"PRIVATE" not in blob
    restored = codec.decode(
        blob,
        clause_id="c1",
        category="term",
        text="current text",
        extraction_model="extraction",
        qa_model="reasoning",
    )
    assert restored is not None
    assert restored.clause_text == "current text"
    assert restored.citation == "PRIVATE-CITATION"
    assert restored.position is Position.ACCEPTABLE
    raw = codec.fernet.decrypt(blob)
    assert b"PRIVATE-CONTRACT-TEXT" not in raw
    assert b"clause_text" not in raw


def test_codec_rejects_wrong_identity_corruption_and_extra_fields() -> None:
    import json

    codec = CheckpointCodec(Fernet.generate_key())
    blob = codec.encode(assessment())
    assert (
        codec.decode(
            b"bad",
            clause_id="c1",
            category="term",
            text="",
            extraction_model="extraction",
            qa_model="reasoning",
        )
        is None
    )
    assert (
        codec.decode(
            blob,
            clause_id="other",
            category="term",
            text="",
            extraction_model="extraction",
            qa_model="reasoning",
        )
        is None
    )
    raw = json.loads(codec.fernet.decrypt(blob))
    raw["confidence"] = True
    assert (
        codec.decode(
            codec.fernet.encrypt(json.dumps(raw).encode()),
            clause_id="c1",
            category="term",
            text="",
            extraction_model="extraction",
            qa_model="reasoning",
        )
        is None
    )
    raw["clause_text"] = "secret"
    assert (
        codec.decode(
            codec.fernet.encrypt(json.dumps(raw).encode()),
            clause_id="c1",
            category="term",
            text="",
            extraction_model="extraction",
            qa_model="reasoning",
        )
        is None
    )


def test_key_is_reused_and_invalid_existing_key_fails_safely(tmp_path: Path) -> None:
    key_path = tmp_path / "config" / "review-checkpoints.key"
    key = load_checkpoint_key(key_path)
    assert load_checkpoint_key(key_path) == key
    key_path.write_bytes(b"BAD-SECRET")
    with pytest.raises(CheckpointError, match="checkpoint key unavailable") as exc:
        load_checkpoint_key(key_path)
    assert "BAD-SECRET" not in str(exc.value)
