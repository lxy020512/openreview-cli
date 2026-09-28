from __future__ import annotations

import contextlib
import io
import logging
import os
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from openreview_cli.app import (
    _LOG_MAX_BYTES,
    _expire_log_files,
    _init,
    _log_retention_days,
)
from openreview_cli.config.paths import get_log_dir
from openreview_cli.gateway.redaction import (
    REDACT_PATTERNS,
    RedactingFilter,
    install_on_root_handlers,
)

_PROBE_KEY = "sk-testkey123456789"


@contextlib.contextmanager
def _root_stream() -> Iterator[io.StringIO]:
    root = logging.getLogger()
    saved = [(handler, list(handler.filters)) for handler in root.handlers]
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root.addHandler(handler)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        for owned, filters in saved:
            owned.filters[:] = filters


def test_child_logger_record_is_redacted_through_root_handler() -> None:
    with _root_stream() as stream:
        install_on_root_handlers()
        logging.getLogger("openreview_cli.parsing.pdf_parser").warning(
            "probe ANTHROPIC_API_KEY=%s", _PROBE_KEY
        )
    rendered = stream.getvalue()
    # Positive check: the record actually reached the handler. Without this a
    # dropped record would satisfy all three negative checks vacuously.
    assert "probe" in rendered
    assert "ANTHROPIC_API_KEY" not in rendered
    assert _PROBE_KEY not in rendered
    assert "testkey123456789" not in rendered


def test_install_on_root_handlers_is_idempotent() -> None:
    with _root_stream():
        install_on_root_handlers()
        install_on_root_handlers()
        for handler in logging.getLogger().handlers:
            assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1


def test_install_does_not_touch_non_root_loggers() -> None:
    child = logging.getLogger("openreview_cli.parsing.install_probe")
    with _root_stream():
        install_on_root_handlers()
        assert child.filters == []


def test_filter_redacts_exception_text() -> None:
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        "",
        0,
        "failure",
        (),
        (ValueError, ValueError("boom ANTHROPIC_API_KEY=sk-x"), None),
    )
    assert RedactingFilter(REDACT_PATTERNS).filter(record)
    assert record.exc_text is not None
    assert "ANTHROPIC_API_KEY" not in record.exc_text


def test_filter_redacts_key_value_body() -> None:
    record = logging.LogRecord(
        "test", logging.INFO, "", 0, "using sk-live-abcdefghijklmn", (), None
    )
    assert RedactingFilter(REDACT_PATTERNS).filter(record)
    assert "abcdefghijklmn" not in record.getMessage()


# ── #161: a bounded, expiring log file ──────────────────────────────────────

_DAY = 86400.0
_NOW = 1_700_000_000.0


@pytest.fixture
def isolated_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Pin all four XDG roots so no `_init` call can reach the developer's tree."""
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    return tmp_path


@pytest.fixture(autouse=True)
def _drop_owned_root_handlers() -> Iterator[None]:
    """Drop any handler `_init` installed so it cannot leak into later tests."""
    yield
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if getattr(handler, "_openreview_owned", False):
            root.removeHandler(handler)
            handler.close()


def test_init_installs_a_size_bounded_rotating_handler(isolated_xdg: Path) -> None:
    _init()

    owned = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_openreview_owned", False) and isinstance(handler, logging.FileHandler)
    ]
    assert len(owned) == 1
    handler = owned[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == _LOG_MAX_BYTES
    assert handler.backupCount == 5


def test_the_log_family_is_size_bounded(isolated_xdg: Path) -> None:
    """Writing past the cap rotates a segment and keeps the active file under it."""
    _init()
    _init()  # a second invoke must replace, not stack, the owned handlers

    log_dir = get_log_dir()
    probe = logging.getLogger("openreview_cli.log_probe")
    chunk = "x" * (1024 * 1024)
    for _ in range(_LOG_MAX_BYTES // len(chunk) + 2):
        probe.warning("probe %s", chunk)
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert (log_dir / "openreview.log.1").exists(), "no rotated segment was produced"
    assert (log_dir / "openreview.log").stat().st_size <= _LOG_MAX_BYTES


@pytest.mark.parametrize(
    ("log_ttl_days", "logs_keep_days", "expected"),
    [(30, 30, 30), (7, 30, 7), (30, 7, 7)],
)
def test_log_retention_is_the_shorter_window(
    log_ttl_days: int, logs_keep_days: int, expected: int
) -> None:
    config = {
        "privacy": {"log_ttl_days": log_ttl_days},
        "storage": {"logs_keep_days": logs_keep_days},
    }
    assert _log_retention_days(config) == expected


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"privacy": {}, "storage": {}},
        {"privacy": {"log_ttl_days": 0}, "storage": {"logs_keep_days": 30}},
        {"privacy": {"log_ttl_days": "not-a-number"}, "storage": {"logs_keep_days": 30}},
        {"privacy": None, "storage": None},
    ],
)
def test_log_retention_defaults_to_thirty_days(config: dict[str, object]) -> None:
    assert _log_retention_days(config) == 30


def _touch(path: Path, mtime: float) -> None:
    path.write_text(path.name, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_expire_log_files_removes_only_segments_outside_the_window(tmp_path: Path) -> None:
    active = tmp_path / "openreview.log"
    stale_backup = tmp_path / "openreview.log.1"
    sibling = tmp_path / "other.log"
    _touch(active, _NOW)
    _touch(stale_backup, _NOW - 40 * _DAY)
    _touch(sibling, _NOW - 40 * _DAY)

    removed = _expire_log_files(tmp_path, 30, now=_NOW)

    assert removed == 1
    assert active.exists()
    assert not stale_backup.exists(), "a segment older than the window must be removed"
    assert sibling.exists(), "a non-matching sibling must never be touched"


def test_expire_log_files_keeps_everything_fresh(tmp_path: Path) -> None:
    active = tmp_path / "openreview.log"
    backup = tmp_path / "openreview.log.1"
    _touch(active, _NOW)
    _touch(backup, _NOW - _DAY)

    assert _expire_log_files(tmp_path, 30, now=_NOW) == 0
    assert active.exists()
    assert backup.exists()


def test_expire_log_files_never_removes_the_active_file(tmp_path: Path) -> None:
    active = tmp_path / "openreview.log"
    stale_backup = tmp_path / "openreview.log.1"
    _touch(active, _NOW - 40 * _DAY)
    _touch(stale_backup, _NOW - 40 * _DAY)

    assert _expire_log_files(tmp_path, 30, now=_NOW) == 1
    assert active.exists(), "the active log must never be unlinked while a process holds it"
    assert not stale_backup.exists()
