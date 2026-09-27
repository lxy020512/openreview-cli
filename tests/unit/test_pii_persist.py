"""PII persistence — audit trail rows + cache rows written by the review pipeline.

Regression coverage for D-8: the supported review pipeline (StripStage) persists
the result of every PII strip to the PII governance lifecycle (pii_audit_trail
table / pii_cache rows), so ``pii list`` reports the recorded entity_count. Every
strip records one ``pii_audit_trail`` row; a clean document records
``entity_count=0`` and still gets no ``pii_cache`` row.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from openreview_cli.pii.cache import PiiCache
from openreview_cli.pii.mapping import read_pii_mapping
from openreview_cli.pii.models import PiiEntity, PiiResult
from openreview_cli.pii.persist import (
    persist_pii_for_document,
    persist_pii_result,
    write_audit_trail_row,
)
from openreview_cli.storage import init_database


def _entity(
    entity_type: str,
    placeholder: str,
    original: str = "value",
    score: float = 0.9,
) -> PiiEntity:
    return PiiEntity(
        entity_type=entity_type,
        original_value=original,
        start=0,
        end=len(original),
        score=score,
        placeholder=placeholder,
        source="nlp",
    )


def _result_with_mapping(
    failed_pages: list[int] | None = None,
    duration_seconds: float = 1.5,
) -> PiiResult:
    return PiiResult(
        stripped_text="Hello [PARTY_A] and [PERSON_1]",
        mapping={"PARTY_A": "Acme", "PERSON_1": "Jane"},
        entities=[
            _entity("ORGANIZATION", "[PARTY_A]", original="Acme"),
            _entity("PERSON", "[PERSON_1]", original="Jane"),
        ],
        page_count=2,
        duration_seconds=duration_seconds,
        warnings=[],
        failed_pages=failed_pages,
    )


def _result_no_mapping() -> PiiResult:
    return PiiResult(
        stripped_text="Hello world",
        mapping={},
        entities=[],
        page_count=1,
        duration_seconds=0.5,
        warnings=[],
        failed_pages=None,
    )


# ── write_audit_trail_row ────────────────────────────────────────────────


def test_write_audit_trail_row_writes_correct_columns(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    init_database(db)

    pii_result = _result_with_mapping()
    write_audit_trail_row(
        db,
        document_hash="h" * 64,
        config_hash="cfg-hash",
        pii_result=pii_result,
        status="success",
    )

    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM pii_audit_trail").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    row = dict(rows[0])
    assert row["document_hash"] == "h" * 64
    assert row["config_hash"] == "cfg-hash"
    assert row["entity_count"] == 2
    assert json.loads(row["entity_type_distribution"]) == {
        "ORGANIZATION": 1,
        "PERSON": 1,
    }
    assert row["processing_time_ms"] == 1500
    assert row["status"] == "success"
    assert json.loads(row["failed_pages"]) == []
    assert row["timestamp"]


def test_write_audit_trail_row_partial_status_and_failed_pages(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    init_database(db)

    write_audit_trail_row(
        db,
        document_hash="a" * 64,
        config_hash="cfg-hash",
        pii_result=_result_with_mapping(failed_pages=[2, 3]),
        status="partial",
    )

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM pii_audit_trail").fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["document_hash"] == "a" * 64
    assert row["processing_time_ms"] == 1500
    assert row["status"] == "partial"
    assert json.loads(row["failed_pages"]) == [2, 3]


# ── persist_pii_result ───────────────────────────────────────────────────


def test_persist_pii_result_writes_cache_and_audit_rows(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    init_database(db)
    review_dir = tmp_path / "reviews" / ("h" * 12)

    persist_pii_result(
        db,
        document_hash="h" * 64,
        config_hash="cfg-hash",
        pii_result=_result_with_mapping(),
        review_dir=review_dir,
        encryption_key="test-key-1234567890123456",
    )

    # pii_cache row exists with paths pointing at the written files
    cache_row = PiiCache(db).get("h" * 64)
    assert cache_row is not None
    assert cache_row["config_hash"] == "cfg-hash"
    assert Path(cache_row["review_result_path"]).exists()
    assert Path(cache_row["mapping_path"]).exists()
    assert Path(cache_row["review_result_path"]).read_text(encoding="utf-8") == (
        "Hello [PARTY_A] and [PERSON_1]"
    )

    # audit trail row exists with correct entity_count
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM pii_audit_trail").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["entity_count"] == 2
    assert row["status"] == "success"


def test_persist_pii_result_records_an_audit_row_for_a_clean_document(
    tmp_path: Path,
) -> None:
    db = tmp_path / "t.db"
    init_database(db)
    review_dir = tmp_path / "reviews" / ("n" * 12)

    persist_pii_result(
        db,
        document_hash="n" * 64,
        config_hash="cfg-hash",
        pii_result=_result_no_mapping(),
        review_dir=review_dir,
        encryption_key="test-key-1234567890123456",
    )

    assert PiiCache(db).get("n" * 64) is None
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM pii_audit_trail").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["entity_count"] == 0
    assert row["status"] == "success"
    assert not (review_dir / "stripped.txt").exists()
    assert not (review_dir / "pii_map.enc").exists()


def test_persist_pii_result_stores_the_filename(tmp_path: Path) -> None:
    """persist_pii_result threads an explicit filename into the cache row."""
    db = tmp_path / "t.db"
    init_database(db)
    review_dir = tmp_path / "reviews" / ("h" * 12)

    persist_pii_result(
        db,
        document_hash="h" * 64,
        config_hash="cfg-hash",
        pii_result=_result_with_mapping(),
        review_dir=review_dir,
        encryption_key="test-key-1234567890123456",
        filename="Acme_NDA_v3.pdf",
    )

    cache_row = PiiCache(db).get("h" * 64)
    assert cache_row is not None
    assert cache_row["filename"] == "Acme_NDA_v3.pdf"


def test_persist_pii_result_preserves_a_prior_mapping_when_a_rewrite_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed re-write must not destroy or orphan the prior complete mapping.

    On a re-write the real atomic writer leaves the PRIOR COMPLETE ``pii_map.enc``
    in place when its ``os.replace`` fails.  The rollback must therefore NOT unlink
    it (and must not leave the ``pii_cache`` row dangling), while it may only
    remove an artifact it itself created on a first write.
    """
    db = tmp_path / "t.db"
    init_database(db)
    review_dir = tmp_path / "reviews" / ("h" * 12)
    encryption_key = "test-key-1234567890123456"
    mapping = {"PARTY_A": "Acme", "PERSON_1": "Jane"}

    # First (successful) persist: the mapping + the pii_cache row now exist.
    persist_pii_result(
        db,
        document_hash="h" * 64,
        config_hash="cfg-hash",
        pii_result=_result_with_mapping(),
        review_dir=review_dir,
        encryption_key=encryption_key,
    )
    mapping_path = review_dir / "pii_map.enc"
    assert mapping_path.exists()
    assert PiiCache(db).get("h" * 64) is not None
    assert read_pii_mapping(review_dir, encryption_key) == mapping

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("injected os.replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        persist_pii_result(
            db,
            document_hash="h" * 64,
            config_hash="cfg-hash",
            pii_result=_result_with_mapping(),
            review_dir=review_dir,
            encryption_key=encryption_key,
        )

    # The prior complete mapping survived, intact (0600) and still decryptable.
    assert mapping_path.exists(), "the prior complete mapping was destroyed"
    assert mapping_path.stat().st_mode & 0o777 == 0o600
    assert read_pii_mapping(review_dir, encryption_key) == mapping
    # ...and its cache row still points at a file that exists (not dangling).
    cache_row = PiiCache(db).get("h" * 64)
    assert cache_row is not None
    assert Path(cache_row["mapping_path"]).exists()
    assert list(review_dir.glob(".pii_map.enc.*.tmp")) == [], "a temp file survived"


def test_persist_pii_result_failure_never_deletes_a_legacy_audit_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback removes only the mapping artifact, never a legacy audit file.

    ``delete_pii_mapping`` owns both ``pii_map.enc`` and the legacy
    ``pii_audit.json``; the ``persist_pii_result`` rollback must not reach for it,
    or a failed strip would destroy an artifact it never wrote.
    """
    db = tmp_path / "t.db"
    init_database(db)
    review_dir = tmp_path / "reviews" / ("h" * 12)
    review_dir.mkdir(parents=True)
    legacy = review_dir / "pii_audit.json"
    legacy.write_text("{}", encoding="utf-8")

    def _boom(mapping: dict[str, str], review_dir: Path, encryption_key: str) -> Path:
        raise OSError("injected mapping write failure")

    monkeypatch.setattr("openreview_cli.pii.persist.write_pii_mapping", _boom)

    with pytest.raises(OSError):
        persist_pii_result(
            db,
            document_hash="h" * 64,
            config_hash="cfg-hash",
            pii_result=_result_with_mapping(),
            review_dir=review_dir,
            encryption_key="test-key-1234567890123456",
        )

    assert legacy.exists(), "the rollback deleted the legacy pii_audit.json"


def test_persist_pii_for_document_stores_the_source_basename(
    tmp_path: Path, isolated_xdg: dict[str, Path]
) -> None:
    """The document path's BASENAME is stored, never the fuller path.

    A full path can carry a client name; the governance database must not
    become the place that leaks it.
    """
    doc_dir = tmp_path / "x"
    doc_dir.mkdir()
    doc_path = doc_dir / "Acme_NDA_v3.pdf"
    doc_path.write_bytes(b"%PDF-1.4\nseed\n")

    persist_pii_for_document(doc_path, _result_with_mapping())

    document_hash = hashlib.sha256(doc_path.read_bytes()).hexdigest()
    cache_row = PiiCache(isolated_xdg["db_path"]).get(document_hash)
    assert cache_row is not None
    assert cache_row["filename"] == "Acme_NDA_v3.pdf"


# ── StripStage integration ───────────────────────────────────────────────


def test_strip_stage_persists_pii_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """StripStage.run persists a real PiiResult to pii_cache + pii_audit_trail."""
    import hashlib

    from openreview_cli.config.loader import load_config
    from openreview_cli.pipeline.adapters.strip import StripStage

    doc_path = tmp_path / "contract.txt"
    doc_path.write_text("Confidential NDA between Acme and Jane", encoding="utf-8")

    clauses = [type("Clause", (), {"id": "1", "text": "Hello [PARTY_A] and [PERSON_1]"})()]
    pii_result = _result_with_mapping()

    monkeypatch.setattr(
        "openreview_cli.pii.strip_pii_clauses",
        lambda *args, **kwargs: (clauses, pii_result),
    )

    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    config_path = config_dir / "config.yml"
    load_config(config_path)  # materialise a valid config.yml for the tmp config dir

    monkeypatch.setattr("openreview_cli.config.paths.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("openreview_cli.config.paths.get_config_dir", lambda: config_dir)

    db = data_dir / "openreview.db"
    init_database(db)  # mirrors app startup (app.py:211) before a review runs

    stage = StripStage()
    ctx = {"clauses": clauses, "document_path": str(doc_path), "document": None}
    result = asyncio.run(stage.run(ctx))

    assert result == {"stripped_clauses": clauses}

    document_hash = hashlib.sha256(doc_path.read_bytes()).hexdigest()
    db = data_dir / "openreview.db"

    cache_row = PiiCache(db).get(document_hash)
    assert cache_row is not None
    assert len(cache_row["config_hash"]) == 64
    assert Path(cache_row["review_result_path"]).read_text(encoding="utf-8") == (
        "Hello [PARTY_A] and [PERSON_1]"
    )
    assert Path(cache_row["mapping_path"]).exists()
    assert cache_row["filename"] == doc_path.name

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT entity_count, status, config_hash FROM pii_audit_trail WHERE document_hash = ?",
            (document_hash,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["entity_count"] == 2
    assert row["status"] == "success"
    assert row["config_hash"] == cache_row["config_hash"]
