"""The local DeepSeek helper must persist configuration without network or key output."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from openreview_cli.config.loader import load_config


@pytest.fixture
def helper() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "configure_deepseek.py"
    spec = importlib.util.spec_from_file_location("configure_deepseek", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_preserves_other_credentials_slots_and_grounding_array(
    helper: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_dir = tmp_path / "user-config"
    config_dir.mkdir()
    config_path = config_dir / "config.yml"
    load_config(config_path)
    original = load_config(config_path)
    auth_path = config_dir / "auth.json"
    auth_path.write_text(json.dumps({"openai": "existing-placeholder"}), encoding="utf-8")
    monkeypatch.setattr(helper, "get_config_dir", lambda: config_dir)
    monkeypatch.setattr(helper.getpass, "getpass", lambda prompt: "secret-offline-placeholder")
    assert helper.configure("deepseek-flash", include_grounding=True) == 0

    updated = load_config(config_path)
    slots = updated["gateway"]["models"]
    for slot in ("extraction", "reasoning", "grounding"):
        assert slots[slot]["primary"] == "deepseek/deepseek-flash"
        assert slots[slot]["fallback"] is None
    for slot in ("embedding", "reranking", "graph"):
        assert slots[slot] == original["gateway"]["models"][slot]
    assert slots["extraction"]["extra_params"]["response_format"] == {"type": "json_object"}
    assert slots["reasoning"]["extra_params"]["response_format"] == {"type": "json_object"}
    assert not (slots["grounding"].get("extra_params") or {}).get("response_format")
    assert updated["privacy"]["tier"] == "balanced"
    assert updated["privacy"]["strip_pii"] is True
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    assert auth["deepseek"] == "secret-offline-placeholder"
    assert auth["openai"] == "existing-placeholder"
    output = capsys.readouterr()
    assert "secret-offline-placeholder" not in output.out + output.err
    assert "secret-offline-placeholder" not in config_path.read_text(encoding="utf-8")
    assert "secret-offline-placeholder" not in config_path.with_suffix(".yml.bak").read_text(
        encoding="utf-8"
    )


def test_no_grounding_preserves_its_existing_settings(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    load_config(tmp_path / "config.yml")
    original = load_config(tmp_path / "config.yml")
    monkeypatch.setattr(helper, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(helper.getpass, "getpass", lambda prompt: "offline-placeholder")
    assert helper.configure("deepseek-v4-pro", include_grounding=False) == 0
    updated = load_config(tmp_path / "config.yml")
    assert updated["gateway"]["models"]["grounding"] == original["gateway"]["models"]["grounding"]


def test_cancellation_does_not_write_key(
    helper: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(helper, "get_config_dir", lambda: tmp_path)

    def cancel(prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(helper.getpass, "getpass", cancel)
    assert helper.configure("deepseek-flash", include_grounding=True) == 1
    assert not (tmp_path / "auth.json").exists()
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("key", ["", "   "])
def test_empty_key_is_not_persisted(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setattr(helper, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(helper.getpass, "getpass", lambda prompt: key)
    assert helper.configure("deepseek-flash", include_grounding=True) == 1
    assert not (tmp_path / "auth.json").exists()


def test_corrupt_auth_error_does_not_print_its_contents_or_prompt_for_key(
    helper: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "auth.json").write_text("secret-in-broken-json", encoding="utf-8")
    monkeypatch.setattr(helper, "get_config_dir", lambda: tmp_path)

    def should_not_prompt(prompt: str) -> str:
        pytest.fail("Existing auth must be checked before requesting another secret.")

    monkeypatch.setattr(helper.getpass, "getpass", should_not_prompt)
    assert helper.configure("deepseek-flash", include_grounding=True) == 1
    output = capsys.readouterr()
    assert "secret-in-broken-json" not in output.out + output.err


def test_cli_rejects_legacy_model_without_prompting(
    helper: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(helper.sys, "argv", ["configure_deepseek.py", "--model", "deepseek-chat"])
    with pytest.raises(SystemExit) as exc:
        helper.main()
    assert exc.value.code == 2


def test_unavailable_hidden_input_aborts_instead_of_echoing(
    helper: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import warnings

    monkeypatch.setattr(helper, "get_config_dir", lambda: tmp_path)

    def unavailable(prompt: str) -> str:
        warnings.warn("Input would be echoed", helper.getpass.GetPassWarning, stacklevel=1)
        pytest.fail("Visible-input fallback must not continue.")

    monkeypatch.setattr(helper.getpass, "getpass", unavailable)
    assert helper.configure("deepseek-flash", include_grounding=True) == 1
    assert not (tmp_path / "auth.json").exists()
    assert "Input would be echoed" not in capsys.readouterr().err


def test_storage_exception_never_prints_secret(
    helper: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(helper, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(helper.getpass, "getpass", lambda prompt: "secret-offline-placeholder")

    def fail_save(*args: object) -> None:
        raise OSError("failure containing secret-offline-placeholder")

    monkeypatch.setattr(helper, "save_key", fail_save)
    assert helper.configure("deepseek-flash", include_grounding=True) == 1
    output = capsys.readouterr()
    assert "secret-offline-placeholder" not in output.out + output.err
