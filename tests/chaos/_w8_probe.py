"""Private W8 chaos harness: interruption, resource limits and concurrency.

Kept as one private module per the W4/W5/W7 precedent (``tests/fuzz/_probe.py``,
``tests/redteam/_w5_probe.py``, ``tests/chaos/_w7_probe.py``): the three W8 suites
share the XDG isolation, the DB read-back oracle and the subprocess plumbing, and
the chaos tree's ``conftest.py`` is owned by W7a alone (plan section 7).

The three things worth stating up front, all measured rather than assumed:

1. **The product installs no CLI signal handler.** The only ``signal.signal``
   calls under ``src/`` are Textual's (``tui/app.py:207-208,250-251``). A SIGINT
   delivered to the CLI therefore reaches the *framework*: Typer 0.26.7 catches
   ``KeyboardInterrupt`` in ``BaseCommand.main`` and raises ``Exit(130)``
   (``.venv/.../typer/core.py:197``), so the observed exit code is **130**, not
   the 1 the brief predicted. A SIGTERM gets the OS default disposition: the
   process dies with signal 15 and **no** rollback runs. Both facts are asserted,
   not assumed (see ``test_chaos_interruption.py``).

2. **Every subprocess gets an explicitly isolated environment.** See
   :func:`isolated_env` — all four XDG roots are pinned at the per-test
   ``tmp_path``. A bare ``uv run openreview`` (no XDG override) writes into the
   developer's real ``~/.config``/``~/.local``/``~/.cache``; nothing here does
   that, and the in-process path uses ``monkeypatch.setenv`` on the same roots.

3. **The oracle for "no partial state" is a read of the tables.** Every
   interruption assertion reopens the database, reads ``PRAGMA integrity_check``,
   ``PRAGMA user_version``, ``PRAGMA journal_mode`` and the review-path tables,
   and checks each surviving row for completeness (a partial row is a row a
   truncated write left behind: a mis-shaped ``pii_cache`` row, a
   ``review_reports.report_json`` that does not parse, a mapping file that does
   not decrypt). stdout is never used as the oracle.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openreview_cli.pii.models import PiiEntity, PiiResult
from openreview_cli.pipeline.adapters.benchmark import BenchmarkStage
from openreview_cli.pipeline.adapters.chunk import ChunkStage
from openreview_cli.pipeline.adapters.comparison import ComparisonStage
from openreview_cli.pipeline.adapters.generate import GenerateStage
from openreview_cli.pipeline.adapters.parse import ParseStage
from openreview_cli.pipeline.adapters.retrieve import RetrieveStage
from openreview_cli.pipeline.adapters.strip import StripStage
from openreview_cli.pipeline.base import PipelineContext, Stage
from openreview_cli.review.pipeline import ReviewStage
from openreview_cli.storage.database import init_database

REPO_ROOT = Path(__file__).resolve().parents[2]

XDG_VARS: tuple[str, ...] = (
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
)

# ``016_review_checkpoints.sql`` is the highest migration shipped today.
LATEST_USER_VERSION = 16

# The review path can write any of these; the other tables are seeded by the CLI
# smoke path (clients) or never touched by a review run.
REVIEW_PATH_TABLES: tuple[str, ...] = (
    "reviews",
    "review_reports",
    "cost_logs",
    "pii_audit_trail",
    "pii_cache",
)

# The full migrated table set, read off a freshly migrated database.
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "benchmark_baselines",
        "benchmark_results",
        "benchmark_runs",
        "clients",
        "cost_logs",
        "graph_edges",
        "graph_meta",
        "graph_nodes",
        "pii_audit_trail",
        "pii_cache",
        "playbook_meta",
        "playbook_versions",
        "prompt_bindings",
        "prompt_versions",
        "recovery_state",
        "review_checkpoint_runs",
        "review_checkpoint_steps",
        "review_diffs",
        "review_reports",
        "reviews",
        "schema_version",
    }
)

TRACEBACK_TOKEN = "Traceback (most recent call last)"


# ── Isolated XDG state (in-process and subprocess) ──────────────────────────


@dataclass
class State:
    """The isolated XDG tree a W8 suite drives the CLI against."""

    root: Path
    config_dir: Path
    config_path: Path
    data_dir: Path
    db_path: Path
    auth_path: Path
    log_dir: Path
    output_dir: Path


def isolated_env(root: Path, **extra: str) -> dict[str, str]:
    """Return an env dict with all four XDG roots pinned under *root*.

    This is the safety-critical helper: every ``subprocess`` spawn goes through
    it, so no child can reach the developer's real config/data/state/cache trees.
    """
    env = dict(os.environ)
    for var in XDG_VARS:
        path = root / var.lower()
        path.mkdir(parents=True, exist_ok=True)
        env[var] = str(path)
    env.pop("OPENREVIEW_OUTPUT_DIR", None)
    env.update(extra)
    return env


def set_xdg(monkeypatch: Any, root: Path) -> None:
    """Point the in-process CLI at *root* (used by every CliRunner case)."""
    for var in XDG_VARS:
        path = root / var.lower()
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(path))
    monkeypatch.delenv("OPENREVIEW_OUTPUT_DIR", raising=False)


def prepare_state(monkeypatch: Any, tmp_path: Path, *, migrate: bool = True) -> State:
    """Redirect XDG at *tmp_path* and seed a real migrated database."""
    set_xdg(monkeypatch, tmp_path)

    config_dir = tmp_path / "xdg_config_home" / "openreview"
    data_dir = tmp_path / "xdg_data_home" / "openreview"
    log_dir = tmp_path / "xdg_state_home" / "openreview" / "log"
    output_dir = tmp_path / "out"
    for directory in (config_dir, data_dir, log_dir, output_dir):
        directory.mkdir(parents=True, exist_ok=True)

    config_path = config_dir / "config.yml"
    config_path.write_text("privacy:\n  tier: balanced\n", encoding="utf-8")

    db_path = data_dir / "openreview.db"
    if migrate:
        init_database(db_path)

    return State(
        root=tmp_path,
        config_dir=config_dir,
        config_path=config_path,
        data_dir=data_dir,
        db_path=db_path,
        auth_path=config_dir / "auth.json",
        log_dir=log_dir,
        output_dir=output_dir,
    )


# ── The DB read-back oracle ─────────────────────────────────────────────────


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def user_version(db_path: Path) -> int:
    conn = _connect(db_path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def integrity_check(db_path: Path) -> str:
    conn = _connect(db_path)
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def table_names(db_path: Path) -> frozenset[str]:
    conn = _connect(db_path)
    try:
        return frozenset(
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
    finally:
        conn.close()


def assert_db_consistent(db_path: Path, *, expect_version: int = LATEST_USER_VERSION) -> None:
    """The reopen oracle: integrity, version, schema and WAL all agree."""
    assert db_path.exists(), f"database was never created: {db_path}"
    assert integrity_check(db_path) == "ok", "PRAGMA integrity_check did not report ok"
    version = user_version(db_path)
    assert version == expect_version, f"user_version={version}, expected {expect_version}"
    names = table_names(db_path)
    missing = EXPECTED_TABLES - names
    assert not missing, f"schema is missing tables: {sorted(missing)}"
    conn = _connect(db_path)
    try:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        conn.close()
    assert mode == "wal", f"journal_mode={mode!r}, expected 'wal'"


def count_rows(db_path: Path, table: str) -> int:
    conn = _connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def assert_no_partial_rows(db_path: Path) -> dict[str, int]:
    """Every surviving row must be complete; a half-written row must not exist.

    Returns the per-table counts it read, so a case can assert reachability (a
    row was actually written) without a second query.
    """
    counts: dict[str, int] = {}
    conn = _connect(db_path)
    try:
        for row in conn.execute("SELECT id, contract_hash, mode, status FROM reviews"):
            assert row["id"] and row["contract_hash"] and row["mode"], f"partial reviews row: {row}"
            assert row["status"] in ("in_progress", "completed"), f"bad status: {row['status']}"
        counts["reviews"] = int(conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0])

        for row in conn.execute("SELECT id, filename, report_json FROM review_reports"):
            assert row["id"] and row["filename"], "partial review_reports row"
            try:
                json.loads(row["report_json"])
            except (TypeError, ValueError) as exc:
                raise AssertionError(
                    f"review_reports.report_json is not a complete JSON document: {exc}"
                ) from exc
        counts["review_reports"] = int(
            conn.execute("SELECT COUNT(*) FROM review_reports").fetchone()[0]
        )

        for row in conn.execute("SELECT model, provider, cost_cents FROM cost_logs"):
            assert row["model"] and row["provider"], "partial cost_logs row"
            assert row["cost_cents"] is not None, "partial cost_logs row"
        counts["cost_logs"] = int(conn.execute("SELECT COUNT(*) FROM cost_logs").fetchone()[0])

        for row in conn.execute("SELECT mapping_path, review_result_path FROM pii_cache"):
            assert row["mapping_path"] and row["review_result_path"], "partial pii_cache row"
            assert Path(str(row["mapping_path"])).exists(), (
                f"pii_cache row references a missing mapping: {row['mapping_path']}"
            )
            assert Path(str(row["review_result_path"])).exists(), (
                f"pii_cache row references a missing stripped text: {row['review_result_path']}"
            )
        counts["pii_cache"] = int(conn.execute("SELECT COUNT(*) FROM pii_cache").fetchone()[0])

        for row in conn.execute("SELECT status, entity_count, config_hash FROM pii_audit_trail"):
            assert row["status"] in ("success", "partial", "failed"), (
                f"bad audit status: {row['status']}"
            )
            assert row["entity_count"] is not None and row["config_hash"], (
                "partial pii_audit_trail row"
            )
        counts["pii_audit_trail"] = int(
            conn.execute("SELECT COUNT(*) FROM pii_audit_trail").fetchone()[0]
        )
    finally:
        conn.close()
    return counts


def assert_no_review_path_writes(db_path: Path) -> None:
    """Stronger form used where the run must not have written anything at all."""
    counts = assert_no_partial_rows(db_path)
    assert counts == dict.fromkeys(REVIEW_PATH_TABLES, 0), f"unexpected rows survived: {counts}"


# ── Reachability counters for the injected stages ───────────────────────────


@dataclass
class Entered:
    """A per-test registry of which injected stage bodies actually ran."""

    stages: dict[str, int] = field(default_factory=dict)

    def note(self, name: str) -> None:
        self.stages[name] = self.stages.get(name, 0) + 1

    def count(self, name: str) -> int:
        return self.stages.get(name, 0)

    def reset(self) -> None:
        self.stages.clear()


def refusal(message: str) -> KeyboardInterrupt:
    """The injected SIGINT stand-in (a KeyboardInterrupt at a stage boundary)."""
    return KeyboardInterrupt(message)


# ── Stage doubles that raise at an enumerated boundary ──────────────────────
#
# Each subclass keeps the REAL stage's constructor (so the production pipeline
# builds it identically) and replaces only ``run``. ``ParsedStage`` runs the real
# parse body first, then raises — the "after parse, before strip" boundary.


class InterruptParseStage(ParseStage):
    """ParseStage that raises KeyboardInterrupt at entry."""

    async def run(self, ctx: PipelineContext) -> dict[str, Any]:
        _ENTERED.note("parse")
        raise refusal("W8 injected SIGINT at the parse stage entry")


class InterruptAfterParseStage(ParseStage):
    """ParseStage that runs for real, then raises before its result is merged."""

    async def run(self, ctx: PipelineContext) -> dict[str, Any]:
        _ENTERED.note("parse_out")
        await super().run(ctx)
        raise refusal("W8 injected SIGINT after the parse stage body")


class InterruptStripStage(StripStage):
    """StripStage that raises KeyboardInterrupt at entry (never loads spaCy)."""

    async def run(self, ctx: PipelineContext) -> dict[str, Any]:
        _ENTERED.note("strip")
        raise refusal("W8 injected SIGINT at the strip stage entry")


class InterruptReviewStage(ReviewStage):
    """ReviewStage that raises KeyboardInterrupt at entry."""

    async def run(self, ctx: PipelineContext) -> dict[str, Any] | None:
        _ENTERED.note("review")
        raise refusal("W8 injected SIGINT at the review stage entry")


class InterruptAfterReviewStage(ReviewStage):
    """ReviewStage whose body completes (an empty report), then raises at exit.

    ``_empty_report`` is the stage's own terminal body for an empty clause list;
    it is DB-free, so this represents the "review output produced, not yet merged"
    boundary without touching a provider.
    """

    async def run(self, ctx: PipelineContext) -> dict[str, Any] | None:
        _ENTERED.note("review_out")
        emptied = self._empty_report()
        if emptied:
            raise refusal("W8 injected SIGINT after the review stage body")
        return emptied


class PersistThenInterruptStripStage(StripStage):
    """StripStage that performs the REAL PII persistence, then raises.

    This is the boundary where committed state legitimately survives an
    interruption: the encrypted mapping, the stripped text, the ``pii_cache`` row
    and the ``pii_audit_trail`` row have all been written before the interrupt, so
    the oracle must find them complete rather than absent.
    """

    async def run(self, ctx: PipelineContext) -> dict[str, Any]:
        from openreview_cli.config.paths import get_data_dir
        from openreview_cli.pii.persist import persist_pii_result

        _ENTERED.note("strip_persist")
        data_dir = get_data_dir()
        persist_pii_result(
            data_dir / "openreview.db",
            document_hash="a" * 64,
            config_hash="cfg-hash",
            pii_result=synthetic_pii_result(),
            review_dir=data_dir / "reviews" / ("a" * 12),
            encryption_key="k" * 32,
            filename="doc.pdf",
        )
        raise refusal("W8 injected SIGINT after the strip stage persisted its result")


def synthetic_pii_result() -> PiiResult:
    """A minimal real ``PiiResult`` carrying one detected entity and a mapping."""
    entity = PiiEntity(
        entity_type="PERSON",
        original_value="Jane Roe",
        start=0,
        end=8,
        score=0.9,
        placeholder="[PERSON_1]",
        source="nlp",
    )
    return PiiResult(
        stripped_text="[PERSON_1] signed the agreement.",
        mapping={"PERSON_1": "Jane Roe"},
        entities=[entity],
        page_count=1,
        duration_seconds=0.01,
        warnings=[],
    )


# The five stage classes no CLI path currently wires (plan section 10, W8a: the
# enumerated boundaries include them even though only parse/strip/review are
# reachable through ``precheck review``).
class InterruptChunkStage(ChunkStage):
    """ChunkStage that raises KeyboardInterrupt at entry."""

    async def run(self, ctx: PipelineContext) -> dict[str, Any]:
        _ENTERED.note("chunk")
        raise refusal("W8 injected SIGINT at the chunk stage entry")


class InterruptRetrieveStage(RetrieveStage):
    """RetrieveStage that raises KeyboardInterrupt at entry."""

    async def run(self, ctx: PipelineContext) -> dict[str, Any]:
        _ENTERED.note("retrieve")
        raise refusal("W8 injected SIGINT at the retrieve stage entry")


class InterruptGenerateStage(GenerateStage):
    """GenerateStage that raises KeyboardInterrupt at entry."""

    async def run(self, ctx: PipelineContext) -> dict[str, Any]:
        _ENTERED.note("generate")
        raise refusal("W8 injected SIGINT at the generate stage entry")


class InterruptComparisonStage(ComparisonStage):
    """ComparisonStage that raises KeyboardInterrupt at entry."""

    async def run(self, ctx: PipelineContext) -> dict[str, Any] | None:
        _ENTERED.note("comparison")
        raise refusal("W8 injected SIGINT at the comparison stage entry")


class InterruptBenchmarkStage(BenchmarkStage):
    """BenchmarkStage that raises KeyboardInterrupt at entry."""

    async def run(self, ctx: PipelineContext) -> dict[str, Any] | None:
        _ENTERED.note("benchmark")
        raise refusal("W8 injected SIGINT at the benchmark stage entry")


class SentinelStage(Stage):
    """A trailing stage that records whether the runner ever reached it."""

    name = "sentinel"

    async def run(self, ctx: PipelineContext) -> dict[str, Any] | None:
        _ENTERED.note("sentinel")
        return {"sentinel": True}


_ENTERED = Entered()


def entered() -> Entered:
    """The module-level reachability registry (reset by the suite's fixture)."""
    return _ENTERED


# ── Explicit, isolated subprocess plumbing ──────────────────────────────────

# One child script drives the real Typer entry with an injected blocking stage, so
# the parent can deliver a real signal mid-stage. The stage class is chosen by
# ``W8_STAGE``; ``W8_STUB_PARSE`` swaps ParseStage for a pass-through so a
# downstream boundary is reached without loading the sentence tokenizer model.
_CHILD_SCRIPT = r"""
import asyncio, json, os, sys
from pathlib import Path

from openreview_cli.pipeline.adapters import parse as parse_mod
from openreview_cli.pipeline.adapters import strip as strip_mod
import openreview_cli.review.pipeline as review_pipeline
import openreview_cli.review.runner as review_runner
from openreview_cli.app import app
from typer.testing import CliRunner

SENTINEL = Path(os.environ["W8_SENTINEL"])
STAGE = os.environ["W8_STAGE"]
ARGV = json.loads(os.environ["W8_ARGV"])


def _blocker(base):
    class _Block(base):
        async def run(self, ctx):
            SENTINEL.write_text("ready")
            # Await, not time.sleep: a real stage awaits its thread-pool work, so
            # the loop stays live and a real SIGINT is delivered (asyncio's Runner
            # installs an _on_sigint handler for the duration of run()).
            await asyncio.sleep(120)
            return None

    return _Block


class _PassThroughParse(parse_mod.ParseStage):
    async def run(self, ctx):
        return {"document": None, "clauses": []}


if os.environ.get("W8_STUB_PARSE") == "1":
    parse_mod.ParseStage = _PassThroughParse
    review_runner.ParseStage = _PassThroughParse
if STAGE == "parse":
    parse_mod.ParseStage = _blocker(parse_mod.ParseStage)
    review_runner.ParseStage = parse_mod.ParseStage
elif STAGE == "strip":
    strip_mod.StripStage = _blocker(strip_mod.StripStage)
    review_runner.StripStage = strip_mod.StripStage
elif STAGE == "review":
    review_pipeline.ReviewStage = _blocker(review_pipeline.ReviewStage)

result = CliRunner().invoke(app, ARGV)
print("CHILD_EXIT", result.exit_code, file=sys.stderr)
# Propagate the CLI's own exit code so the parent observes it.
sys.exit(result.exit_code)
"""


@dataclass
class KilledChild:
    """A child process killed mid-stage, plus what the parent observed."""

    stage: str
    signalled: str
    returncode: int
    reached_sentinel: bool


def spawn_child(
    root: Path, *, stage: str, argv: list[str], stub_parse: bool
) -> subprocess.Popen[str]:
    """Start the blocking child with an explicitly isolated environment."""
    sentinel = root / "w8_sentinel"
    if sentinel.exists():
        sentinel.unlink()
    env = isolated_env(
        root,
        W8_SENTINEL=str(sentinel),
        W8_STAGE=stage,
        W8_ARGV=json.dumps(argv),
        W8_STUB_PARSE="1" if stub_parse else "0",
    )
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_SCRIPT],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_for_sentinel(root: Path, proc: subprocess.Popen[str], timeout: float = 30.0) -> bool:
    """Block until the child reports it is inside the stage, or *timeout*."""
    sentinel = root / "w8_sentinel"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sentinel.exists():
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.01)
    return False


def kill_child(proc: subprocess.Popen[str], signum: int) -> tuple[int, str, str]:
    """Send *signum*, reap the child and return (returncode, stdout, stderr)."""
    proc.send_signal(signum)
    try:
        out, err = proc.communicate(timeout=20.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return proc.returncode, out, err


def run_killed_child(
    root: Path, *, stage: str, signum: int, argv: list[str], stub_parse: bool
) -> KilledChild:
    """Spawn, wait for the stage, deliver *signum* and reap."""
    proc = spawn_child(root, stage=stage, argv=argv, stub_parse=stub_parse)
    reached = wait_for_sentinel(root, proc)
    rc, _out, _err = kill_child(proc, signum)
    name = signal.Signals(signum).name
    return KilledChild(stage=stage, signalled=name, returncode=rc, reached_sentinel=reached)


# ── Multi-process CLI plumbing (W8c) ────────────────────────────────────────

_CLI_CHILD_SCRIPT = r"""
import sys
from typer.testing import CliRunner
from openreview_cli.app import app

result = CliRunner().invoke(app, ["client", "add", sys.argv[1], "Client " + sys.argv[1]])
print("CHILD_EXIT", result.exit_code)
sys.exit(result.exit_code)
"""


def spawn_cli_writer(root: Path, client_id: str) -> subprocess.Popen[str]:
    """Start one real CLI process that writes a client row into the shared DB."""
    return subprocess.Popen(
        [sys.executable, "-c", _CLI_CHILD_SCRIPT, client_id],
        env=isolated_env(root),
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def reap_child(proc: subprocess.Popen[str]) -> tuple[int, str, str]:
    """Wait for a child with a bounded timeout and return (rc, stdout, stderr)."""
    try:
        out, err = proc.communicate(timeout=60.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return (proc.returncode if proc.returncode is not None else -999), out, err


# ── Small utilities ─────────────────────────────────────────────────────────


def read_text_best_effort(path: Path) -> str:
    """Read a text file, returning '' when it is missing or undecodable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


@contextmanager
def read_only_dir(path: Path) -> Iterator[None]:
    """chmod *path* read-only and restore it on exit.

    A no-op for the root user: ``chmod`` does not restrict root, so the suites
    that use this skip themselves when ``os.geteuid() == 0``.
    """
    original = path.stat().st_mode & 0o777
    path.chmod(0o500)
    try:
        yield
    finally:
        path.chmod(original)
