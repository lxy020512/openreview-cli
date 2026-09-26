"""Exploratory probes: the 23 named product modes plus ``precheck``.

Each product mode is a thin wrapper around the shared ``_run_product_review``
(``app.py:3326``).  This file parametrizes over every registered mode name so a
mode that silently diverges from the shared contract is caught, and records the
``precheck`` / ``compare`` flag-conflict behaviour.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import Result

from openreview_cli.product_modes import GENERIC_MODE, PRODUCT_MODES

EXIT_USER_ERROR = 1
EXIT_USAGE = 2

NAMED_MODES: list[str] = [mode.name for mode in PRODUCT_MODES if not mode.generic]
ALL_MODE_COMMANDS: list[str] = [GENERIC_MODE, *NAMED_MODES]


def _text(result: Result) -> str:
    raw = (getattr(result, "output", "") or "") + (getattr(result, "stderr", "") or "")
    return " ".join(raw.split())


@pytest.mark.fast
def test_all_23_named_modes_plus_precheck_are_registered() -> None:
    """The surface is exactly 23 named modes plus the generic ``precheck``."""
    assert len(NAMED_MODES) == 23, NAMED_MODES
    assert GENERIC_MODE == "precheck"

    import openreview_cli.app as app_module

    registered = {c.name for c in app_module.app.registered_commands}
    missing = set(NAMED_MODES) - registered
    assert not missing, f"modes not registered as commands: {sorted(missing)}"


@pytest.mark.fast
@pytest.mark.parametrize("mode", NAMED_MODES)
def test_every_mode_missing_document_is_user_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path, mode: str
) -> None:
    """Every named mode reaches the shared review path and reports a missing document.

    Reachability: the run must get past argument parsing into
    ``_run_product_review`` and emit the shared "No documents processed." line.
    ``precheck`` is excluded because it is a Typer group taking ``--document``,
    not a positional path; it is covered separately below.
    """
    result = invoke([mode, str(tmp_path / "missing.pdf")])
    assert result.exit_code == EXIT_USER_ERROR, (mode, result.exit_code, _text(result))
    assert "No documents processed" in _text(result), (mode, _text(result))


@pytest.mark.fast
@pytest.mark.parametrize("mode", NAMED_MODES)
def test_every_named_mode_rejects_unknown_format(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path, mode: str
) -> None:
    """``--format`` is validated by the shared emitter for every named mode."""
    result = invoke([mode, str(tmp_path / "missing.pdf"), "--format", "xml"])
    assert result.exit_code == EXIT_USAGE, (mode, result.exit_code, _text(result))
    assert "--format must be" in _text(result), (mode, _text(result))


@pytest.mark.fast
def test_named_mode_rejects_out_of_range_confidence_threshold(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    result = invoke(["licensecheck", str(tmp_path / "missing.pdf"), "--confidence-threshold", "9"])
    assert result.exit_code == EXIT_USAGE, (result.exit_code, _text(result))
    assert "0.0 and 1.0" in _text(result)


@pytest.mark.fast
@pytest.mark.parametrize(
    ("value", "token"),
    [("x", "MODE=VALUE"), ("leasecheck=y", "VALUE must be a float")],
)
def test_named_mode_rejects_malformed_mode_threshold(
    isolated_dirs: Path,
    invoke: Callable[[list[str]], Result],
    tmp_path: Path,
    value: str,
    token: str,
) -> None:
    result = invoke(["leasecheck", str(tmp_path / "missing.pdf"), "--mode-threshold", value])
    assert result.exit_code == EXIT_USAGE, (value, result.exit_code, _text(result))
    assert token in _text(result), (value, _text(result))


# ── precheck and its subcommands ──────────────────────────────────────────


@pytest.mark.fast
def test_precheck_document_missing_is_user_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    result = invoke(["precheck", "--document", str(tmp_path / "missing.pdf")])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    assert "Error" in _text(result)


@pytest.mark.fast
def test_precheck_is_a_group_not_a_positional_mode(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    """``precheck`` is a Typer group: bare invocation is help (exit 2), and a
    positional path is an unexpected extra argument (exit 2)."""
    bare = invoke(["precheck"])
    assert bare.exit_code == EXIT_USAGE, (bare.exit_code, _text(bare))
    assert "Commands" in _text(bare)

    positional = invoke(["precheck", str(tmp_path / "missing.pdf")])
    assert positional.exit_code == EXIT_USAGE, (positional.exit_code, _text(positional))


@pytest.mark.fast
def test_precheck_requires_a_document_path(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    """An option that is not a subcommand reaches the callback with no document."""
    result = invoke(["precheck", "--no-pii"])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    assert "missing document path" in _text(result)


@pytest.mark.fast
def test_precheck_review_missing_document_is_user_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    result = invoke(["precheck", "review", str(tmp_path / "missing.pdf")])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    assert "No documents processed" in _text(result)


@pytest.mark.fast
def test_precheck_flag_conflict_uses_exit_3_not_usage(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    """RT-008 (deliberate trade-off): ``--no-pii`` with ``--pii-threshold`` is a
    mutually-exclusive-flag error, yet it exits 3 (``EXIT_NOT_FOUND``, "requested
    resource does not exist") rather than 2 (``EXIT_USAGE``).  ``compare`` does
    the same (``app.py:1897``) and that path is already asserted by
    ``tests/unit/test_bilateral_comparison.py``."""
    result = invoke(
        ["precheck", "--no-pii", "--pii-threshold", "0.5", "--document", str(tmp_path / "x.pdf")]
    )
    assert result.exit_code == 3, (result.exit_code, _text(result))
    assert "mutually exclusive" in _text(result)


# ── negative control ──────────────────────────────────────────────────────


@pytest.mark.fast
def test_negative_control_oracle_rejects_a_wrong_exit_code(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    """Negative control: flip the inner assertion (or drop ``pytest.raises``)
    and this case fails."""
    result = invoke(["licensecheck", str(tmp_path / "missing.pdf")])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    with pytest.raises(AssertionError):
        assert result.exit_code == 0, "negative control: exit 1 is not exit 0"
