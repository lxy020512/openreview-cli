"""Leak-capture harness: collect every output surface, then assert absence.

Owned by W0. W5 and W6 consume this to prove that a synthetic secret reaches
none of stdout, stderr, a log file or a SQLite text column.

``Captured`` is deliberately NOT frozen: ``capture()`` populates it as it
exits and tests fold in a ``CliRunner`` result afterwards, so the plan's
``frozen=True`` is over-specified and would break that usage pattern.
"""

from __future__ import annotations

import io
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Captured:
    """Every place a leaked value could surface, keyed by surface name."""

    stdout: str = ""
    stderr: str = ""
    logs: dict[str, str] = field(default_factory=dict)  # log file path -> text
    db_text: dict[str, list[str]] = field(default_factory=dict)  # "<table>.<column>" -> values

    def surfaces(self) -> dict[str, str]:
        """Return surface name -> text: stdout, stderr, each log, each DB column."""
        out: dict[str, str] = {"stdout": self.stdout, "stderr": self.stderr}
        out.update(self.logs)
        for column, values in self.db_text.items():
            out[column] = "\n".join(values)
        return out

    def find_all(self, needle: str) -> dict[str, int]:
        """Return surface -> occurrence count, only for surfaces where count > 0."""
        found: dict[str, int] = {}
        for name, text in self.surfaces().items():
            count = text.count(needle)
            if count > 0:
                found[name] = count
        return found

    def assert_absent(self, needle: str) -> None:
        """Raise ``AssertionError`` naming every surface that contains ``needle``."""
        hits = self.find_all(needle)
        if hits:
            named = ", ".join(sorted(hits))
            raise AssertionError(f"{needle!r} found on surface(s): {named}")

    def add_cli_result(self, result: Any) -> None:
        """Fold a typer/click ``CliRunner`` result into the stdout/stderr surfaces."""
        self.stdout += result.stdout or ""
        self.stderr += result.stderr or ""


def _text_affinity(declared_type: str) -> bool:
    """Return True when a SQLite declared type has TEXT affinity."""
    upper = declared_type.upper()
    return any(token in upper for token in ("CHAR", "CLOB", "TEXT"))


def _read_db_text(db_path: Path, out: dict[str, list[str]]) -> None:
    """Read every non-null value of every TEXT-affinity column into ``out``."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = [
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            for column in conn.execute(f"PRAGMA table_info({table})"):
                name = str(column[1])
                if not _text_affinity(str(column[2])):
                    continue
                values = [
                    str(row[0])
                    for row in conn.execute(f"SELECT {name} FROM {table} WHERE {name} IS NOT NULL")
                ]
                if values:
                    out[f"{table}.{name}"] = values
    finally:
        conn.close()


@contextmanager
def capture(
    log_dir: Path | None = None,
    db_path: Path | None = None,
) -> Iterator[Captured]:
    """Redirect stdout/stderr into buffers; harvest log files and DB text on exit."""
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    captured = Captured()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out_buf, err_buf
    try:
        yield captured
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        captured.stdout = out_buf.getvalue()
        captured.stderr = err_buf.getvalue()
        if log_dir is not None:
            for log_path in sorted(log_dir.glob("**/*.log")):
                captured.logs[str(log_path)] = log_path.read_text(
                    encoding="utf-8", errors="replace"
                )
        if db_path is not None:
            _read_db_text(db_path, captured.db_text)


def assert_detects(captured: Captured, planted: str) -> None:
    """Positive control: raise ``AssertionError`` if ``planted`` is found nowhere."""
    if not captured.find_all(planted):
        raise AssertionError(f"positive control failed: {planted!r} absent from every surface")
