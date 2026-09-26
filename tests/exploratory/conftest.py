"""Shared fixtures for the exploratory probes.

Every CLI probe redirects platformdirs to a throwaway tmp tree so the real
user data/config/log directories are never touched by ``_init``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from openreview_cli.app import app


@pytest.fixture(autouse=True)
def _isolated_platformdirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect platformdirs to a per-test tmp tree for *every* probe.

    A probe must never write into the developer's real platformdirs tree
    (``~/.config``, ``~/.local/share``, ``~/.local/state``, ``~/.cache``).  The
    first ``_init`` for any command creates and seeds those directories, so the
    redirection is unconditional (autouse) rather than opt-in: forgetting to
    request ``isolated_dirs`` can no longer leak real user data.  ``isolated_dirs``
    below is kept for the existing probes that still request it.
    """
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("OPENREVIEW_OUTPUT_DIR", raising=False)


@pytest.fixture
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point platformdirs at a fresh tmp tree and return the tmp root."""
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("OPENREVIEW_OUTPUT_DIR", raising=False)
    return tmp_path


@pytest.fixture
def invoke() -> Callable[[list[str]], Result]:
    """Return a CliRunner-backed ``invoke(args)`` callable."""
    runner = CliRunner()
    return lambda args: runner.invoke(app, args)
