import logging
import re
import sqlite3
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_WAL_RETRY_ATTEMPTS = 5
_WAL_RETRY_DELAY_S = 0.2


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Set WAL, retrying only the "database is locked" case.

    SQLite does not invoke the busy handler for a journal-mode change while
    another connection holds a transaction, so this is a bounded retry, not a
    timeout. Every other OperationalError (readonly, unable to open) raises on
    the first attempt.
    """
    for attempt in range(_WAL_RETRY_ATTEMPTS):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == _WAL_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_WAL_RETRY_DELAY_S * (attempt + 1))
        else:
            return


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    _enable_wal(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    run_migrations(db_path)


def run_migrations(db_path: Path) -> None:
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            num = int(sql_file.stem.split("_")[0])
            if num > version:
                conn.execute("BEGIN")
                try:
                    _exec_migration_safely(conn, sql_file)
                    conn.execute(f"PRAGMA user_version = {num}")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
    finally:
        conn.close()


def _skip_comment(sql: str, start: int) -> int:
    """Return the index just past the comment opening at *start*."""
    if sql.startswith("--", start):
        end = sql.find("\n", start)
        return len(sql) if end == -1 else end
    end = sql.find("*/", start + 2)
    return len(sql) if end == -1 else end + 2


def _skip_quoted(sql: str, start: int) -> int:
    """Return the index just past a quoted region (doubled-quote escapes honoured)."""
    quote = sql[start]
    n = len(sql)
    i = start + 1
    while i < n:
        if sql[i] == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _skip_region(sql: str, start: int) -> int:
    """Return the index just past the comment / string / identifier at *start*.

    Identifiers may be ``"..."``, `` `...` `` (doubled-quote escapes) or ``[...]``
    (closed by the first ``]``); comments are ``--`` to end of line or ``/* */``.
    """
    if sql.startswith(("--", "/*"), start):
        return _skip_comment(sql, start)
    if sql[start] == "[":
        close = sql.find("]", start + 1)
        return len(sql) if close == -1 else close + 1
    return _skip_quoted(sql, start)


def _apply_word(
    word: str, leading: list[str], is_trigger: bool, depth: int, case_depth: int
) -> tuple[bool, int, int]:
    """Update trigger detection from a statement's leading keywords / body words."""
    upper = word.upper()
    if not is_trigger and len(leading) < 3:
        leading.append(upper)
        return leading[0] == "CREATE" and "TRIGGER" in leading, depth, case_depth
    if is_trigger:
        if upper == "CASE":
            case_depth += 1
        elif upper == "BEGIN":
            depth += 1
        elif upper == "END":
            if case_depth > 0:
                case_depth -= 1
            else:
                depth -= 1
    return is_trigger, depth, case_depth


def iter_sql_statements(sql: str) -> Iterator[str]:
    """Split a SQL script into complete, executable statements.

    A naive ``str.split(";")`` breaks a statement whenever a ``;`` appears in a
    string literal, a quoted identifier (``"..."``, `` `...` `` or ``[...]``), a
    comment, or a ``CREATE TRIGGER`` body. This scanner tracks those regions and
    yields each non-empty statement stripped. Trigger bodies are detected from
    the leading keywords (``CREATE [TEMP|TEMPORARY] TRIGGER``); inner ``;``
    separators are only skipped while BEGIN/END depth is positive, and a
    ``CASE ... END`` inside the body does not close the trigger.
    """
    n = len(sql)
    i = 0
    buf: list[str] = []
    leading: list[str] = []
    is_trigger = False
    depth = 0
    case_depth = 0

    while i < n:
        ch = sql[i]

        if sql.startswith(("--", "/*"), i) or ch in ("'", '"', "`") or ch == "[":
            end = _skip_region(sql, i)
            buf.append(sql[i:end])
            i = end
            continue

        if ch.isalpha() or ch == "_":
            match = _WORD_RE.match(sql, i)
            assert match is not None
            word = match.group(0)
            is_trigger, depth, case_depth = _apply_word(
                word, leading, is_trigger, depth, case_depth
            )
            buf.append(word)
            i += len(word)
            continue

        if ch == ";":
            if not (is_trigger and depth > 0):
                stmt = "".join(buf).strip()
                if stmt:
                    yield stmt
                buf.clear()
                leading.clear()
                is_trigger = False
                depth = 0
                case_depth = 0
            else:
                buf.append(ch)
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        yield tail


def _exec_migration_safely(conn: sqlite3.Connection, sql_file: Path) -> None:
    """Execute a migration script, tolerating idempotent re-runs.

    Some migration scripts (e.g. 011) use `ALTER TABLE ADD COLUMN` which is not
    idempotent in SQLite. If a previous run partially applied the migration
    (e.g. test fixture left the column but user_version wasn't bumped), a
    plain re-run would fail with "duplicate column name". Split the script into
    complete statements (``iter_sql_statements``) and execute them
    individually, skipping those that fail with a "duplicate column" /
    "already exists" / "no such column" OperationalError. Any other
    OperationalError is a real failure and is re-raised.
    """
    _logger = logging.getLogger(__name__)
    text = sql_file.read_text()
    for stmt in iter_sql_statements(text):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "already exists" in msg or "no such column" in msg:
                _logger.warning(
                    "Migration %s: skipped statement (schema already matches): %.80s",
                    sql_file.name,
                    stmt,
                )
                continue
            raise
