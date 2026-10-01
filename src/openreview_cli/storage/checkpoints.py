"""Small transactional store for the opt-in clause checkpoints."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from openreview_cli.review.checkpoints import SCHEMA_VERSION, CheckpointError
from openreview_cli.storage.database import init_database, transaction

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True)
class CheckpointRun:
    identity: str
    session_id: str


class CheckpointStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        try:
            init_database(db_path)
        except (OSError, sqlite3.Error):
            raise CheckpointError("checkpoint storage unavailable") from None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with transaction(self.db_path) as conn:
                yield conn
        except (OSError, sqlite3.Error):
            raise CheckpointError("checkpoint storage unavailable") from None

    def get_or_create(
        self, identity: str, document_hash: str, session_id: str | None = None
    ) -> CheckpointRun:
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO review_checkpoint_runs "
                "(identity_digest,document_hash,schema_version,session_id,status) VALUES (?,?,?,?,?)",
                (
                    identity,
                    document_hash,
                    SCHEMA_VERSION,
                    session_id or f"review:{uuid.uuid4()}",
                    "active",
                ),
            )
            row = conn.execute(
                "SELECT session_id FROM review_checkpoint_runs WHERE identity_digest=?", (identity,)
            ).fetchone()
            if session_id is not None and row["session_id"] != session_id:
                raise CheckpointError("checkpoint session mismatch")
            return CheckpointRun(identity, row["session_id"])

    def get_step(
        self, run: str, clause: str, clause_hash: str, category: str, step: str
    ) -> bytes | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT payload FROM review_checkpoint_steps WHERE run_id=? "
                "AND clause_id=? AND clause_hash=? AND category_id=? AND step=? "
                "AND status='completed'",
                (run, clause, clause_hash, category, step),
            ).fetchone()
            payload = row["payload"] if row else None
            # SQLite's BLOB affinity still permits TEXT/integer values. Never
            # coerce integers to bytes (allocation) or treat corrupt rows as hits.
            return payload if isinstance(payload, bytes) else None

    def set_step(
        self,
        run: str,
        clause: str,
        clause_hash: str,
        category: str,
        step: str,
        status: str,
        payload: bytes | None = None,
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO review_checkpoint_steps "
                "(run_id,clause_id,clause_hash,category_id,step,status,payload) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id,clause_id,step) DO UPDATE SET "
                "clause_hash=excluded.clause_hash,category_id=excluded.category_id, "
                "status=excluded.status,payload=excluded.payload,updated_at=CURRENT_TIMESTAMP",
                (run, clause, clause_hash, category, step, status, payload),
            )

    def finish(self, run: str, complete: bool) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE review_checkpoint_runs SET status=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE identity_digest=?",
                ("completed" if complete else "incomplete", run),
            )

    def force(self, run: str) -> int:
        with self._transaction() as conn:
            count = conn.execute(
                "DELETE FROM review_checkpoint_steps WHERE run_id=?", (run,)
            ).rowcount
            conn.execute(
                "UPDATE review_checkpoint_runs SET status='active',updated_at=CURRENT_TIMESTAMP "
                "WHERE identity_digest=?",
                (run,),
            )
            return count

    def clear_document(self, document_hash: str) -> tuple[int, int]:
        with self._transaction() as conn:
            steps = conn.execute(
                "SELECT COUNT(*) FROM review_checkpoint_steps WHERE run_id IN "
                "(SELECT identity_digest FROM review_checkpoint_runs WHERE document_hash=?)",
                (document_hash,),
            ).fetchone()[0]
            runs = conn.execute(
                "DELETE FROM review_checkpoint_runs WHERE document_hash=?", (document_hash,)
            ).rowcount
            return runs, steps
