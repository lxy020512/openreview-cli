"""W4 fuzz suite: the ``auth.json`` WRITE boundary.

Sharp edge 7 (plan section 3). The read path is guarded — ``load_auth`` catches
``json.JSONDecodeError`` and raises ``AuthCorruptError``, and the
corrupt-``auth.json`` read is already asserted by ``tests/unit/test_auth.py``;
permissions likewise. This suite does NOT re-assert either. It targets the WRITE
sites: ``save_key`` and ``save_provider_credentials``, reached from the CLI
through ``gateway provider add``. Both now read through ``_read_auth_json``, so
a corrupt ``auth.json`` raises ``AuthCorruptError``, which the CLI maps to exit 5
(``Config error``).
"""

from __future__ import annotations

import json
import platform
from pathlib import Path

import pytest

from openreview_cli.config import auth
from openreview_cli.errors import EXIT_CONFIG
from tests.fuzz import _state_probe
from tests.helpers import corpus_state

pytestmark = pytest.mark.fuzz

_IS_WINDOWS = platform.system() == "Windows"


# ── CLI: a corrupt auth.json must fail the write cleanly ────────────────────


def test_auth_corrupt_credentials_file_is_a_clean_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _state_probe.prepare_state(monkeypatch, tmp_path)
    corpus_state.auth_json_invalid(state.config_dir)
    with _state_probe.count_calls(auth, "save_provider_credentials") as hits:
        result = _state_probe.run_cli(
            [
                "gateway",
                "provider",
                "add",
                "zzzprobe",
                "--base-url",
                "http://127.0.0.1:9",
                "--cred",
                "api_key=k",
            ]
        )
    assert hits[0] >= 1, "the auth write path was never reached"
    _state_probe.assert_clean_failure(result, frozenset({EXIT_CONFIG}), "Config error")


# ── CLI: a valid-but-wrong-mode file must be repaired and preserved ─────────


@pytest.mark.skipif(_IS_WINDOWS, reason="unix file modes only")
def test_auth_valid_file_with_wrong_mode_is_repaired_on_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _state_probe.prepare_state(monkeypatch, tmp_path)
    path = corpus_state.auth_json_wrong_mode(state.config_dir)
    assert path.stat().st_mode & 0o777 == 0o644  # precondition

    with _state_probe.count_calls(auth, "save_provider_credentials") as hits:
        result = _state_probe.run_cli(
            [
                "gateway",
                "provider",
                "add",
                "zzzprobe",
                "--base-url",
                "http://127.0.0.1:9",
                "--cred",
                "api_key=secret-canary",
            ]
        )
    assert hits[0] >= 1, "the auth write path was never reached"
    assert result.exit_code == 0, f"{result.exit_code}: {result.output!r}"
    # The write path repairs the mode (a write-path property, distinct from the
    # read-path permission assertion in tests/unit/test_auth.py) and keeps the
    # pre-existing entry.
    assert path.stat().st_mode & 0o777 == 0o600
    data = json.loads(path.read_text())
    assert data["openai"] == "sk-test"
    assert data["zzzprobe"]["api_key"] == "secret-canary"


# ── Library: both write sites map a corrupt file to AuthCorruptError ───


def test_save_key_corrupt_file_raises_auth_corrupt_error(tmp_path: Path) -> None:
    path = corpus_state.auth_json_invalid(tmp_path)
    with (
        _state_probe.count_calls(auth, "save_key") as hits,
        pytest.raises(auth.AuthCorruptError),
    ):
        auth.save_key(path, "openai", "sk-test")
    assert hits[0] >= 1


def test_save_provider_credentials_corrupt_file_raises_auth_corrupt_error(tmp_path: Path) -> None:
    path = corpus_state.auth_json_invalid(tmp_path)
    with (
        _state_probe.count_calls(auth, "save_provider_credentials") as hits,
        pytest.raises(auth.AuthCorruptError),
    ):
        auth.save_provider_credentials(path, "prov", {"api_key": "sk-test"})
    assert hits[0] >= 1


# ── Negative control: the oracle must be able to fail ───────────────────────


def test_negative_control_auth_oracle_rejects_a_successful_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A successful provider add is NOT a config error; the oracle must reject it."""
    _state_probe.prepare_state(monkeypatch, tmp_path)
    with _state_probe.count_calls(auth, "save_provider_credentials") as hits:
        result = _state_probe.run_cli(
            [
                "gateway",
                "provider",
                "add",
                "zzzprobe",
                "--base-url",
                "http://127.0.0.1:9",
                "--cred",
                "api_key=k",
            ]
        )
    assert hits[0] >= 1
    assert result.exit_code == 0, f"{result.exit_code}: {result.output!r}"
    with pytest.raises(AssertionError):
        _state_probe.assert_clean_failure(result, frozenset({EXIT_CONFIG}), "Config error")
