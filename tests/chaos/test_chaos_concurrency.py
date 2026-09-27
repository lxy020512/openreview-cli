"""W8c chaos suite: concurrency.

Three concurrency surfaces, two of which the brief named as candidates and one
that turned out to be the real defect. Everything below is measured, not assumed:

1. **The process-global cloud-call counter** (``gateway/models.py:142-148``) is a
   plain ``_total_cloud_calls += 1`` with no lock. The brief called a lost update
   a "real candidate finding"; it does **not** reproduce on CPython 3.12. The
   read-modify-write is ``LOAD_GLOBAL / LOAD_CONST / BINARY_OP / STORE_GLOBAL``
   with no eval-breaker checkpoint between the load and the store, so the GIL
   makes it atomic: 8 threads x 20k increments (also tried 64 x 50k at a 1 ns
   switch interval) lost zero updates every time. The exactness assertion below
   pins that; the ``test_negative_control_*`` pair proves the oracle would catch a
   lost update if one ever appeared (a deliberate read/checkpoint/write control
   loses ~85% of them).

2. **The PII-before-egress flag** (``router.py:70-90``) is likewise one
   process-global, documented as "per-operation evidence ... callers reset it at
   operation start". That contract only holds if a process runs one operation at a
   time: two concurrent operations sharing the process each reset the flag at
   start, so one operation's reset clears the other's successful strip mark and
   the other's egress gate reads ``False`` (a spurious ``PIIUnavailableError``).
   The forced-interleaving case below pins that.

3. **Multi-process writers on one database.** Two and four real CLI processes
   against a single migrated database are fast (~1-2 s warm) and clean: WAL stays
   WAL, every write lands, no locked-database crash — because ``sqlite3.connect``
   defaults to a 5 s busy timeout even though the repo never sets ``busy_timeout``
   (so the "busy_timeout is unset" sharp edge is real but *not* the failure here).
   The real defect is on a **cold** database: when a second CLI start races while
   another connection holds a write transaction on a not-yet-WAL file, ``get_connection``
   runs ``PRAGMA journal_mode=WAL`` (``storage/database.py:12``) and SQLite, which
   does **not** invoke the busy handler for a journal-mode change, raises
   ``sqlite3.OperationalError('database is locked')`` immediately (0.00 s, even with
   ``busy_timeout=2000``). ``app.py:264`` calls that unguarded before any subcommand,
   so the process exits 1 with **empty output** and its write is lost. An organic
   4-process cold start hit this on 1 of 3 trials (rows=3, expected 4); the case
   below reproduces the identical stack deterministically by holding the write
   transaction in the test.

Every subprocess is spawned through ``_w8_probe.isolated_env``, which pins all four
XDG roots under the per-test ``tmp_path``; no case can reach the developer's real
``~/.config``, ``~/.local/share`` or ``~/.local/state``.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from openreview_cli.app import app
from openreview_cli.gateway.models import (
    get_total_cloud_calls,
    record_cloud_call,
    reset_total_cloud_calls,
)
from openreview_cli.gateway.router import (
    mark_pii_available,
    pii_available,
    reset_pii_available,
)
from openreview_cli.storage.database import get_connection, init_database
from tests.chaos import _w8_probe as w8

pytestmark = pytest.mark.chaos

TRACEBACK_TOKEN = w8.TRACEBACK_TOKEN

# ``sqlite3.connect`` defaults to ``timeout=5.0`` (Python's documented default),
# implemented as a 5000 ms busy handler, even though the repo never sets it.
SQLITE3_DEFAULT_BUSY_TIMEOUT_MS = 5000


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> w8.State:
    """An isolated XDG tree with a real migrated database (all four XDG roots).

    Built from ``tmp_path`` + ``monkeypatch`` via ``_w8_probe.prepare_state``, so
    neither the in-process CLI nor any child process can reach the developer's real
    config/data/state trees.
    """
    return w8.prepare_state(monkeypatch, tmp_path)


def invoke(args: list[str]) -> Result:
    """Invoke the real Typer entry with the isolated XDG env already in place."""
    return CliRunner().invoke(app, args)


# ── Oracles (module-level and primitive-driven so the controls can fail them) ──


def assert_cloud_call_counter_exact(*, label: str, expected: int, actual: int) -> None:
    """Exactness oracle for the process-global cloud-call counter."""
    assert actual == expected, (
        f"{label}: lost {expected - actual} update(s) — expected {expected}, observed {actual}"
    )


def assert_clients_and_wal(*, db_path: Path, expected_ids: frozenset[str], label: str) -> None:
    """Multi-process oracle: WAL mode, integrity, and every write present."""
    assert db_path.exists(), f"{label}: the database was never created: {db_path}"
    assert w8.integrity_check(db_path) == "ok", f"{label}: PRAGMA integrity_check != ok"
    conn = sqlite3.connect(str(db_path))
    try:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        present = frozenset(str(row[0]) for row in conn.execute("SELECT id FROM clients"))
    finally:
        conn.close()
    assert mode == "wal", f"{label}: journal_mode={mode!r}, expected 'wal'"
    missing = expected_ids - present
    assert not missing, (
        f"{label}: lost writes — the database is missing clients {sorted(missing)} "
        f"(present {sorted(present)})"
    )


def assert_clean_database_failure(*, exit_code: int, output: str) -> None:
    """The W8b-shaped clean-failure oracle: an allowed code and a named component.

    A clean failure exits with a code from ``errors.py`` and prints a message that
    names what failed; the crash this suite pins exits 1 with *empty* output, so it
    fails every one of these conjuncts.
    """
    assert exit_code in (1, 5), f"exit={exit_code} is not a clean failure code"
    assert TRACEBACK_TOKEN not in output, f"a raw traceback reached the user: {output!r}"
    assert "database" in output.lower(), f"no message names the failing component: {output!r}"


# ── In-process race helpers ─────────────────────────────────────────────────────


@contextmanager
def _tiny_switch_interval(interval: float = 1e-6) -> Iterator[None]:
    """Shrink the GIL switch interval so any checkpoint-free RMW is exercised hard."""
    original = sys.getswitchinterval()
    sys.setswitchinterval(interval)
    try:
        yield
    finally:
        sys.setswitchinterval(original)


def _run_in_threads(target: Callable[[], None], *, threads: int) -> None:
    """Start *threads* copies of *target*, released together by a barrier."""
    barrier = threading.Barrier(threads)

    def worker() -> None:
        barrier.wait()
        target()

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join()


def _hit_counter(per_thread: int) -> None:
    for _ in range(per_thread):
        record_cloud_call()


def _hit_racy_counter(per_thread: int, box: list[int]) -> None:
    """A deliberately racy increment: read, yield the GIL, write back."""
    for _ in range(per_thread):
        value = box[0]
        time.sleep(0)  # an explicit scheduling point between the read and the write
        box[0] = value + 1


# ── 1. The cloud-call counter ───────────────────────────────────────────────────


def test_cloud_call_counter_is_exact_under_concurrent_threads() -> None:
    """``record_cloud_call`` must not lose an update under thread contention.

    Reachability: the observed total is asserted ``> 0`` (the increment body ran)
    before the exactness comparison.
    """
    threads = 8
    per_thread = 20_000
    reset_total_cloud_calls()

    with _tiny_switch_interval():
        _run_in_threads(lambda: _hit_counter(per_thread), threads=threads)

    observed = get_total_cloud_calls()
    assert observed > 0, "reachability: record_cloud_call was never entered"
    assert_cloud_call_counter_exact(
        label="record_cloud_call",
        expected=threads * per_thread,
        actual=observed,
    )


def test_the_counter_race_control_loses_updates_so_the_oracle_is_not_vacuous() -> None:
    """Anti-vacuity: a genuinely racy increment must make the oracle fire.

    If ``assert_cloud_call_counter_exact`` accepted a lost update, this case would
    fail — that is the negative control the file relies on.
    """
    threads = 4
    per_thread = 5_000
    box = [0]

    _run_in_threads(lambda: _hit_racy_counter(per_thread, box), threads=threads)

    expected = threads * per_thread
    assert box[0] < expected, (
        "the deliberately racy control lost no updates; the exactness check is vacuous"
    )
    with pytest.raises(AssertionError, match="lost"):
        assert_cloud_call_counter_exact(label="racy control", expected=expected, actual=box[0])


def test_negative_control_exactness_oracle_accepts_the_exact_shape() -> None:
    """Positive control: the oracle must not be permanently failing."""
    assert_cloud_call_counter_exact(label="record_cloud_call", expected=1000, actual=1000)


# ── 2. The PII-before-egress flag ───────────────────────────────────────────────


def test_pii_availability_flag_is_process_global_not_thread_local() -> None:
    """A mark set in one thread is visible in every other thread (and vice versa)."""
    reset_pii_available()
    observed: dict[str, bool] = {}

    def mark_in_thread() -> None:
        mark_pii_available()
        observed["worker_sees_mark"] = pii_available()

    marker = threading.Thread(target=mark_in_thread)
    marker.start()
    marker.join()

    assert observed.get("worker_sees_mark") is True, (
        "reachability: mark_pii_available was never entered in the worker thread"
    )
    assert pii_available() is True, "a mark set in one thread is not visible in another"

    def reset_in_thread() -> None:
        reset_pii_available()

    resetter = threading.Thread(target=reset_in_thread)
    resetter.start()
    resetter.join()

    assert pii_available() is False, "a reset in one thread is not visible in another"


def test_a_concurrent_operation_reset_clears_another_operations_strip_mark() -> None:
    """Characterisation: one shared flag cannot give two operations a private gate.

    ``_pii_available`` is process-global ("per-operation evidence", ``router.py:70-90``).
    Operation A resets, strips and marks; operation B's start-reset then clears A's
    mark, and A's egress gate (``router._enforce_tier``) reads ``False`` — a spurious
    ``PIIUnavailableError`` for an operation that did strip. The interleaving is
    forced with events; the reachability check is that A's mark was set and read.
    """
    reset_pii_available()
    a_marked = threading.Event()
    b_reset = threading.Event()
    observed: dict[str, bool] = {}

    def operation_a() -> None:
        reset_pii_available()  # operation A start
        mark_pii_available()  # operation A stripped successfully
        observed["a_marked"] = pii_available()
        a_marked.set()
        assert b_reset.wait(10.0), "operation B never reset the flag"
        observed["a_egress_gate"] = pii_available()

    def operation_b() -> None:
        assert a_marked.wait(10.0), "operation A never marked the flag"
        reset_pii_available()  # operation B start clears operation A's mark
        b_reset.set()

    thread_a = threading.Thread(target=operation_a)
    thread_b = threading.Thread(target=operation_b)
    thread_a.start()
    thread_b.start()
    thread_a.join(10.0)
    thread_b.join(10.0)

    assert observed.get("a_marked") is True, "reachability: operation A never marked"
    assert observed.get("a_egress_gate") is False, (
        "expected the other operation's reset to have cleared the shared flag"
    )


# ── 3. Multi-process writers on one database ────────────────────────────────────


def _run_cli_writers(state: w8.State, client_ids: list[str]) -> list[tuple[int, str, str]]:
    """Start one real CLI process per id and reap them all."""
    processes = [w8.spawn_cli_writer(state.root, client_id) for client_id in client_ids]
    return [w8.reap_child(process) for process in processes]


def _assert_writers_succeeded(ids: list[str], results: list[tuple[int, str, str]]) -> None:
    for client_id, (returncode, stdout, stderr) in zip(ids, results, strict=True):
        assert returncode == 0, f"{client_id}: rc={returncode} stdout={stdout!r} stderr={stderr!r}"
        assert "CHILD_EXIT 0" in stdout, f"{client_id}: the CLI child never reached its end"


def test_two_cli_processes_write_one_database_without_loss(state: w8.State) -> None:
    """Two processes, one migrated database: WAL, no lost write, no locked crash."""
    ids = ["w8c-mp-a", "w8c-mp-b"]

    results = _run_cli_writers(state, ids)
    _assert_writers_succeeded(ids, results)

    assert_clients_and_wal(
        db_path=state.db_path, expected_ids=frozenset(ids), label="two processes"
    )


def test_four_cli_processes_write_one_database_without_loss(state: w8.State) -> None:
    """Four processes, one migrated database: WAL, no lost write, no locked crash."""
    ids = ["w8c-mp-c0", "w8c-mp-c1", "w8c-mp-c2", "w8c-mp-c3"]

    results = _run_cli_writers(state, ids)
    _assert_writers_succeeded(ids, results)

    assert_clients_and_wal(
        db_path=state.db_path, expected_ids=frozenset(ids), label="four processes"
    )


def test_connections_carry_the_sqlite3_default_busy_timeout(state: w8.State) -> None:
    """``busy_timeout`` is never set repo-wide, but sqlite3's 5 s default applies.

    That default is why the two ordinary writers above serialise instead of failing.
    It does *not* cover the ``PRAGMA journal_mode=WAL`` change (see the cold-start
    cases): SQLite does not invoke the busy handler for a journal-mode change while
    another connection holds a transaction.
    """
    conn = get_connection(state.db_path)
    try:
        busy_timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    finally:
        conn.close()

    assert busy_timeout == SQLITE3_DEFAULT_BUSY_TIMEOUT_MS, (
        f"busy_timeout={busy_timeout}, expected the sqlite3 default "
        f"{SQLITE3_DEFAULT_BUSY_TIMEOUT_MS}"
    )


# ── 4. The cold-start locked-database defect ────────────────────────────────────


def _hold_migration_write_lock(db_path: Path) -> sqlite3.Connection:
    """Create *db_path* in rollback-journal mode and hold an IMMEDIATE write lock.

    This models the second process of a cold start: the first process is mid
    migration with a write transaction open on a database that is not yet WAL.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("CREATE TABLE IF NOT EXISTS seed (value INTEGER)")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO seed VALUES (1)")
    return conn


def _has_client(db_path: Path, client_id: str) -> bool:
    """Whether *client_id* is a committed row; a missing table reads as absent."""
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "clients" not in tables:
            return False  # the clients table was never migrated into existence
        row = conn.execute("SELECT 1 FROM clients WHERE id = ?", (client_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


@pytest.mark.xfail(
    strict=True,
    reason="a CLI start blocked by another writer's migration reports an unhandled "
    "OperationalError('database is locked') instead of a clean error",
)
def test_a_cli_start_racing_an_open_migration_transaction_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expected: a code from ``errors.py`` and a message that names the component."""
    state = w8.prepare_state(monkeypatch, tmp_path, migrate=False)
    holder = _hold_migration_write_lock(state.db_path)
    try:
        result = invoke(["client", "add", "w8c-cold-expected", "Client cold"])
    finally:
        holder.rollback()
        holder.close()

    assert_clean_database_failure(exit_code=result.exit_code, output=result.output)


def test_a_cli_start_racing_an_open_migration_transaction_crashes_locked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Characterisation pinning the defect in ``storage/database.py:12``.

    ``_init`` (``app.py:264``) runs ``init_database`` before any subcommand, which
    opens a connection and executes ``PRAGMA journal_mode=WAL`` while another
    connection holds a write transaction on a non-WAL file. SQLite returns
    ``SQLITE_BUSY`` and does not invoke the busy handler for a journal-mode change,
    so the pragma fails in 0.00 s even with a 2 s ``busy_timeout``. Observed: exit 1,
    ``result.exception`` IS the ``OperationalError``, ``result.output == ""``, the
    schema was never migrated (``user_version == 0``) and the write is lost.
    """
    state = w8.prepare_state(monkeypatch, tmp_path, migrate=False)
    holder = _hold_migration_write_lock(state.db_path)
    try:
        result = invoke(["client", "add", "w8c-cold", "Client cold"])
    finally:
        holder.rollback()
        holder.close()

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, sqlite3.OperationalError), repr(result.exception)
    assert "locked" in str(result.exception), str(result.exception)
    assert result.output == "", repr(result.output)
    assert w8.user_version(state.db_path) == 0, "migrations should not have run"
    assert not _has_client(state.db_path, "w8c-cold"), "the blocked write landed anyway"


# ── Negative controls for the multi-process and clean-failure oracles ───────────


def _seed_clients(db_path: Path, client_ids: list[str]) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        for client_id in client_ids:
            conn.execute("INSERT INTO clients (id, name) VALUES (?, ?)", (client_id, client_id))
        conn.commit()
    finally:
        conn.close()


def test_negative_control_multi_process_oracle_rejects_a_lost_write(tmp_path: Path) -> None:
    """The oracle must fail when a process's write is missing, not just report WAL."""
    db_path = tmp_path / "openreview.db"
    init_database(db_path)
    _seed_clients(db_path, ["present"])

    with pytest.raises(AssertionError, match="lost writes"):
        assert_clients_and_wal(
            db_path=db_path,
            expected_ids=frozenset({"present", "missing"}),
            label="negative control",
        )


def test_negative_control_multi_process_oracle_accepts_the_full_set(tmp_path: Path) -> None:
    """Positive control: the same oracle must accept a complete, WAL database."""
    db_path = tmp_path / "openreview.db"
    init_database(db_path)
    _seed_clients(db_path, ["a", "b"])

    assert_clients_and_wal(
        db_path=db_path, expected_ids=frozenset({"a", "b"}), label="positive control"
    )


def test_negative_control_clean_failure_oracle_rejects_empty_output() -> None:
    """The oracle must reject the very shape the defect produces: exit 1, no output."""
    with pytest.raises(AssertionError, match="no message names"):
        assert_clean_database_failure(exit_code=1, output="")


def test_negative_control_clean_failure_oracle_accepts_a_named_message() -> None:
    """Positive control: a component-naming, traceback-free message must pass."""
    assert_clean_database_failure(
        exit_code=5,
        output="Config error: database is locked (another process holds a write lock)\n",
    )
