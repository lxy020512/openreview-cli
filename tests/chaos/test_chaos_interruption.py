"""W8a chaos suite: interruption (SIGINT and SIGTERM).

Two signals, deliberately distinguished, because the product installs **no** CLI
signal handler (the only ``signal.signal`` calls under ``src/`` are Textual's at
``tui/app.py:207-208,250-251``; see ``test_the_cli_installs_no_signal_handler``):

* **SIGINT** arrives as a ``KeyboardInterrupt`` and is translated by the
  *framework*, not the product: Typer 0.26.7 catches it in ``BaseCommand.main``
  and raises ``Exit(130)`` (``.venv/.../typer/core.py:197``), so the observed
  exit code is **130**, not 1. The case injects the ``KeyboardInterrupt`` at each
  enumerated stage boundary through the real Typer entry and asserts exit 130 plus
  the absence of any partial row.
* **SIGTERM** has the OS default disposition: no handler, no rollback. The case
  therefore does NOT assert a clean exit and does NOT assert a code from
  ``errors.py``; it kills a real child process mid-stage, reopens the database and
  asserts recovery (integrity, ``user_version``, WAL, no half-written rows).

The interruption points enumerated once (the W8 oracle) are, with the boundary
each case drives:

===========================  ==============================  ==================
boundary                     test                            reachable
===========================  ==============================  ==================
``parse`` entry              ``...cli_boundary[parse_in]``    CLI
``parse`` exit (post-body)   ``...cli_boundary[parse_out]``   CLI
``strip`` entry              ``...cli_boundary[strip_in]``    CLI
``strip`` persistence        ``...after_the_pii_persist``     CLI
``review`` entry             ``...cli_boundary[review_in]``   CLI
``review`` exit (post-body)  ``...cli_boundary[review_out]``  CLI
``chunk`` entry              ``...runner_boundary[chunk]``    runner only
``retrieve`` entry           ``...runner_boundary[retrieve]`` runner only
``generate`` entry           ``...runner_boundary[generate]`` runner only
``comparison`` entry         ``...runner_boundary[comparison]`` runner only
``benchmark`` entry          ``...runner_boundary[benchmark]`` runner only
migration loop / statement   ``...between_migration_and_version``  library
PII mapping write            ``...mid_pii_mapping_write``     library
PII cache row (after map)    ``...after_the_pii_persist``     library/CLI
===========================  ==============================  ==================

``chunk``/``retrieve``/``generate``/``comparison`` are defined in
``src/openreview_cli/pipeline/adapters/`` but no CLI path composes them today
(``retrieve``/``chunk`` commands parse their own inputs); ``benchmark`` needs a
dataset download. They are still enumerated and driven at the runner boundary, so
the list is complete rather than convenient.
"""

from __future__ import annotations

import signal
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

import openreview_cli.review.pipeline as review_pipeline
import openreview_cli.review.runner as review_runner
import openreview_cli.storage.database as database_mod
from openreview_cli.app import app
from openreview_cli.pii.mapping import read_pii_mapping
from openreview_cli.pipeline import Pipeline
from openreview_cli.pipeline.adapters import parse as parse_mod
from openreview_cli.pipeline.adapters import strip as strip_mod
from openreview_cli.pipeline.base import Stage
from openreview_cli.storage.database import run_migrations
from tests.chaos import _w8_probe as w8

pytestmark = pytest.mark.chaos

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
DOC = FIXTURES / "pdf" / "simple_contract.pdf"

# The exit code Typer's framework translation produces for a KeyboardInterrupt
# (typer/core.py:197 -> click Exit(130)). NOT 1: the product never sees the
# KeyboardInterrupt, and never maps it to an errors.py code.
TYPER_SIGINT_EXIT = 130


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_entered() -> None:
    w8.entered().reset()


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> w8.State:
    """An isolated XDG tree with a real migrated database."""
    return w8.prepare_state(monkeypatch, tmp_path)


@pytest.fixture
def document(tmp_path: Path) -> Path:
    """A real, parseable PDF inside the isolated tree (never the tracked fixture)."""
    target = tmp_path / "contract.pdf"
    target.write_bytes(DOC.read_bytes())
    return target


def invoke_review(state: w8.State, document: Path, *, no_pii: bool) -> Result:
    """Invoke the real Typer entry for ``precheck review`` on the isolated tree."""
    args = [
        "precheck",
        "review",
        str(document),
        "--no-grounding",
        "--output-dir",
        str(state.output_dir),
    ]
    if no_pii:
        args.append("--no-pii")
    return CliRunner().invoke(app, args)


# ── SIGINT at a CLI stage boundary ──────────────────────────────────────────

BOUNDARIES: tuple[tuple[str, str, bool], ...] = (
    # (boundary key, entered-counter key, no_pii)
    ("parse_in", "parse", True),
    ("parse_out", "parse_out", True),
    ("strip_in", "strip", False),
    ("review_in", "review", True),
    ("review_out", "review_out", True),
)


def _class_for(boundary: str) -> type[Any]:
    return {
        "parse_in": w8.InterruptParseStage,
        "parse_out": w8.InterruptAfterParseStage,
        "strip_in": w8.InterruptStripStage,
        "review_in": w8.InterruptReviewStage,
        "review_out": w8.InterruptAfterReviewStage,
    }[boundary]


def _install(monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    """Patch the stage class the real pipeline composes for *boundary*."""
    cls = _class_for(boundary)
    if boundary.startswith("parse"):
        monkeypatch.setattr(parse_mod, "ParseStage", cls)
        monkeypatch.setattr(review_runner, "ParseStage", cls)
    elif boundary == "strip_in":
        monkeypatch.setattr(strip_mod, "StripStage", cls)
        monkeypatch.setattr(review_runner, "StripStage", cls)
    else:
        monkeypatch.setattr(review_pipeline, "ReviewStage", cls)


@pytest.mark.parametrize(("boundary", "entered_key", "no_pii"), BOUNDARIES)
def test_sigint_at_a_cli_stage_boundary_exits_130_without_partial_rows(
    state: w8.State,
    document: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    entered_key: str,
    no_pii: bool,
) -> None:
    """Inject KeyboardInterrupt at the boundary through the real Typer entry."""
    _install(monkeypatch, boundary)

    result = invoke_review(state, document, no_pii=no_pii)

    # Reachability: the injected boundary really was the code that ran.
    assert w8.entered().count(entered_key) == 1, w8.entered().stages
    # The framework translates the KeyboardInterrupt (typer/core.py:197).
    assert result.exit_code == TYPER_SIGINT_EXIT, (
        f"exit={result.exit_code} output={result.output!r} exc={result.exception!r}"
    )
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert w8.TRACEBACK_TOKEN not in result.output
    # No partial row: nothing on the review path may have been half-written.
    w8.assert_db_consistent(state.db_path)
    w8.assert_no_review_path_writes(state.db_path)


def test_sigint_after_the_pii_persist_leaves_only_complete_rows(
    state: w8.State, document: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boundary after committed persistence: the surviving rows must be complete.

    This is the meaningful half of "no partial row" — the rows that DO survive an
    interruption are asserted complete (the mapping decrypts, the cache row's
    referenced files exist, the audit row has a valid status).
    """
    monkeypatch.setattr(strip_mod, "StripStage", w8.PersistThenInterruptStripStage)
    monkeypatch.setattr(review_runner, "StripStage", w8.PersistThenInterruptStripStage)

    result = invoke_review(state, document, no_pii=False)

    assert w8.entered().count("strip_persist") == 1
    assert result.exit_code == TYPER_SIGINT_EXIT, repr(result.exception)
    w8.assert_db_consistent(state.db_path)

    counts = w8.assert_no_partial_rows(state.db_path)
    assert counts["pii_audit_trail"] == 1, counts
    assert counts["pii_cache"] == 1, counts
    # The encrypted mapping really is the real artifact written by the product.
    mapping = read_pii_mapping(state.data_dir / "reviews" / ("a" * 12), "k" * 32)
    assert mapping == {"PERSON_1": "Jane Roe"}


# ── SIGINT inside the PII mapping write (library boundary) ──────────────────


def _interrupting_mapping_writer(message: str) -> Any:
    """A ``write_pii_mapping`` stand-in that writes partial bytes then raises."""

    def _write(mapping: dict[str, str], review_dir: Path, encryption_key: str) -> Path:
        review_dir.mkdir(parents=True, exist_ok=True)
        path = review_dir / "pii_map.enc"
        path.write_bytes(b"\x00" * 16 + b"PARTIAL")  # a half-written artifact
        raise w8.refusal(message)

    return _write


def assert_mapping_artifact_is_complete_or_absent(review_dir: Path, key: str) -> None:
    """The PII-mapping oracle: a present artifact must be a readable 0600 mapping."""
    path = review_dir / "pii_map.enc"
    if not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"the mapping artifact is {oct(mode)}, not 0600"
    read_pii_mapping(review_dir, key)  # an InvalidToken here is a half write


@pytest.mark.xfail(strict=True, reason="RT-045")
def test_interrupt_mid_pii_mapping_write_leaves_no_half_written_mapping(
    state: w8.State, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expected: the mapping artifact is either absent or a complete 0600 mapping."""
    review_dir = state.data_dir / "reviews" / ("a" * 12)
    monkeypatch.setattr(
        "openreview_cli.pii.persist.write_pii_mapping",
        _interrupting_mapping_writer("W8 injected SIGINT mid mapping write"),
    )

    from openreview_cli.pii.persist import persist_pii_result

    with pytest.raises(KeyboardInterrupt):
        persist_pii_result(
            state.db_path,
            document_hash="a" * 64,
            config_hash="cfg-hash",
            pii_result=w8.synthetic_pii_result(),
            review_dir=review_dir,
            encryption_key="k" * 32,
            filename="doc.pdf",
        )

    assert_mapping_artifact_is_complete_or_absent(review_dir, "k" * 32)


def test_a_half_written_mapping_survives_today(
    state: w8.State, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Characterisation pinning RT-045: the half write lands, unreadable and 0664.

    ``write_pii_mapping`` writes the ciphertext, then chmods (``mapping.py:41-42``),
    with no temp-file-and-rename, so an interrupt between the two leaves a partial
    artifact with the process umask's permissions (0o664 here, never 0o600) that
    the next read rejects with a bare ``InvalidToken``. No ``pii_cache`` row and no
    ``pii_audit_trail`` row exist yet, so the artifact is an orphan.
    """
    review_dir = state.data_dir / "reviews" / ("a" * 12)
    monkeypatch.setattr(
        "openreview_cli.pii.persist.write_pii_mapping",
        _interrupting_mapping_writer("W8 injected SIGINT mid mapping write"),
    )

    from openreview_cli.pii.persist import persist_pii_result

    with pytest.raises(KeyboardInterrupt):
        persist_pii_result(
            state.db_path,
            document_hash="a" * 64,
            config_hash="cfg-hash",
            pii_result=w8.synthetic_pii_result(),
            review_dir=review_dir,
            encryption_key="k" * 32,
            filename="doc.pdf",
        )

    path = review_dir / "pii_map.enc"
    assert path.exists(), "the partial artifact should have survived"
    assert path.stat().st_size < 200, "the artifact is a truncated fragment"
    assert path.stat().st_mode & 0o077, "the partial artifact is not 0600"
    with pytest.raises(Exception) as exc_info:
        read_pii_mapping(review_dir, "k" * 32)
    assert type(exc_info.value).__name__ == "InvalidToken", repr(exc_info.value)
    counts = w8.assert_no_partial_rows(state.db_path)
    assert counts["pii_cache"] == 0 and counts["pii_audit_trail"] == 0, counts


# ── SIGINT between a migration and its version bump (library boundary) ──────


def _interrupt_after_migration(
    real: Callable[[sqlite3.Connection, Path], None], target_stem: str
) -> Callable[[sqlite3.Connection, Path], None]:
    """Wrap the real migration executor; raise after *target_stem* ran."""

    def _wrapper(conn: sqlite3.Connection, sql_file: Path) -> None:
        real(conn, sql_file)
        if sql_file.stem.startswith(target_stem):
            raise w8.refusal(f"W8 injected SIGINT after {sql_file.name}")

    return _wrapper


def _schema(db_path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        return sorted(
            (str(r[0]), str(r[1]))
            for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
        )
    finally:
        conn.close()


def test_interrupt_between_migration_and_version_bump_is_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration is not wrapped in a transaction, so ``user_version`` lags.

    The observable damage is bounded, and recovery holds: a second
    ``run_migrations`` reaches the same version and the same schema as a fresh
    database. The recovery relies on the unconditional ``OperationalError``
    swallow of RT-023 (observed as "skipped statement (schema already matches)").
    """
    db_path = tmp_path / "mig.db"
    real_exec = database_mod._exec_migration_safely
    monkeypatch.setattr(
        database_mod, "_exec_migration_safely", _interrupt_after_migration(real_exec, "011")
    )

    with pytest.raises(KeyboardInterrupt):
        run_migrations(db_path)

    monkeypatch.setattr(database_mod, "_exec_migration_safely", real_exec)
    # Characterisation: the schema is AHEAD of the recorded version.
    assert w8.user_version(db_path) == 10, w8.user_version(db_path)
    conn = sqlite3.connect(db_path)
    try:
        columns = [str(r[1]) for r in conn.execute("PRAGMA table_info(review_reports)")]
    finally:
        conn.close()
    assert "client_id" in columns, "migration 011's column should already exist"

    # Recovery: a second run converges on a fresh database's schema.
    run_migrations(db_path)
    fresh = tmp_path / "fresh.db"
    run_migrations(fresh)
    assert w8.user_version(db_path) == w8.LATEST_USER_VERSION
    assert _schema(db_path) == _schema(fresh), "the re-run did not converge"
    w8.assert_db_consistent(db_path)


@pytest.mark.xfail(strict=True, reason="RT-046")
def test_interrupted_migration_keeps_user_version_equal_to_the_applied_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expected: ``user_version`` names the highest fully applied migration."""
    db_path = tmp_path / "mig.db"
    real_exec = database_mod._exec_migration_safely
    monkeypatch.setattr(
        database_mod, "_exec_migration_safely", _interrupt_after_migration(real_exec, "011")
    )

    with pytest.raises(KeyboardInterrupt):
        run_migrations(db_path)

    monkeypatch.setattr(database_mod, "_exec_migration_safely", real_exec)
    conn = sqlite3.connect(db_path)
    try:
        columns = [str(r[1]) for r in conn.execute("PRAGMA table_info(review_reports)")]
    finally:
        conn.close()
    version = w8.user_version(db_path)
    applied = 11 if "client_id" in columns else 10
    assert version == applied, f"user_version={version} but schema is at {applied}"


# ── SIGTERM: a real process killed mid-stage ────────────────────────────────

SIGTERM_CASES: tuple[tuple[str, bool, bool], ...] = (
    # (stage, stub_parse, no_pii)
    ("parse", False, True),
    ("strip", True, False),
    ("review", True, True),
)


def _subprocess_root(tmp_path: Path) -> Path:
    """Materialize the isolated XDG roots a child process will use."""
    w8.isolated_env(tmp_path)
    return tmp_path


def _doc_for_child(root: Path) -> Path:
    """An existing file is all ``run_review`` checks before the stage is entered."""
    path = root / "contract.pdf"
    path.write_bytes(b"")
    return path


def _child_args(root: Path, document: Path, *, no_pii: bool) -> list[str]:
    args = [
        "precheck",
        "review",
        str(document),
        "--no-grounding",
        "--output-dir",
        str(root / "out"),
    ]
    if no_pii:
        args.append("--no-pii")
    return args


@pytest.mark.parametrize(("stage", "stub_parse", "no_pii"), SIGTERM_CASES)
def test_sigterm_mid_stage_leaves_a_recoverable_database(
    tmp_path: Path, stage: str, stub_parse: bool, no_pii: bool
) -> None:
    """Kill mid-stage, reopen, assert recovery — never a clean documented exit."""
    root = _subprocess_root(tmp_path)
    document = _doc_for_child(root)

    killed = w8.run_killed_child(
        root,
        stage=stage,
        signum=int(signal.SIGTERM),
        argv=_child_args(root, document, no_pii=no_pii),
        stub_parse=stub_parse,
    )

    assert killed.reached_sentinel, f"the child never reached the {stage} stage"
    # The OS default disposition: died by signal, no handler, no errors.py code.
    assert killed.returncode == -int(signal.SIGTERM), killed.returncode
    assert killed.returncode not in {0, 1, 2, 3, 5, 6, 8, 9}

    db_path = root / "xdg_data_home" / "openreview" / "openreview.db"
    w8.assert_db_consistent(db_path)
    w8.assert_no_review_path_writes(db_path)


def test_sigint_in_a_real_process_exits_130(tmp_path: Path) -> None:
    """The real signal, not the injection: a live SIGINT yields Typer's 130."""
    root = _subprocess_root(tmp_path)
    document = _doc_for_child(root)

    killed = w8.run_killed_child(
        root,
        stage="parse",
        signum=int(signal.SIGINT),
        argv=_child_args(root, document, no_pii=True),
        stub_parse=False,
    )

    assert killed.reached_sentinel
    assert killed.returncode == TYPER_SIGINT_EXIT, killed.returncode
    db_path = root / "xdg_data_home" / "openreview" / "openreview.db"
    w8.assert_db_consistent(db_path)
    w8.assert_no_review_path_writes(db_path)


# ── The runner boundary for the stages no CLI path wires ────────────────────

RUNNER_CASES: tuple[tuple[str, Callable[[Path], Stage]], ...] = (
    ("chunk", lambda tmp: w8.InterruptChunkStage()),
    ("retrieve", lambda tmp: w8.InterruptRetrieveStage(db_path=tmp / "x.db")),
    ("generate", lambda tmp: w8.InterruptGenerateStage(gateway=None)),
    ("comparison", lambda tmp: w8.InterruptComparisonStage(model="extraction")),
    ("benchmark", lambda tmp: w8.InterruptBenchmarkStage()),
)


@pytest.mark.parametrize(("key", "factory"), RUNNER_CASES)
async def test_runner_propagates_a_stage_boundary_interrupt_and_stops(
    tmp_path: Path, key: str, factory: Callable[[Path], Stage]
) -> None:
    """A BaseException must escape the runner, not be swallowed as a StageError.

    ``Pipeline._execute_single_stage`` catches ``Exception``; a ``KeyboardInterrupt``
    is a ``BaseException``, so it must escape both the stage and the runner, and
    the stage after the interrupted one must never execute.
    """
    pipeline = Pipeline([factory(tmp_path), w8.SentinelStage()])

    with pytest.raises(KeyboardInterrupt):
        await pipeline.run({})

    assert w8.entered().count(key) == 1, w8.entered().stages
    assert w8.entered().count("sentinel") == 0, "the runner continued past the interrupt"


# ── The structural fact the whole suite rests on ────────────────────────────


def test_the_cli_installs_no_signal_handler() -> None:
    """The only ``signal.signal`` calls under ``src/`` are Textual's.

    This is what makes the SIGTERM expectation (default disposition, no rollback)
    and the SIGINT expectation (framework translation, exit 130) correct rather
    than assumed.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "openreview_cli"
    hits: list[str] = []
    for path in sorted(src.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "signal.signal(" in line:
                hits.append(f"{path.relative_to(src)}:{lineno}")
    assert hits, "the grep found no signal handler at all — the probe is looking at the wrong tree"
    assert all(hit.startswith("tui/app.py:") for hit in hits), hits


# ── Negative control (anti-vacuity, plan section 9.3) ───────────────────────


def test_negative_control_row_oracle_rejects_a_planted_partial_row(state: w8.State) -> None:
    """The completeness oracle must fail on a row a truncated write left behind."""
    conn = sqlite3.connect(state.db_path)
    try:
        conn.execute(
            "INSERT INTO review_reports (id, filename, mode, report_json) "
            "VALUES ('partial', 'f.pdf', 'precheck', '{not json')"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssertionError, match="not a complete JSON document"):
        w8.assert_no_partial_rows(state.db_path)


def test_negative_control_mapping_oracle_rejects_a_partial_artifact(tmp_path: Path) -> None:
    """The mapping oracle must reject a fragment it did not write itself."""
    review_dir = tmp_path / "reviews" / "x"
    review_dir.mkdir(parents=True)
    (review_dir / "pii_map.enc").write_bytes(b"\x00" * 16 + b"PARTIAL")

    with pytest.raises(AssertionError, match="not 0600"):
        assert_mapping_artifact_is_complete_or_absent(review_dir, "k" * 32)
