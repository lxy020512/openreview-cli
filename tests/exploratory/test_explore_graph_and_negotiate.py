"""Exploratory probes: the graph group and the ``negotiate`` command.

Extends the existing ``--weights`` findings in ``test_explore_cli_validation.py``
to ``--solver`` and to the format/weight interactions, and characterises the
``graph health`` weight parser.  Findings are ``RT-NNN`` register rows.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import Result

EXIT_USER_ERROR = 1
EXIT_USAGE = 2

_EMPTY_GRAPH = {"nodes": [], "edges": [], "metadata": {}}


def _text(result: Result) -> str:
    raw = (getattr(result, "output", "") or "") + (getattr(result, "stderr", "") or "")
    return " ".join(raw.split())


def _write_graph(path: Path) -> Path:
    path.write_text(json.dumps(_EMPTY_GRAPH), encoding="utf-8")
    return path


# ── graph: argument validation ────────────────────────────────────────────


@pytest.mark.fast
def test_graph_build_missing_input_is_user_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    result = invoke(["graph", "build", str(tmp_path / "missing.json")])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    assert "File not found" in _text(result)


@pytest.mark.fast
@pytest.mark.parametrize("command", ["metrics", "health", "view"])
def test_graph_readers_require_a_source(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], command: str
) -> None:
    """With neither a path nor ``--from-db`` the readers exit 2."""
    result = invoke(["graph", command])
    assert result.exit_code == EXIT_USAGE, (command, result.exit_code, _text(result))
    assert "GRAPH_PATH argument or --from-db required" in _text(result)


@pytest.mark.fast
@pytest.mark.parametrize("command", ["metrics", "health", "view"])
def test_graph_from_db_requires_contract_id(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], command: str
) -> None:
    result = invoke(["graph", command, "--from-db"])
    assert result.exit_code == EXIT_USAGE, (command, result.exit_code, _text(result))
    assert "--contract-id required with --from-db" in _text(result)


@pytest.mark.fast
def test_graph_diff_missing_files_is_user_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    result = invoke(["graph", "diff", str(tmp_path / "a.json"), str(tmp_path / "b.json")])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    assert "File not found" in _text(result)


# ── graph: --weights parser (extends the negotiate --weights findings) ─────


@pytest.mark.fast
@pytest.mark.parametrize(
    ("weights", "token"),
    [
        ("", "exactly 5 values"),
        ("1 2 3 4", "exactly 5 values"),
        ("1 2 3 4 5 6", "exactly 5 values"),
        ("a b c d e", "valid floats"),
        ("-1 1 1 1 1", "non-negative"),
    ],
)
def test_graph_health_rejects_malformed_weights(
    isolated_dirs: Path,
    invoke: Callable[[list[str]], Result],
    tmp_path: Path,
    weights: str,
    token: str,
) -> None:
    """FINDING: unlike ``negotiate``, ``graph health`` rejects the empty string
    (``--weights ""`` -> "exactly 5 values"), but ``nan``/``inf`` pass the
    non-negativity check and are only rejected downstream with an internal
    message ("cannot convert float NaN to integer")."""
    graph = _write_graph(tmp_path / "g.json")
    result = invoke(["graph", "health", str(graph), "--weights", weights])
    assert result.exit_code == EXIT_USAGE, (weights, result.exit_code, _text(result))
    assert token in _text(result), (weights, _text(result))


@pytest.mark.fast
@pytest.mark.parametrize("weights", ["nan nan nan nan nan", "inf 0 0 0 0"])
def test_graph_health_nan_and_inf_weights_are_rejected_downstream(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path, weights: str
) -> None:
    """``nan``/``inf`` reach ``compute_health`` and blow up there."""
    graph = _write_graph(tmp_path / "g.json")
    result = invoke(["graph", "health", str(graph), "--weights", weights])
    assert result.exit_code == EXIT_USAGE, (weights, result.exit_code, _text(result))
    assert "NaN" in _text(result) or "nan" in _text(result)


@pytest.mark.fast
def test_graph_health_accepts_weights_and_scores(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    graph = _write_graph(tmp_path / "g.json")
    result = invoke(["graph", "health", str(graph), "--weights", "0.2 0.2 0.2 0.2 0.2"])
    assert result.exit_code == 0, (result.exit_code, _text(result))
    assert "Health Score:" in _text(result)


@pytest.mark.fast
def test_graph_health_all_zero_weights_fall_back_to_defaults(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    """Documented trade-off: a zero-sum weight vector falls back to defaults
    (``graph/health.py:normalise_weights``), so an empty graph scores 100."""
    graph = _write_graph(tmp_path / "g.json")
    result = invoke(["graph", "health", str(graph), "--weights", "0 0 0 0 0"])
    assert result.exit_code == 0, (result.exit_code, _text(result))
    assert "Health Score: 100/100" in _text(result)


# ── negotiate: --solver and format interactions ───────────────────────────


@pytest.mark.fast
@pytest.mark.parametrize("solver", ["bogus", "NASH", "QRE", "level-k", ""])
def test_negotiate_rejects_unknown_or_miscased_solver(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], solver: str
) -> None:
    """FINDING (extends the ``--format`` case-sensitivity finding to
    ``--solver``): the solver enum is exact-match and case-sensitive, and unlike
    ``--weights ""`` an empty ``--solver ""`` is *rejected* rather than ignored."""
    result = invoke(["negotiate", "nonexistent.pdf", "--solver", solver])
    assert result.exit_code == EXIT_USAGE, (solver, result.exit_code, _text(result))
    assert "--solver" in _text(result)


@pytest.mark.fast
@pytest.mark.parametrize("fmt", ["xml", "yaml", "TABLE", ""])
def test_negotiate_rejects_unknown_format(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], fmt: str
) -> None:
    result = invoke(["negotiate", "nonexistent.pdf", "--format", fmt])
    assert result.exit_code == EXIT_USAGE, (fmt, result.exit_code, _text(result))
    assert "--format" in _text(result)


@pytest.mark.fast
def test_negotiate_empty_weights_is_a_silent_no_op_but_empty_solver_is_rejected(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    """Documented trade-off contrast: ``--weights ""`` is falsy so validation is
    skipped entirely and the run proceeds as if the flag were absent, while
    ``--solver ""`` fails the enum check.  Characterised, not a new defect."""
    missing = str(tmp_path / "missing.pdf")
    weights = invoke(["negotiate", missing, "--weights", ""])
    assert weights.exit_code == EXIT_USER_ERROR, (weights.exit_code, _text(weights))
    assert "file not found" in _text(weights).lower()
    assert "--weights" not in _text(weights).lower()

    solver = invoke(["negotiate", missing, "--solver", ""])
    assert solver.exit_code == EXIT_USAGE, (solver.exit_code, _text(solver))
    assert "--solver" in _text(solver)


@pytest.mark.fast
@pytest.mark.parametrize("weights", ["nan,nan,nan", "inf,0,0", "1,1,1", "0,0,0"])
def test_negotiate_weights_accept_nan_inf_and_unnormalised(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path, weights: str
) -> None:
    """FINDING (edge case, already recorded in ``test_explore_cli_validation.py``):
    validation checks only count, numeric-ness and non-negativity, so
    ``nan``/``inf`` slip through and no "must sum to ~1.0" check exists."""
    missing = str(tmp_path / "missing.pdf")
    result = invoke(["negotiate", missing, "--weights", weights])
    assert result.exit_code == EXIT_USER_ERROR, (weights, result.exit_code, _text(result))
    assert "file not found" in _text(result).lower()


@pytest.mark.fast
@pytest.mark.parametrize("value", ["1.5", "-0.1", "2", "nan"])
def test_negotiate_confidence_threshold_out_of_range(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], value: str
) -> None:
    result = invoke(["negotiate", "nonexistent.pdf", "--confidence-threshold", value])
    assert result.exit_code == EXIT_USAGE, (value, result.exit_code, _text(result))
    assert "0.0 and 1.0" in _text(result)


@pytest.mark.fast
def test_negotiate_missing_document_is_user_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    result = invoke(["negotiate", str(tmp_path / "missing.pdf")])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    assert "File not found" in _text(result)


# ── negative control ──────────────────────────────────────────────────────


@pytest.mark.fast
def test_negative_control_oracle_rejects_a_wrong_exit_code(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    """Negative control: flipping the inner assertion (or dropping
    ``pytest.raises``) makes this case fail."""
    result = invoke(["graph", "health"])
    assert result.exit_code == EXIT_USAGE, (result.exit_code, _text(result))
    with pytest.raises(AssertionError):
        assert result.exit_code == 0, "negative control: exit 2 is not exit 0"
