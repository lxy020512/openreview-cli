"""Exploratory probes: the error-group commands ``client``, ``config``, ``pii``
and ``playbook``.

These characterise boundary behaviour for argument validation, missing
resources and success messages, and record surprises as ``RT-NNN`` findings.
Probes that assert the *intended* behaviour of a known-open defect are marked
``xfail(strict=True, reason="RT-NNN")`` so the file stays green while the row
is open (plan section 9.4).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import Result

EXIT_USER_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFIG = 5


def _text(result: Result) -> str:
    raw = (getattr(result, "output", "") or "") + (getattr(result, "stderr", "") or "")
    return " ".join(raw.split())


# ── client ────────────────────────────────────────────────────────────────


@pytest.mark.fast
def test_client_add_then_duplicate_is_config_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    """A fresh add succeeds; a duplicate id is a config error (exit 5)."""
    first = invoke(["client", "add", "acme", "Acme Inc"])
    assert first.exit_code == 0, (first.exit_code, _text(first))
    assert "added client acme" in _text(first)

    second = invoke(["client", "add", "acme", "Acme Again"])
    assert second.exit_code == EXIT_CONFIG, (second.exit_code, _text(second))
    assert "Config error" in _text(second)


@pytest.mark.fast
def test_client_delete_missing_is_config_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    result = invoke(["client", "delete", "ghost"])
    assert result.exit_code == EXIT_CONFIG, (result.exit_code, _text(result))
    assert "not found" in _text(result)


@pytest.mark.fast
def test_client_list_on_empty_db_succeeds(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    result = invoke(["client", "list"])
    assert result.exit_code == 0, (result.exit_code, _text(result))
    assert "Clients" in _text(result)


# ── config ────────────────────────────────────────────────────────────────


@pytest.mark.fast
def test_config_show_succeeds(isolated_dirs: Path, invoke: Callable[[list[str]], Result]) -> None:
    result = invoke(["config", "show"])
    assert result.exit_code == 0, (result.exit_code, _text(result))
    assert (result.output or "").strip(), "config show must render a table"


@pytest.mark.fast
def test_config_get_known_key_prints_value(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    result = invoke(["config", "get", "privacy.tier"])
    assert result.exit_code == 0, (result.exit_code, _text(result))
    assert "balanced" in _text(result)


@pytest.mark.fast
def test_config_get_unknown_key_is_config_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    result = invoke(["config", "get", "no.such.key"])
    assert result.exit_code == EXIT_CONFIG, (result.exit_code, _text(result))
    assert "Unknown config key" in _text(result)


@pytest.mark.fast
def test_config_set_invalid_value_is_config_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    """A wrong-typed value for a typed key exits 5, not a raw pydantic error."""
    result = invoke(["config", "set", "privacy.tier", "bogus"])
    assert result.exit_code == EXIT_CONFIG, (result.exit_code, _text(result))
    assert "Config error" in _text(result)


@pytest.mark.fast
def test_config_set_known_key_persists(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    """A valid set is persisted and readable back through ``config get``."""
    set_result = invoke(["config", "set", "retrieval.top_k", "7"])
    assert set_result.exit_code == 0, (set_result.exit_code, _text(set_result))
    get_result = invoke(["config", "get", "retrieval.top_k"])
    assert get_result.exit_code == 0, (get_result.exit_code, _text(get_result))
    assert _text(get_result).strip() == "7"


@pytest.mark.fast
def test_config_set_unknown_key_is_greppable_after_set(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    """RT-005: ``config set <unknown.key> x`` prints ``updated`` but the value is
    silently dropped by validation, so ``config get`` then rejects it with exit 5.

    Intended: a key that ``set`` reports as written must be readable back, or the
    set must fail loudly.
    """
    set_result = invoke(["config", "set", "rt005.unknown.key", "x"])
    assert set_result.exit_code == 0, (set_result.exit_code, _text(set_result))
    get_result = invoke(["config", "get", "rt005.unknown.key"])
    assert get_result.exit_code == 0, (get_result.exit_code, _text(get_result))
    assert _text(get_result).strip() == "x"


# ── pii ───────────────────────────────────────────────────────────────────


@pytest.mark.fast
def test_pii_list_empty_db_is_valid_empty(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    table = invoke(["pii", "list"])
    assert table.exit_code == 0, (table.exit_code, _text(table))
    assert "Documents with PII data" in _text(table)

    as_json = invoke(["pii", "list", "--format", "json"])
    assert as_json.exit_code == 0, (as_json.exit_code, _text(as_json))
    assert (as_json.output or "").strip() == "[]"


@pytest.mark.fast
def test_pii_delete_missing_long_hash_reports_nothing(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    result = invoke(["pii", "delete", "deadbeefcafe"])
    assert result.exit_code == 0, (result.exit_code, _text(result))
    assert "No PII data found" in _text(result)


@pytest.mark.fast
def test_pii_delete_short_hash_is_a_clean_usage_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    """RT-002: ``pii delete`` documents a minimum 8-char prefix, but a shorter
    hash raises an uncaught ``ValueError`` from the library guard
    (``pii/retention.py:72``) and surfaces as a raw traceback / empty output.

    Intended: a clean usage error (exit 2) naming the constraint.
    """
    result = invoke(["pii", "delete", "abc"])
    assert result.exit_code == EXIT_USAGE, (result.exit_code, _text(result))
    assert "at least 8" in _text(result)


@pytest.mark.fast
def test_pii_cleanup_and_dry_run_succeed(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    dry = invoke(["pii", "cleanup", "--dry-run"])
    assert dry.exit_code == 0, (dry.exit_code, _text(dry))
    assert "Dry run" in _text(dry)

    real = invoke(["pii", "cleanup"])
    assert real.exit_code == 0, (real.exit_code, _text(real))
    assert "Cleanup complete" in _text(real)


# ── playbook ──────────────────────────────────────────────────────────────


@pytest.mark.fast
def test_playbook_list_on_empty_db_succeeds(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    result = invoke(["playbook", "list"])
    assert result.exit_code == 0, (result.exit_code, _text(result))
    assert "No playbooks saved yet" in _text(result)


@pytest.mark.fast
def test_playbook_show_missing_is_usage_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    result = invoke(["playbook", "show", "nosuch", "1"])
    assert result.exit_code == EXIT_USAGE, (result.exit_code, _text(result))
    assert "not found" in _text(result)


@pytest.mark.fast
def test_playbook_show_non_positive_version_is_usage_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    result = invoke(["playbook", "show", "nosuch", "0"])
    assert result.exit_code == EXIT_USAGE, (result.exit_code, _text(result))
    assert "positive integer" in _text(result)


@pytest.mark.fast
def test_playbook_export_requires_id_and_output(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    no_id = invoke(["playbook", "export"])
    assert no_id.exit_code == EXIT_USAGE, (no_id.exit_code, _text(no_id))
    assert "PLAYBOOK_ID or --all" in _text(no_id)

    no_output = invoke(["playbook", "export", "nosuch"])
    assert no_output.exit_code == EXIT_USAGE, (no_output.exit_code, _text(no_output))
    assert "--output is required" in _text(no_output)


@pytest.mark.fast
def test_playbook_delete_without_id_is_usage_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    result = invoke(["playbook", "delete"])
    assert result.exit_code == EXIT_USAGE, (result.exit_code, _text(result))
    assert "PLAYBOOK_ID or --all" in _text(result)


@pytest.mark.fast
def test_playbook_undelete_missing_is_user_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    result = invoke(["playbook", "undelete", "nosuch"])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    assert "not found or is not deleted" in _text(result)


@pytest.mark.fast
def test_playbook_import_missing_file_is_usage_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    result = invoke(["playbook", "import", str(tmp_path / "missing.yaml")])
    assert result.exit_code == EXIT_USAGE, (result.exit_code, _text(result))
    assert "File not found" in _text(result)


@pytest.mark.fast
def test_playbook_import_malformed_yaml_is_usage_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(": : : not a mapping\n", encoding="utf-8")
    result = invoke(["playbook", "import", str(bad)])
    assert result.exit_code == EXIT_USAGE, (result.exit_code, _text(result))
    assert "Invalid playbook" in _text(result)


# ── negative control ──────────────────────────────────────────────────────


@pytest.mark.fast
def test_negative_control_oracle_rejects_a_wrong_exit_code(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    """Negative control: prove the exit-code oracle is not vacuous.

    ``config get no.such.key`` exits 5. The inner assertion deliberately names a
    wrong code; this test passes only because ``pytest.raises`` confirms the
    wrong oracle fails. Remove the ``pytest.raises`` wrapper (or make the oracle
    always-true) and this case goes red, which is the demonstration that the
    file can fail.
    """
    result = invoke(["config", "get", "no.such.key"])
    assert result.exit_code == EXIT_CONFIG, (result.exit_code, _text(result))
    with pytest.raises(AssertionError):
        assert result.exit_code == 0, "negative control: exit 5 is not exit 0"
