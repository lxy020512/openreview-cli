"""W8b chaos suite: resource limits.

Three runtime resources the product does not control, each driven through the real
CLI or the real storage helper:

1. **A read-only config directory.** ``config set`` must fail with a code from
   ``errors.py`` and a message that names the failing component. It does not: it
   exits 1 with an unhandled ``PermissionError`` and **empty output** (RT-048).
2. **A read-only data directory**, and a fresh data directory in which the
   database cannot be created. Every command dies inside ``_init`` with an
   unhandled ``sqlite3.OperationalError`` and empty output (RT-047).
3. **A failing write** injected through ``tests/helpers/faults.py``'s
   ``failing_write`` row: the write must roll back, leave no partial row and leave
   the database usable.

The read-only cases skip themselves when the suite runs as root, because
``chmod`` does not restrict root (the same guard the plan's W8b names).

The memory case carries ``@pytest.mark.memory`` and is a no-op unless the run was
selected with ``-m memory``, so ``-m memory`` stays standalone (plan section 8:
a memory case is never folded into a combined run).
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from openreview_cli.app import app
from openreview_cli.storage.database import init_database, transaction
from tests.chaos import _w8_probe as w8
from tests.helpers import faults

pytestmark = pytest.mark.chaos

TRACEBACK_TOKEN = w8.TRACEBACK_TOKEN

# chmod does not restrict root, so the read-only cases are meaningless there.
NOT_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="chmod does not restrict root; the read-only case is vacuous"
)


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> w8.State:
    """An isolated XDG tree with a real migrated database (all four XDG roots).

    Built from the ``tmp_path`` + ``monkeypatch`` fixtures via
    ``_w8_probe.prepare_state`` (the same shape W8a consumes), so neither the
    in-process CLI nor any subprocess can reach the developer's real
    ``~/.config`` / ``~/.local/share`` / ``~/.local/state`` trees.
    """
    return w8.prepare_state(monkeypatch, tmp_path)


def invoke(args: list[str]) -> Result:
    """Invoke the real Typer entry with the isolated XDG env already in place."""
    return CliRunner().invoke(app, args)


def assert_clean_failure(
    *, exit_code: int, output: str, allowed: frozenset[int], token: str
) -> None:
    """The W8b oracle: an allowed exit code, a message, and no raw traceback.

    Module-level and primitive-driven on purpose, so the negative control can
    prove it can fail (plan section 9.3 rule 1).
    """
    assert exit_code in allowed, f"exit={exit_code} (allowed {sorted(allowed)})"
    assert TRACEBACK_TOKEN not in output, f"a raw traceback reached the user: {output!r}"
    assert token in output, f"the message does not name the failing component: {output!r}"


# ── A read-only config directory ────────────────────────────────────────────


def _assert_not_writable(path: Path) -> None:
    """Reachability: prove the chmod actually removed our write permission."""
    assert not os.access(path, os.W_OK), f"the read-only guard did not take: {path}"


@NOT_ROOT
def test_config_set_on_a_read_only_config_dir_is_a_clean_config_error(
    state: w8.State,
) -> None:
    """Expected: exit 5 (or 1) with a message naming the failing component."""
    with w8.read_only_dir(state.config_dir):
        _assert_not_writable(state.config_dir)
        result = invoke(["config", "set", "privacy.tier", "maximum"])

    assert_clean_failure(
        exit_code=result.exit_code,
        output=result.output,
        allowed=frozenset({1, 5}),
        token="auth.json",
    )
    assert "Config error" in result.output, result.output


@NOT_ROOT
def test_config_set_on_a_read_only_config_dir_is_a_clean_config_error_and_names_auth_json(
    state: w8.State,
) -> None:
    """The auth write in ``_init`` is guarded, not left to escape.

    ``_init`` calls ``ensure_auth(config_dir)`` before the command, and on a
    fresh tree ``write_auth`` opens ``auth.json`` with ``os.open``
    (``auth.py:72``), raising ``PermissionError`` on a read-only config dir.
    The new ``except OSError`` routes that to ``config_error`` — whose message
    names the file that could not be written.
    """
    with w8.read_only_dir(state.config_dir):
        _assert_not_writable(state.config_dir)
        result = invoke(["config", "set", "privacy.tier", "maximum"])

    assert result.exit_code == 5, result.output
    assert TRACEBACK_TOKEN not in result.output, result.output
    assert CLEAN_CONFIG_TOKEN in result.output, result.output
    assert "auth.json" in result.output, result.output


# ── A read-only / unwritable data directory ─────────────────────────────────


@NOT_ROOT
def test_a_read_only_data_dir_is_a_clean_error(state: w8.State) -> None:
    """Expected: every command fails with a code from ``errors.py`` and a message."""
    with w8.read_only_dir(state.data_dir):
        _assert_not_writable(state.data_dir)
        result = invoke(["client", "add", "cx", "Client X"])

    assert_clean_failure(
        exit_code=result.exit_code,
        output=result.output,
        allowed=frozenset({1, 5}),
        token="database",
    )
    assert "Error" in result.output, result.output


@NOT_ROOT
def test_a_read_only_existing_database_is_a_named_storage_error(
    state: w8.State,
) -> None:
    """``_init`` routes every ``sqlite3.DatabaseError`` to the named failure.

    ``init_database`` runs before any subcommand and the first thing
    ``get_connection`` does is a WAL journal-mode change, which needs to create
    ``-wal``/``-shm`` files in the directory. On a read-only data dir SQLite
    raises ``OperationalError("attempt to write a readonly database")`` — now a
    clean exit 1 whose message names the database path.
    """
    with w8.read_only_dir(state.data_dir):
        _assert_not_writable(state.data_dir)
        result = invoke(["client", "add", "cx", "Client X"])

    assert result.exit_code == 1, result.output
    assert TRACEBACK_TOKEN not in result.output, result.output
    assert "cannot open database" in result.output, result.output
    assert str(state.db_path) in result.output, result.output


@NOT_ROOT
def test_a_fresh_unwritable_data_dir_is_a_named_storage_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The database cannot even be created: the same named, exit-1 failure."""
    state = w8.prepare_state(monkeypatch, tmp_path, migrate=False)
    with w8.read_only_dir(state.data_dir):
        _assert_not_writable(state.data_dir)
        result = invoke(["client", "add", "cy", "Client Y"])

    assert result.exit_code == 1, result.output
    assert TRACEBACK_TOKEN not in result.output, result.output
    assert "cannot open database" in result.output, result.output
    assert str(state.db_path) in result.output, result.output
    assert not state.db_path.exists(), "nothing should be created on a read-only tree"


# ── A read-only XDG state / log directory ───────────────────────────────────


@NOT_ROOT
def test_a_read_only_state_log_dir_only_degrades_logging(state: w8.State) -> None:
    """``_init``'s log/state step is non-fatal: only file logging stops.

    The rotating handler cannot be created under a read-only ``XDG_STATE_HOME``;
    the warning goes to the stderr handler and the command still runs and writes.
    """
    with w8.read_only_dir(state.log_dir):
        _assert_not_writable(state.log_dir)
        result = invoke(["client", "add", "cz", "Client Z"])

    assert result.exit_code == 0, result.output
    assert TRACEBACK_TOKEN not in result.output, result.output
    assert TRACEBACK_TOKEN not in result.stderr, result.stderr
    assert "file logging disabled" in result.stderr, result.stderr
    assert not (state.log_dir / "openreview.log").exists(), "the log file must not exist"
    assert w8.count_rows(state.db_path, "clients") == 1


# ── The second read-only-write seam: ``config set`` itself ──────────────────


@NOT_ROOT
def test_config_set_on_a_read_only_config_dir_names_the_error_with_auth_present(
    state: w8.State,
) -> None:
    """With ``auth.json`` present, ``_init``'s auth write early-returns.

    The failure then comes from ``set_config_value``'s backup + write
    (``loader.py:327,337``), which raise ``OSError`` on a read-only config dir.
    ``config_set`` must route that to ``config_error`` (exit 5) and leave
    ``config.yml`` untouched.
    """
    state.auth_path.write_text("{}", encoding="utf-8")
    state.auth_path.chmod(0o600)
    before = state.config_path.read_text(encoding="utf-8")

    with w8.read_only_dir(state.config_dir):
        _assert_not_writable(state.config_dir)
        result = invoke(["config", "set", "privacy.tier", "maximum"])

    assert result.exit_code == 5, result.output
    assert TRACEBACK_TOKEN not in result.output, result.output
    assert "Config error" in result.output, result.output
    assert state.config_path.read_text(encoding="utf-8") == before


# ── A failing write through the W0 fault table ──────────────────────────────


def _insert_client(conn: sqlite3.Connection, client_id: str) -> None:
    conn.execute("INSERT INTO clients (id, name) VALUES (?, ?)", (client_id, client_id))


def _clients(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [str(r[0]) for r in conn.execute("SELECT id FROM clients ORDER BY id")]
    finally:
        conn.close()


def test_a_failing_write_rolls_back_and_leaves_the_database_usable(tmp_path: Path) -> None:
    """``transaction()`` must roll back the first write when the second fails."""
    db_path = tmp_path / "openreview.db"
    init_database(db_path)
    # Let the first write through, fail the second: ``at_call=1`` lets call 1
    # pass and fires on call 2. The SAME wrapper must gate both calls — a fresh
    # ``apply`` per call resets the counter, so the fault would never fire (the
    # regression this pins).
    fault = dataclasses.replace(faults.FAULTS["failing_write"], at_call=1)
    write = faults.apply(fault, _insert_client)

    with (
        pytest.raises(OSError, match="failing_write fault injected"),
        transaction(db_path) as conn,
    ):
        write(conn, "a")
        write(conn, "b")

    assert _clients(db_path) == [], "the rollback left a partial write"
    assert w8.integrity_check(db_path) == "ok"
    # The database is still usable afterwards.
    with transaction(db_path) as conn:
        _insert_client(conn, "c")
    assert _clients(db_path) == ["c"]


def test_the_same_write_without_the_fault_commits_both_rows(tmp_path: Path) -> None:
    """Control: the rollback above is caused by the fault, not by the write."""
    db_path = tmp_path / "openreview.db"
    init_database(db_path)

    with transaction(db_path) as conn:
        _insert_client(conn, "a")
        _insert_client(conn, "b")

    assert _clients(db_path) == ["a", "b"]


def test_a_failing_write_at_the_top_of_a_transaction_writes_nothing(tmp_path: Path) -> None:
    """The fault's own firing shape: ``at_call=0`` fires before the first write."""
    db_path = tmp_path / "openreview.db"
    init_database(db_path)

    with pytest.raises(OSError), transaction(db_path) as conn:
        faults.apply(faults.FAULTS["failing_write"], _insert_client)(conn, "a")

    assert _clients(db_path) == []
    assert w8.integrity_check(db_path) == "ok"


# ── Memory (standalone; excluded from combined runs) ────────────────────────


@pytest.mark.memory
def test_the_failing_write_path_stays_under_the_memory_floor(
    tmp_path: Path,
    memory_tracker: None,
    request: pytest.FixtureRequest,
) -> None:
    """A 5 MB payload held across a failing write must not breach the 110 MB floor.

    ``memory_tracker`` asserts the constitutional floor; the payload makes the
    assertion non-trivial. Guarded so the case only runs under ``-m memory``: the
    repo keeps memory cases out of combined runs (plan section 8).
    """
    expression = request.config.getoption("-m")
    if not isinstance(expression, str) or "memory" not in expression:
        pytest.skip("standalone memory case; run with `uv run pytest -m memory tests/chaos -q`")

    db_path = tmp_path / "openreview.db"
    init_database(db_path)
    payload = "x" * (5 * 1024 * 1024)  # held live across the failing write

    def _write_payload(conn: sqlite3.Connection, value: str) -> None:
        conn.execute("INSERT INTO clients (id, name) VALUES ('p', ?)", (value,))

    wrapped = faults.apply(faults.FAULTS["failing_write"], _write_payload)
    with (
        pytest.raises(OSError, match="failing_write fault injected"),
        transaction(db_path) as conn,
    ):
        _insert_client(conn, "first")
        wrapped(conn, payload)

    assert _clients(db_path) == [], "the failing write left a partial row"
    assert w8.integrity_check(db_path) == "ok"
    assert len(payload) == 5 * 1024 * 1024


# ── Negative control (anti-vacuity, plan section 9.3) ───────────────────────


# The oracle's token is the product's real clean-failure prefix. ``config_error``
# prints ``Config error: <message>`` and exits 5 (``errors.py:46-48``), so that
# is the shape the oracle accepts; a message lacking it (empty, a leaked
# exception repr, a raw traceback) must still be rejected.
CLEAN_CONFIG_TOKEN = "Config error"


def test_negative_control_clean_failure_oracle_rejects_an_empty_message() -> None:
    """The oracle must fail on the very shape RT-047 and RT-048 produce."""
    with pytest.raises(AssertionError, match="does not name the failing component"):
        assert_clean_failure(
            exit_code=1, output="", allowed=frozenset({1, 5}), token=CLEAN_CONFIG_TOKEN
        )


def test_negative_control_clean_failure_oracle_rejects_a_raw_traceback() -> None:
    """A traceback in the output must fail even when the exit code is allowed."""
    forge = f"Traceback (most recent call last):\n  ...\n{CLEAN_CONFIG_TOKEN}: boom\n"
    with pytest.raises(AssertionError, match="raw traceback"):
        assert_clean_failure(
            exit_code=1, output=forge, allowed=frozenset({1, 5}), token=CLEAN_CONFIG_TOKEN
        )


def test_negative_control_clean_failure_oracle_rejects_a_leaked_exception_repr() -> None:
    """A non-empty, traceback-free message that leaks an exception must fail.

    This is the discrimination the token buys: ``Error`` alone would have
    accepted this leak, while ``Config error`` (the component-naming prefix) does
    not.
    """
    leak = "PermissionError(13, 'Permission denied')\n"
    with pytest.raises(AssertionError, match="does not name the failing component"):
        assert_clean_failure(
            exit_code=1, output=leak, allowed=frozenset({1, 5}), token=CLEAN_CONFIG_TOKEN
        )


def test_negative_control_clean_failure_oracle_accepts_the_expected_shape() -> None:
    """Positive control: the oracle must not be unconditionally failing.

    The message is the product's real clean-failure shape (``config_error`` at
    ``errors.py:46-48``): the ``Config error`` prefix, exit 5, no traceback.
    """
    assert_clean_failure(
        exit_code=5,
        output="Config error: cannot write config.yml (permission denied)\n",
        allowed=frozenset({1, 5}),
        token=CLEAN_CONFIG_TOKEN,
    )
