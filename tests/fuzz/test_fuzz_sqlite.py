"""W4 fuzz suite: the SQLite boundaries (shared database + retrieval index).

Sharp edge 8 (plan section 3): ``_exec_migration_safely`` (``storage/database.py:68-78``)
is documented to tolerate only three idempotent-rerun message patterns
(``duplicate column`` / ``already exists`` / ``no such column``) but the code has
no ``else``/``raise``: EVERY ``sqlite3.OperationalError`` falls out of the except
block unraised. The required question — "does the migration swallow hide real
errors?" — is answered below with a direct proof and an end-to-end proof.

Retrieval-owned failures are asserted with the retrieval exception classes
(``IndexNotFoundError`` → registry code 40, ``IndexCorruptError`` → 41). The
unreachability of codes 40-43 at the CLI is already recorded as RT-003 and is not
re-asserted here. Issue #118 (a raw ``sqlite3.DatabaseError`` from the retrieval
index reader) is pre-existing and is deduped, not refiled.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from openreview_cli.retrieval.engine import RetrievalEngine
from openreview_cli.retrieval.errors import IndexCorruptError, IndexNotFoundError, RetrievalError
from openreview_cli.retrieval.models import RetrievalQuery
from openreview_cli.retrieval.storage import RetrievalStorage
from openreview_cli.storage import database
from tests.fuzz import _state_probe
from tests.helpers import corpus_state

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.fuzz


def _latest_migration_version() -> int:
    return max(int(p.stem.split("_")[0]) for p in database.MIGRATIONS_DIR.glob("*.sql"))


def _user_version(db_path: Path) -> int:
    conn = database.get_connection(db_path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _table_names(db_path: Path) -> set[str]:
    conn = database.get_connection(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {str(r[0]) for r in rows}
    finally:
        conn.close()


# ── Sharp edge 8: the migration swallow (the required verdict) ──────────────


def test_migration_swallow_does_not_hide_a_non_matching_operational_error(tmp_path: Path) -> None:
    conn = database.get_connection(tmp_path / "scratch.db")
    try:
        sql_file = tmp_path / "999_bad.sql"
        sql_file.write_text("INSERT INTO no_such_table VALUES (1);", encoding="utf-8")
        with (
            _state_probe.count_calls(database, "_exec_migration_safely") as hits,
            pytest.raises(sqlite3.OperationalError),
        ):
            database._exec_migration_safely(conn, sql_file)
        assert hits[0] >= 1
    finally:
        conn.close()


def test_run_migrations_does_not_record_a_failed_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "999_bad.sql").write_text(
        "INSERT INTO no_such_table VALUES (1);", encoding="utf-8"
    )
    monkeypatch.setattr(database, "MIGRATIONS_DIR", migrations)
    db_path = tmp_path / "bad.db"

    with (
        pytest.raises(sqlite3.OperationalError),
        _state_probe.count_calls(database, "_exec_migration_safely") as hits,
    ):
        database.run_migrations(db_path)
    assert hits[0] >= 1
    assert _user_version(db_path) == 0, (
        f"a failed migration was recorded as applied (user_version={_user_version(db_path)})"
    )


def test_migration_rerun_tolerates_a_duplicate_column(tmp_path: Path) -> None:
    """The documented idempotent-rerun case must still be tolerated (positive)."""
    path = corpus_state.interrupted_migration_sqlite(tmp_path)
    with _state_probe.count_calls(database, "_exec_migration_safely") as hits:
        database.run_migrations(path)
    assert hits[0] >= 1
    assert _user_version(path) == _latest_migration_version()

    conn = database.get_connection(path)
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(review_reports)")}
    finally:
        conn.close()
    assert "client_id" in cols


# ── Well-formed hostile SQLite files must yield a usable schema ─────────────


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(corpus_state.zero_byte_sqlite, id="zero-byte"),
        pytest.param(corpus_state.missing_table_sqlite, id="missing-table"),
    ],
)
def test_init_database_makes_a_usable_schema(build: Callable[[Path], Path], tmp_path: Path) -> None:
    path = build(tmp_path)
    with _state_probe.count_calls(database, "run_migrations") as hits:
        database.init_database(path)
    assert hits[0] >= 1
    assert _user_version(path) == _latest_migration_version()
    assert {"clients", "prompt_versions", "playbook_versions"} <= _table_names(path)


# ── A corrupt shared database must fail cleanly, not crash _init ────────────


def test_corrupt_shared_database_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _state_probe.prepare_state(monkeypatch, tmp_path)
    state.db_path.write_bytes(corpus_state.corrupt_sqlite(tmp_path).read_bytes())
    result = _state_probe.run_cli(["config", "get", "privacy.tier"])
    _state_probe.assert_clean_failure(result, frozenset({1, 5}))


# ── Retrieval-owned failures assert the retrieval classes (codes 40/41) ────


def test_retrieval_missing_index_is_index_not_found(tmp_path: Path) -> None:
    engine = RetrievalEngine(tmp_path / "absent.db")
    with pytest.raises(IndexNotFoundError) as exc:
        engine.retrieve(RetrievalQuery(query_text="x", method="sparse"))
    assert isinstance(exc.value, RetrievalError)


def test_retrieval_marked_corrupt_index_is_index_corrupt(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    storage = RetrievalStorage(db_path)
    storage.create_schema()
    conn = storage.conn
    conn.execute(
        "INSERT INTO index_meta (document_id, document_path, index_status) "
        "VALUES ('doc', 'path', 'corrupt')"
    )
    conn.commit()
    storage.close()

    engine = RetrievalEngine(db_path)
    with pytest.raises(IndexCorruptError) as exc:
        engine.retrieve(RetrievalQuery(query_text="x", method="sparse"))
    assert isinstance(exc.value, RetrievalError)


def test_damaged_index_file_is_reported_as_index_corrupt(tmp_path: Path) -> None:
    """Issue #118: a damaged index must be reported as corrupt, not traceback.

    ``RetrievalStorage.get_index_meta`` used to catch only
    ``sqlite3.OperationalError``; a damaged file raises ``sqlite3.DatabaseError``
    from the connection PRAGMA first, so the caller got a raw traceback. The read
    now distinguishes "not indexed" (``None``) from "exists but unreadable"
    (``IndexCorruptError``).
    """
    path = corpus_state.corrupt_sqlite(tmp_path)
    with pytest.raises(IndexCorruptError) as exc:
        RetrievalStorage(path).get_index_meta()
    assert isinstance(exc.value, RetrievalError)


# ── Negative control: the oracle must be able to fail ───────────────────────


def test_negative_control_sqlite_oracle_rejects_a_clean_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A healthy database is NOT a failure; the oracle must reject it."""
    _state_probe.prepare_state(monkeypatch, tmp_path)
    result = _state_probe.run_cli(["config", "get", "privacy.tier"])
    assert result.exit_code == 0, f"{result.exit_code}: {result.output!r}"
    with pytest.raises(AssertionError):
        _state_probe.assert_clean_failure(result, frozenset({1, 5}))
