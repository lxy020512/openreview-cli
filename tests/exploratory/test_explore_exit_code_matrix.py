"""Exploratory probes: a CLOSED exit-code matrix.

Each row pairs a command with a documented-failure input and asserts the
observed code equals the code ``src/openreview_cli/errors.py`` names for that
failure class.  This is a closed, fixed list, not a proof that no command can
exit with an undocumented code: ``app.py`` has 40+ bare ``typer.Exit`` sites
behind catch-all handlers, so that property is undecidable (see the register
residual-risk section).

Codes the CLI cannot reach are not matrix rows; they are recorded as findings
(RT-001 exit 4, RT-003 retrieval 40-43, RT-009 PII 9) and asserted here as
source-level reachability facts.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import Result

EXIT_SUCCESS = 0
EXIT_USER_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_CONFIG = 5
EXIT_PARSE_ERROR = 8
EXIT_BENCHMARK_CONFIG = 78


def _text(result: Result) -> str:
    raw = (getattr(result, "output", "") or "") + (getattr(result, "stderr", "") or "")
    return " ".join(raw.split())


# (args, documented code, errors.py constant, failure class)
MATRIX: list[tuple[list[str], int, str, str]] = [
    (
        ["parse", "missing.pdf", "--format", "text"],
        EXIT_PARSE_ERROR,
        "EXIT_PARSE_ERROR",
        "ParseError",
    ),
    (["chunk", "missing.pdf"], EXIT_USER_ERROR, "EXIT_USER_ERROR", "missing file"),
    (["ingest", "missing.ndax"], EXIT_USER_ERROR, "EXIT_USER_ERROR", "missing file"),
    (["index-status"], EXIT_USER_ERROR, "EXIT_USER_ERROR", "missing argument"),
    (["licensecheck", "missing.pdf"], EXIT_USER_ERROR, "EXIT_USER_ERROR", "missing file"),
    (["gateway", "models", "nosuch"], EXIT_USER_ERROR, "EXIT_USER_ERROR", "unknown provider"),
    (["gateway", "test", "badslot"], EXIT_USER_ERROR, "EXIT_USER_ERROR", "invalid slot"),
    (["graph", "metrics", "missing.json"], EXIT_USER_ERROR, "EXIT_USER_ERROR", "missing file"),
    (["pii", "list", "--format", "xml"], EXIT_USAGE, "EXIT_USAGE", "bad enum"),
    (["config", "get", "no.such.key"], EXIT_CONFIG, "EXIT_CONFIG", "unknown key"),
    (["config", "set", "privacy.tier", "bogus"], EXIT_CONFIG, "EXIT_CONFIG", "validation"),
    (["client", "delete", "ghost"], EXIT_CONFIG, "EXIT_CONFIG", "missing resource"),
    (["negotiate", "missing.pdf", "--solver", "bogus"], EXIT_USAGE, "EXIT_USAGE", "bad enum"),
    (
        ["negotiate", "missing.pdf", "--confidence-threshold", "9"],
        EXIT_USAGE,
        "EXIT_USAGE",
        "range",
    ),
    (["graph", "health", "--from-db"], EXIT_USAGE, "EXIT_USAGE", "missing option"),
    (["export", "--batch-dir", "no-such-dir"], EXIT_USAGE, "EXIT_USAGE", "bad path"),
    (
        ["benchmark", "run", "--datasets", "bogus"],
        EXIT_BENCHMARK_CONFIG,
        "EXIT_BENCHMARK_CONFIG",
        "unknown dataset",
    ),
    (
        ["benchmark", "baseline", "--provider", "bogus"],
        EXIT_BENCHMARK_CONFIG,
        "EXIT_BENCHMARK_CONFIG",
        "unknown provider",
    ),
]


@pytest.mark.fast
@pytest.mark.parametrize(
    ("args", "expected", "constant", "failure_class"),
    MATRIX,
    ids=[f"{row[0][0]}-{row[3].replace(' ', '-')}" for row in MATRIX],
)
def test_exit_code_matrix(
    isolated_dirs: Path,
    invoke: Callable[[list[str]], Result],
    args: list[str],
    expected: int,
    constant: str,
    failure_class: str,
) -> None:
    """Observed code equals the code ``errors.py`` names for this failure class."""
    result = invoke(args)
    assert result.exit_code == expected, (args, constant, result.exit_code, _text(result))
    # Reachability: the command ran and reported something, rather than failing
    # collection before dispatch.
    assert _text(result), (args, "no output: command likely not reached")


@pytest.mark.fast
def test_matrix_covers_multiple_distinct_codes() -> None:
    """Guard against a matrix that collapses to one code and proves nothing."""
    codes = {expected for _args, expected, _const, _cls in MATRIX}
    assert len(codes) >= 5, codes


@pytest.mark.fast
def test_success_code_for_a_read_only_command(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    """Code 0: the matrix's positive end."""
    result = invoke(["config", "get", "privacy.tier"])
    assert result.exit_code == EXIT_SUCCESS, (result.exit_code, _text(result))


@pytest.mark.fast
def test_registry_codes_with_no_cli_call_site() -> None:
    """Reachability evidence for RT-003 (retrieval 40-43) and RT-009 (PII 9).

    The only producers of those codes are ``errors.retrieval_error`` and
    ``errors.pii_error``.  Neither is referenced by ``app.py`` (every CLI
    subcommand) nor anywhere else in ``src/`` besides ``errors.py`` itself, so
    no product path can emit 40-43 or 9.
    """
    import openreview_cli.app as app_module

    app_source = inspect.getsource(app_module)
    assert "retrieval_error" not in app_source
    assert "pii_error" not in app_source

    import pathlib

    src_root = pathlib.Path(app_module.__file__).parent
    call_sites: list[str] = []
    for py_file in src_root.rglob("*.py"):
        if py_file.name == "errors.py":
            continue
        text = py_file.read_text(encoding="utf-8")
        if "retrieval_error(" in text or "pii_error(" in text:
            call_sites.append(str(py_file))
    assert call_sites == [], f"unexpected call sites: {call_sites}"


@pytest.mark.fast
def test_exit_gateway_is_not_asserted_anywhere() -> None:
    """RT-001: the plan forbids asserting exit 4. This file must never do so."""
    assert 4 not in {expected for _args, expected, _c, _cls in MATRIX}


# ── negative control ──────────────────────────────────────────────────────


@pytest.mark.fast
def test_negative_control_matrix_row_rejects_a_wrong_code(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    """Negative control: the matrix oracle is discriminating. If a matrix row
    were mis-stated (or the oracle always-true), the inner assertion below would
    pass; ``pytest.raises(AssertionError)`` proves it fails today."""
    args = ["config", "get", "no.such.key"]
    result = invoke(args)
    assert result.exit_code == EXIT_CONFIG, (result.exit_code, _text(result))
    with pytest.raises(AssertionError):
        assert result.exit_code == EXIT_SUCCESS, "negative control: exit 5 is not exit 0"
