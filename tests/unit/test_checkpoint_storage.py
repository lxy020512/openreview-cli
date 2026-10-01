from pathlib import Path
from typing import Any

import pytest

from openreview_cli.review.checkpoints import CheckpointError
from openreview_cli.storage.checkpoints import CheckpointStore
from openreview_cli.storage.database import (
    MIGRATIONS_DIR,
    _exec_migration_safely,
    get_connection,
    init_database,
)


def test_run_session_steps_force_clear_and_unrelated_data(tmp_path: Path) -> None:
    db = tmp_path / "openreview.db"
    store = CheckpointStore(db)
    run = store.get_or_create("identity", "filehash", "session-original")
    assert store.get_or_create("identity", "filehash").session_id == "session-original"
    with pytest.raises(CheckpointError, match="checkpoint session mismatch"):
        store.get_or_create("identity", "filehash", "different")
    store.set_step("identity", "c1", "chash", "term", "extraction", "running")
    assert store.get_step("identity", "c1", "chash", "term", "extraction") is None
    store.set_step("identity", "c1", "chash", "term", "extraction", "completed", b"cipher")
    assert store.get_step("identity", "c1", "chash", "term", "extraction") == b"cipher"
    assert store.get_step("identity", "c1", "wrong", "term", "extraction") is None
    assert store.force("identity") == 1
    assert store.get_or_create("identity", "filehash").session_id == run.session_id
    store.get_or_create("old", "old-file-version")
    with get_connection(db) as conn:
        conn.execute("CREATE TABLE unrelated (value TEXT)")
        conn.execute("INSERT INTO unrelated VALUES ('preserve')")
        conn.commit()
    assert store.clear_document("filehash") == (1, 0)
    with get_connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_checkpoint_runs").fetchone()[0] == 1
        assert conn.execute("SELECT value FROM unrelated").fetchone()[0] == "preserve"


def test_migration_upgrade_repeat_and_fail_closed(tmp_path: Path) -> None:
    db = tmp_path / "openreview.db"
    # Build the real pre-feature schema, without creating checkpoint tables.
    with get_connection(db) as conn:
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if int(migration.stem.split("_")[0]) <= 15:
                _exec_migration_safely(conn, migration)
        conn.execute("PRAGMA user_version=15")
        conn.execute("CREATE TABLE legacy_test_data (value TEXT)")
        conn.execute("INSERT INTO legacy_test_data VALUES ('preserve')")
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name='review_checkpoint_runs'"
            ).fetchone()
            is None
        )
        conn.commit()
    init_database(db)
    init_database(db)
    with get_connection(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 16
        assert conn.execute("SELECT value FROM legacy_test_data").fetchone()[0] == "preserve"
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='review_checkpoint_runs'"
        ).fetchone()
    broken = tmp_path / "not-a-db"
    broken.write_bytes(b"SENSITIVE-NOT-A-DATABASE")
    with pytest.raises(CheckpointError, match="checkpoint storage unavailable") as exc:
        CheckpointStore(broken)
    assert "SENSITIVE" not in str(exc.value)


def test_store_transaction_rolls_back_all_rows_on_constraint_failure(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "openreview.db")
    store.get_or_create("run", "hash")
    with (
        pytest.raises(CheckpointError, match="checkpoint storage unavailable"),
        store._transaction() as conn,
    ):
        conn.execute(
            "INSERT INTO review_checkpoint_steps "
            "(run_id,clause_id,clause_hash,category_id,step,status,payload) "
            "VALUES ('run','c1','hash','term','extraction','completed',X'00')"
        )
        # A completed row without a payload is prohibited; whole transaction rolls back.
        conn.execute(
            "INSERT INTO review_checkpoint_steps "
            "(run_id,clause_id,clause_hash,category_id,step,status,payload) "
            "VALUES ('run','c2','hash','term','qa','completed',NULL)"
        )
    with get_connection(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_checkpoint_steps").fetchone()[0] == 0


@pytest.mark.parametrize("bad_payload", ["not-a-blob", 17])
def test_wrong_sqlite_payload_type_is_miss_without_coercion(
    tmp_path: Path, bad_payload: Any
) -> None:
    store = CheckpointStore(tmp_path / "db.sqlite")
    store.get_or_create("run", "hash")
    with get_connection(store.db_path) as conn:
        conn.execute(
            "INSERT INTO review_checkpoint_steps "
            "(run_id,clause_id,clause_hash,category_id,step,status,payload) "
            "VALUES ('run','c1','hash','term','qa','completed',?)",
            (bad_payload,),
        )
        conn.commit()
    assert store.get_step("run", "c1", "hash", "term", "qa") is None
