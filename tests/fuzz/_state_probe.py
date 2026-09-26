"""Shared probe harness for the W4 state fuzzing suites.

Private to ``tests/fuzz`` (the plan keeps the fuzz tree conftest-free, so nothing
here is a fixture). It gives the config, auth, SQLite and playbook suites three
things:

* :func:`prepare_state` redirects every platformdirs XDG root — including
  ``XDG_STATE_HOME``, which ``platformdirs.user_log_dir`` reads and the shared
  ``isolated_xdg`` fixture does not — at a per-test ``tmp_path`` and seeds a valid
  ``config.yml`` plus a migrated database. No test ever touches the developer's
  real config/data/state/cache trees.
* :func:`count_calls` is the anti-vacuity counter (plan section 9.3, rule 2): it
  wraps one function on a module, counts entry, and restores it afterwards. A
  counter assertion stays true before and after a fix, so it composes with
  ``xfail(strict=True)``.
* :func:`assert_clean_failure` is the shared oracle: a malformed state file must
  terminate in a code from ``errors.py`` with a documented token and no raw
  traceback / uncaught exception.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from openreview_cli.app import app
from openreview_cli.storage import init_database

XDG_VARS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")

TRACEBACK_MARKER = "Traceback (most recent call last)"

# The seeded, valid config the shared ``isolated_xdg`` fixture also uses.
VALID_CONFIG = "privacy:\n  tier: balanced\ngateway:\n  models: {}\n"


@dataclass
class State:
    """The isolated XDG tree a suite drives the CLI against."""

    root: Path
    config_dir: Path
    config_path: Path
    data_dir: Path
    db_path: Path
    auth_path: Path


def prepare_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> State:
    """Redirect all four XDG roots at *tmp_path* and seed a valid config + DB."""
    for var in XDG_VARS:
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("OPENREVIEW_OUTPUT_DIR", raising=False)

    config_dir = tmp_path / "xdg_config_home" / "openreview"
    data_dir = tmp_path / "xdg_data_home" / "openreview"
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    config_path = config_dir / "config.yml"
    config_path.write_text(VALID_CONFIG, encoding="utf-8")

    db_path = data_dir / "openreview.db"
    init_database(db_path)

    return State(
        root=tmp_path,
        config_dir=config_dir,
        config_path=config_path,
        data_dir=data_dir,
        db_path=db_path,
        auth_path=config_dir / "auth.json",
    )


def run_cli(args: list[str]) -> Result:
    """Invoke the real Typer app with *args* through a one-shot CliRunner."""
    return CliRunner().invoke(app, args)


@contextmanager
def count_calls(target: object, attr: str) -> Iterator[list[int]]:
    """Count calls to ``target.attr``, restoring the original on exit."""
    real: Any = getattr(target, attr)
    hits = [0]

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        hits[0] += 1
        return real(*args, **kwargs)

    setattr(target, attr, wrapper)
    try:
        yield hits
    finally:
        setattr(target, attr, real)


def assert_clean_failure(result: Result, allowed: frozenset[int], token: str | None = None) -> None:
    """Assert *result* is a clean failure: an allowed code, an optional token, no crash.

    A clean failure exits through a code from ``errors.py`` with ``result.exception``
    being the ``SystemExit`` Typer raises — never a raw ``yaml.YAMLError``,
    ``ValidationError``, ``JSONDecodeError`` or ``sqlite3.DatabaseError`` — and
    never prints a traceback.
    """
    assert result.exit_code in allowed, (
        f"expected exit in {sorted(allowed)}, got {result.exit_code}; "
        f"exception={result.exception!r}; output={result.output!r}"
    )
    assert isinstance(result.exception, SystemExit), (
        f"uncaught {type(result.exception).__name__}: {result.exception!r}"
    )
    assert TRACEBACK_MARKER not in result.output, result.output
    if token is not None:
        assert token in result.output, f"{token!r} not in {result.output!r}"
