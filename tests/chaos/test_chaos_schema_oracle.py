"""Regression coverage for the W8 database schema oracle."""

import sqlite3
from pathlib import Path

import pytest

from openreview_cli.storage.database import init_database
from tests.chaos import _w8_probe as w8

pytestmark = pytest.mark.chaos


def test_schema_oracle_accepts_a_fresh_migrated_database(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    init_database(db_path)

    w8.assert_db_consistent(db_path)


@pytest.mark.parametrize("table", ("review_checkpoint_runs", "review_checkpoint_steps"))
def test_schema_oracle_rejects_a_missing_checkpoint_table(tmp_path: Path, table: str) -> None:
    db_path = tmp_path / "missing.db"
    init_database(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"DROP TABLE {table}")
        conn.commit()
    finally:
        conn.close()

    # Isolate the table check from the version check, including before the fix.
    with pytest.raises(AssertionError, match=f"schema is missing tables:.*{table}"):
        w8.assert_db_consistent(db_path, expect_version=16)
